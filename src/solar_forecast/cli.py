"""solar-data: fetch snapshots, build targets and features, write the manifest and the quality report.

    uv run solar-data fetch belgium|pvdaq
    uv run solar-data build belgium|pvdaq [--inputs data/datasets/<experiment>-v1.json]
    uv run solar-data evaluate belgium|pvdaq
    uv run solar-data compare belgium|pvdaq
    uv run solar-data lock belgium
    uv run solar-data contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import features as F
from . import sources

ROOT = sources.ROOT
BUILD = ROOT / "data/build"
DATASETS = ROOT / "data/datasets"
QUALITY = ROOT / "docs/quality"
SPLIT = pd.Timestamp("2024-11-12", tz="UTC")  # 49r1 operational at ECMWF; runs initialised earlier are hindcasts


def experiment(name: str) -> F.Experiment:
    return F.belgium() if name == "belgium" else F.pvdaq()


def fetch(name: str, weather_only: bool = False, workers: int = 4) -> None:
    exp = experiment(name)
    if weather_only:
        pass
    elif name == "belgium":
        for sel in sources.elia_selections():
            print("snapshot", sources.fetch_elia(sel).name)
    else:
        print("pvdaq files", [p.name for p in sources.fetch_pvdaq(json.loads((ROOT / "data/manifest.json").read_text()))])
    have = {pd.Timestamp(m["run"]) for m in sources.snapshots(f"weather-{name}")}
    today = pd.Timestamp.now(tz=exp.tz).date()
    last = today + pd.Timedelta(days=1) if name == "belgium" else pd.Timestamp("2025-12-31").date()
    runs = sorted({F.rule_run(exp, F.issuance_time(exp, d, k), k) for d, k in F.issuances(exp, last)} - have)
    print(f"{len(runs)} weather runs to fetch")

    def one(run):
        try:
            sources.fetch_run(name, run)
        except (ValueError, urllib.error.HTTPError) as e:  # not published yet, or upstream down after retries
            print("skipped", run, e, flush=True)

    with ThreadPoolExecutor(workers) as pool:  # Open-Meteo's free limit is 600 calls a minute
        for i, _ in enumerate(pool.map(one, runs), 1):
            if i % 100 == 0:
                print(i, "fetched", flush=True)


def _rel(p) -> str:
    return str(Path(p).resolve().relative_to(ROOT))


def _select(source: str, pinned: dict | None) -> list[dict]:
    metas = sources.snapshots(source)
    if pinned is None:
        return metas
    want = {e["file"]: e for e in pinned["inputs"] if e["source"] == source}
    got = [m for m in metas if _rel(m["path"]) in want]
    if len(got) != len(want):
        raise SystemExit(f"{source}: {len(want) - len(got)} pinned snapshots are not in data/raw/snapshots")
    for m in got:
        e = want[_rel(m["path"])]
        if m["sha256"] != e["sha256"]:
            raise SystemExit(f"{m['path']}: payload checksum differs from the manifest")
        # the sidecar decides availability (retrieved_at) and identity (run, dataset): pinned byte for byte
        if m["sidecar_sha256"] != e.get("sidecar_sha256"):
            raise SystemExit(f"{m['sidecar']}: sidecar differs from the manifest")
    return got


def content_hash(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df.astype(str), index=False).to_numpy().tobytes()).hexdigest()


def build(name: str, inputs_file: str | None) -> None:
    exp = experiment(name)
    pinned = json.loads(Path(inputs_file).read_text()) if inputs_file else None
    weather = _select(f"weather-{name}", pinned)
    if name == "belgium":
        elia = _select("elia", pinned)
        inputs, used = F.BelgiumInputs(elia, weather, exp), elia + weather
    else:
        pv = _select("pvdaq", pinned)
        inputs, used = F.PvdaqInputs(pv, weather, exp, json.loads((ROOT / "data/manifest.json").read_text())), pv + weather

    targets = inputs.targets()
    last = targets["local_date"].max() if name == "pvdaq" else max(
        d for d, g in targets.groupby("local_date") if g["n_quarter_hours"].sum() > 0)
    feats = F.build_features(exp, inputs, F.issuances(exp, last))
    window = targets[targets["local_date"] >= pd.Timestamp(exp.first_target).date()]

    out = BUILD / f"{name}-{F.FEATURE_VERSION}"
    out.mkdir(parents=True, exist_ok=True)
    targets.to_parquet(out / "targets.parquet", index=False)
    feats.to_parquet(out / "features.parquet", index=False)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "src"], cwd=ROOT, capture_output=True, text=True).stdout)
    manifest = {
        "experiment": name, "feature_version": F.FEATURE_VERSION,
        "label": "measured and upscaled" if name == "belgium" else "revenue meter, AC",
        "built_at": datetime.now(timezone.utc).isoformat(), "code_commit": commit, "code_dirty": dirty,
        "command": f"uv run solar-data build {name} --inputs data/datasets/{name}-{F.FEATURE_VERSION}.json",
        "window": {"first_target": exp.first_target, "last_target": str(last)},
        "outputs": {
            f"data/build/{name}-{F.FEATURE_VERSION}/targets.parquet": {"rows": len(targets), "content_sha256": content_hash(targets)},
            f"data/build/{name}-{F.FEATURE_VERSION}/features.parquet": {"rows": len(feats), "content_sha256": content_hash(feats)},
        },
        "inputs": [{"source": m["source"], "name": m["name"], "file": _rel(m["path"]), "sha256": m["sha256"],
                    "sidecar": _rel(m["sidecar"]), "sidecar_sha256": m["sidecar_sha256"],
                    "retrieved_at": m["retrieved_at"]} for m in used],
    }
    DATASETS.mkdir(parents=True, exist_ok=True)
    (DATASETS / f"{name}-{F.FEATURE_VERSION}.json").write_text(json.dumps(manifest, indent=1) + "\n")
    QUALITY.mkdir(parents=True, exist_ok=True)
    (QUALITY / f"{name}.md").write_text(report(exp, inputs, window, feats, manifest))
    (ROOT / "docs/feature-contract.md").write_text(F.contract_markdown())
    print(json.dumps(manifest["outputs"], indent=1))


def _ranges(days) -> str:
    days = sorted(days)
    if not days:
        return "none"
    out, start, prev = [], days[0], days[0]
    for d in days[1:] + [None]:
        if d is not None and (d - prev).days == 1:
            prev = d
            continue
        out.append(str(start) if start == prev else f"{start} to {prev}")
        if d is not None:
            start = prev = d
    return ", ".join(out)


def period(run) -> str:
    """Provenance period of a row, from the initialisation of its weather run."""
    if pd.isna(run):
        return "no run"
    return "hindcast runs (initialised before 2024-11-12 00Z)" if run < SPLIT else "runs from 2024-11-12 00Z"


def report(exp: F.Experiment, inputs, t: pd.DataFrame, feats: pd.DataFrame, manifest: dict) -> str:
    unit = "MWh" if exp.name == "belgium" else "kWh"
    by_day = t.groupby("local_date")["observed"].all()
    excl = t.loc[t["exclusion"] != "", "exclusion"].str.split(",").explode().value_counts().to_dict()
    lines = [
        f"# Data quality: {exp.name} (features {F.FEATURE_VERSION})",
        "",
        f"Generated by `{manifest['command']}`; do not edit by hand. Code {manifest['code_commit'][:7]}"
        f"{' (uncommitted changes)' if manifest['code_dirty'] else ''}, {len(manifest['inputs'])} input snapshots listed in "
        f"`data/datasets/{exp.name}-{F.FEATURE_VERSION}.json`. Window {manifest['window']['first_target']} to "
        f"{manifest['window']['last_target']}.",
        "",
        "## Target" + (" (Elia, measured and upscaled)" if exp.name == "belgium" else " (revenue meter)"),
        "",
        "| | Count |", "| --- | --- |",
        f"| Hours in window | {len(t)} |",
        f"| Hours observed (four valid quarter-hours) | {int(t['observed'].sum())} |",
        f"| Hours missing | {int((~t['observed']).sum())} |",
        f"| of which with a night-zero estimate (kept separate) | {int(t['night_zero_estimate'].notna().sum())} |",
        f"| Hours eligible for primary metrics (observed, no exclusion) | {int(t['eligible'].sum())} |",
        f"| Exclusions by reason (hours) | {excl or 'none'} |",
        f"| Calendar days | {len(by_day)} |",
        f"| Days with every hour observed | {int(by_day.sum())} |",
        "",
        f"Incomplete days: {_ranges([d for d, ok in by_day.items() if not ok])}.",
        "",
    ]
    if exp.name == "belgium":
        b = inputs.resolved()
        b = b[(b["region"] == "Belgium")]
        lines += [
            f"Before cleaning: {len(b)} Belgium quarter-hours across snapshots "
            f"{inputs.stats['snapshots']}, {int(b['measured_mw'].isna().sum())} without a measured value; "
            f"{inputs.stats['duplicates_dropped']} identical duplicates dropped; {inputs.revisions()} quarter-hours "
            "revised between snapshots.", "",
            "## Elia benchmark", "",
            f"Hours with the 11:00 day-ahead forecast: {int(t['elia_da11_mwh'].notna().sum())} of {len(t)}; "
            f"with the 18:00 forecast: {int(t['elia_da18_mwh'].notna().sum())}. Days without any 11:00 forecast: "
            f"{_ranges([d for d, g in t.groupby('local_date') if g['elia_da11_mwh'].isna().all()])}.", "",
        ]
    else:
        lines += [f"Before cleaning: {inputs.stats['rows']} meter rows, {inputs.stats['duplicates_dropped']} identical "
                  f"duplicates dropped, {inputs.stats['labels_dropped']} ambiguous or nonexistent DST labels dropped.", ""]
    lines += ["## Weather runs", ""]
    for kind, g in feats.groupby("kind"):
        per = g.groupby("issuance_utc")["weather_availability"].first()
        missing = sorted(set(g.loc[g["weather_availability"] == "missing", "target_date"]))
        lines.append(f"- {kind}: {len(per)} issuances, availability {per.value_counts().to_dict()}; "
                     f"target days without a run: {_ranges(missing)}.")
    bad = feats.loc[~feats["capacity_complete"]]
    lines.append(f"- Capacity incomplete at the cutoff (weather and solar geometry null): "
                 f"{bad['issuance_utc'].nunique()} issuances; target days: {_ranges(sorted(set(bad['target_date'])))}.")
    lines += ["", "## Usable examples", "",
              "Usable: target hour eligible, weather complete, persistence available. Days usable: every hour usable. "
              "Period: by the initialisation of the weather run used, not the target day.", "",
              "| Kind | Period | Issuances | Rows | Eligible | Weather complete | Persistence | Usable | Days usable | Persistence not from D-2 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    ex = F.examples(feats, t[["hour_utc", "local_date", "local_hour", "eligible"]])
    ex["usable"] = ex["eligible"].fillna(False).astype(bool) & ex["weather_complete"] & ex["persistence_lf"].notna()
    ex["period"] = ex["weather_run_utc"].map(period)
    for (kind, per), g in ex.groupby(["kind", "period"], sort=False):
        d2 = sum(1 for d, s in zip(g["target_date"], g["persistence_source_date"])
                 if s is not None and (d - s).days != 2)
        lines.append(f"| {kind} | {per} | {g['issuance_utc'].nunique()} | {len(g)} | "
                     f"{int(g['eligible'].fillna(False).sum())} | {int(g['weather_complete'].sum())} | "
                     f"{int(g['persistence_lf'].notna().sum())} | {int(g['usable'].sum())} | "
                     f"{int(g.groupby('target_date')['usable'].all().sum())} | {d2} |")
    lines += ["", f"Target unit: {unit} per hour. Historical rows are a retrospective evaluation under the availability "
              "assumptions in `docs/forecast-protocol.md`.", ""]
    return "\n".join(lines)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="solar-data")
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("fetch", "build", "evaluate", "compare", "lock"):
        s = sub.add_parser(c)
        s.add_argument("experiment", choices=["belgium", "pvdaq"])
        if c == "fetch":
            s.add_argument("--weather-only", action="store_true", help="skip Elia and PVDAQ, fetch missing runs only")
        if c == "build":
            s.add_argument("--inputs", help="rebuild from exactly the snapshots listed in a dataset manifest")
    sub.add_parser("contract")
    sub.add_parser("snapshot", help="save the live Elia datasets (ods209, ods210) with their retrieval time")
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        fetch(a.experiment, a.weather_only)
    elif a.cmd == "build":
        build(a.experiment, a.inputs)
    elif a.cmd == "evaluate":
        from .baseline_report import run
        run(a.experiment)
    elif a.cmd == "compare":
        from .model_report import run
        run(a.experiment)
    elif a.cmd == "lock":
        from .model_report import lock
        print(json.dumps(lock(a.experiment)["selection"], indent=1))
    elif a.cmd == "snapshot":
        for sel in ("ods209_total", "ods210_total"):  # Elia's day-ahead forecasts vanish from ods209 later
            print("snapshot", sources.fetch_elia(sel).name)
    else:
        sys.stdout.write(F.contract_markdown())


if __name__ == "__main__":
    main()
