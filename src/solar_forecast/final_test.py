"""`uv run python -m solar_forecast.final_test belgium`: the one final test of the lock (config/lock-belgium.json),
then the deployment artifact from the same fit. Writes data/evaluation/belgium-test.json,
docs/evaluation/belgium-test.md and artifacts/belgium/.

Before scoring it checks that nothing the comparison evaluated has changed since: every module that existed at the
lock's code commit, uv.lock, Python and library versions, the dataset manifest, the protocol and the recipes. New
modules (this one, `artifact`) are allowed; they are not used by the locked candidates. It refuses to run a second
time once results exist. `--rehearse <dir>` runs the same code on the last validation fold into <dir>, without
touching the test period or the once-only guard."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from . import artifact as A
from . import baselines as B
from . import evaluation as E
from . import features as F
from . import models as M
from .baseline_report import ELIA, _f, _sha, load
from .model_report import _Fixed, fingerprint, recipe, with_physics

ROOT = E.sources.ROOT
COMPARATORS = ["persistence", "physics", "physics_calibrated", "elia"]
FIXTURE_DAYS = ["2026-01-15", "2026-06-21", "2026-10-25"]  # winter, summer, the 25-hour day of October 2026


def drift(lock: dict) -> list[str]:
    ref = lock["references"]
    out = []
    changed = subprocess.run(["git", "diff", "--name-status", ref["code_commit"], "--", "src"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split("\n")
    out += [f"module {ln.split()[-1]}" for ln in changed if ln and not ln.startswith("A")]
    fp, now = ref["fingerprint"], fingerprint()
    for k in ("uv_lock_sha256", "python", "versions"):
        if fp[k] != now[k]:
            out.append(k)
    if _sha(ROOT / ref["dataset_manifest"]["file"]) != ref["dataset_manifest"]["sha256"]:
        out.append("dataset manifest")
    if _sha(ROOT / "config/evaluation.json") != ref["protocol"]["sha256"]:
        out.append("protocol")
    if _sha(ROOT / ref["comparison"]["file"]) != ref["comparison"]["sha256"]:
        out.append("comparison results")
    return out


def build(rec: dict):
    """The candidate object for a recorded recipe; refuses if the code no longer produces that recipe."""
    c = (M.GBM(rec["name"], rec["groups"], physics=rec["physics"], residual=rec["residual"], params=rec["params"])
         if rec["class"] == "GBM" else M.CalibratedPhysics(rec["name"]))
    if recipe(c) != {k: rec[k] for k in recipe(c)}:
        raise SystemExit(f"code no longer builds the locked recipe of {rec['name']}")
    return c


def run(name: str, rehearse: Path | None = None) -> dict:
    lock = json.loads((ROOT / f"config/lock-{name}.json").read_text())
    out_json, out_md = ROOT / f"data/evaluation/{name}-test.json", ROOT / f"docs/evaluation/{name}-test.md"
    art_dir = A.directory(name)
    if rehearse:
        rehearse.mkdir(parents=True, exist_ok=True)
        out_json, out_md, art_dir = rehearse / out_json.name, rehearse / out_md.name, rehearse / "artifact"
    elif out_json.exists():
        raise SystemExit(f"{out_json.relative_to(ROOT)} exists: the final test runs once")
    elif _git("status", "--porcelain", "src", "config"):
        raise SystemExit("uncommitted changes in src or config: the final test runs from a commit")
    bad = drift(lock)
    if bad:
        raise SystemExit("changed since the lock: " + "; ".join(bad))
    fold = E.folds(name)[-1] if rehearse else E.folds(name, include_test=True)[-1]
    if not rehearse and (str(fold.start), str(fold.end), str(E.train_end(fold))) != (
            lock["final_test"]["start"], lock["final_test"]["end"], lock["refit_recipe"]["last_target"]):
        raise SystemExit("test period or refit cutoff differs from the lock")

    exp, manifest, man_path, feats, targets, inputs = load(name)
    ex = with_physics(name, E.scored_examples(exp, feats, targets), inputs)
    sel = build(lock["model"])
    fitted = [sel] + [build(r) for r in lock["final_test"]["comparator_recipes"].values()]
    rows = {c.name: E.backtest(c, ex, [fold], unlock_test=not rehearse, extra=tuple(M.EXTRA)) for c in fitted}
    for c in (B.Persistence(), _Fixed("physics")):
        rows[c.name] = E.backtest(c, ex, [fold], unlock_test=not rehearse, extra=tuple(M.EXTRA))
    p = rows["persistence"]
    rows["elia"] = p.assign(candidate="elia", prediction=[r[ELIA[k]] for k, r in zip(p["kind"], p.to_dict("records"))])
    order = [sel.name] + COMPARATORS

    res = {"experiment": name, "run_at": datetime.now(timezone.utc).isoformat(),
           "command": f"uv run python -m solar_forecast.final_test {name}", "rehearsal": bool(rehearse),
           "code_commit": _git("rev-parse", "HEAD"), "code_dirty": bool(_git("status", "--porcelain", "src", "config")),
           "lock": {"file": f"config/lock-{name}.json", "sha256": _sha(ROOT / f"config/lock-{name}.json")},
           "selected": sel.name, "period": {"start": str(fold.start), "end": str(fold.end),
                                            "train_last_target": str(E.train_end(fold))},
           "splits": E.split_manifest(ex, [fold]), "results": {}}
    for kind in sorted(ex["kind"].unique()):
        r = {c: rows[c][rows[c]["kind"] == kind] for c in order}
        has = r["elia"]["prediction"].notna().to_numpy()
        k = {"all": {c: E.metrics(v) for c, v in r.items() if c != "elia"},
             "elia_subset": {c: E.metrics(v[has]) for c, v in r.items()},
             "elia_missing_scored_hours": int((r["elia"]["scored"] & ~has).sum()),
             "difference": {c: E.block_bootstrap(r[sel.name], r[c]) for c in COMPARATORS},
             "lead": {c: {int(h): float(g["e"].abs().mean()) for h, g in
                          v[v["scored"] & v["daylight"] & v["prediction"].notna()]
                          .assign(e=lambda d: d["prediction"] - d["actual"]).groupby("lead_hours")} for c, v in r.items()},
             "slices": slices(r, lock["final_test"]["slices"])}
        res["results"][kind] = k
    res["artifact"] = package(name, sel, lock, rows[sel.name], ex, art_dir, rehearse)
    out_json.write_text(json.dumps(res, indent=1, default=str) + "\n")
    validation = json.loads((ROOT / lock["references"]["comparison"]["file"]).read_text())
    out_md.write_text(report(exp, res, lock, validation))
    return res


def slices(r: dict, source_slices: list[str]) -> dict:
    """Daylight MAE per slice of the protocol (season, forecast sky class) and the lock's target-source slices.
    Slices with fewer days than `scoring.slices.min_days` are reported with their counts but marked."""
    base = next(iter(r.values()))
    cut = pd.Timestamp(source_slices[1].split()[1]).date()  # "from 2026-10-26 (ods209)"
    keys = {"source": pd.Series(np.where(base["target_date"] < cut, source_slices[0], source_slices[1]), index=base.index),
            "season": base["target_date"].map(E.season), "sky": E.sky(base)}
    out = {}
    for dim, key in keys.items():
        out[dim] = {}
        for val in key.dropna().unique():
            idx = key.index[key == val]
            m = {c: E.metrics(v.loc[idx][v.loc[idx]["prediction"].notna()]) for c, v in r.items()}
            days = int(base.loc[idx].loc[base.loc[idx, "scored"], "target_date"].nunique())
            out[dim][str(val)] = {"days": days, "enough_days": days >= E.CONFIG["scoring"]["slices"]["min_days"],
                                  "mae_daylight": {c: v["mae_daylight"] for c, v in m.items()},
                                  "daylight_hours": {c: v["daylight_hours"] for c, v in m.items()}}
    return out


def package(name: str, model, lock: dict, scored: pd.DataFrame, ex: pd.DataFrame, path: Path, rehearse) -> dict:
    """The model fitted by the scored run, with its manifest and reference fixtures; checks reload parity."""
    path.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    joblib.dump(model, buf)
    (path / "model.joblib").write_bytes(buf.getvalue())
    required = [c for g in model.groups for c in M.GROUPS[g] if c != "clearness"] + ["capacity"] + (
        ["physics_mwh"] if model.physics else [])
    days = [pd.Timestamp(d).date() for d in FIXTURE_DAYS] if not rehearse else sorted(scored["target_date"].unique())[-3:]
    fx = scored[scored["target_date"].isin(days)]
    fixtures = fx[["kind", "issuance_utc", "target_date", "target_hour_utc"] + required].assign(
        expected_mwh=fx["prediction"].to_numpy(float))
    fixtures.to_parquet(path / "fixtures.parquet", index=False)
    fp = fingerprint()
    man = {"experiment": name, "candidate": model.name, "created_at": datetime.now(timezone.utc).isoformat(),
           "model_sha256": hashlib.sha256(buf.getvalue()).hexdigest(), "model_bytes": len(buf.getvalue()),
           "schema": {"feature_version": F.FEATURE_VERSION, "required": required,
                      "dtypes": {c: F.CONTRACT[c][0] for c in required if c in F.CONTRACT},
                      "builder": "features.features_for_issuance (one row per local hour of the target day)",
                      "output": "energy per hour in the experiment's unit, clipped to [0, capacity x 1 h]"},
           "recipe": lock["model"], "seed": lock["model"]["seed"], "postprocessing": lock["postprocessing"],
           "fitted_on": {"first_target": str(E.CONFIG["training"]["first_target"]),
                         "last_target": str(E.train_end(E.folds(name, include_test=not rehearse)[-1])),
                         "rows": int(len(E.training_rows(ex, E.folds(name, include_test=not rehearse)[-1])))},
           "code_commit": _git("rev-parse", "HEAD"), "lock_sha256": _sha(ROOT / f"config/lock-{name}.json"),
           "dataset_manifest": lock["references"]["dataset_manifest"], "protocol": lock["references"]["protocol"],
           "uv_lock_sha256": fp["uv_lock_sha256"], "python": fp["python"], "versions": fp["versions"],
           "fixtures": {"file": "fixtures.parquet", "sha256": _sha(path / "fixtures.parquet"), "rows": int(len(fixtures)),
                        "target_days": [str(d) for d in days]}}
    (path / "manifest.json").write_text(json.dumps(man, indent=1, default=str) + "\n")
    gap = A.parity(path)
    if gap > 1e-9:
        raise SystemExit(f"reloaded artifact differs from the scored predictions by {gap}")
    return {"dir": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), "model_sha256": man["model_sha256"],
            "model_mb": man["model_bytes"] / 2**20, "fixture_rows": man["fixtures"]["rows"], "parity_max_abs": gap}


def _git(*a) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def report(exp: F.Experiment, res: dict, lock: dict, val: dict) -> str:
    u = E.CONFIG["experiments"][exp.name]["unit"]
    primary = E.CONFIG["experiments"][exp.name]["primary_kind"]
    s, per = res["selected"], res["period"]
    sp = res["splits"][0]
    title = "Final test" if not res["rehearsal"] else "Rehearsal on the last validation fold (not the final test)"
    L = [f"# {title}: {exp.name}", "",
         f"Generated by `{res['command']}`; do not edit by hand. Code {res['code_commit'][:7]}"
         f"{' (uncommitted changes)' if res['code_dirty'] else ''}, lock sha256 {res['lock']['sha256'][:12]}, run "
         f"{res['run_at'][:10]}. Unit: {u} per hour.", "",
         f"Period {per['start']} to {per['end']}: {sp['scored_hours']} scored hours over both kinds, "
         f"{sp['rows_on_hindcast_runs']} rows on hindcast runs. Run once, as locked: `{s}` and `physics_calibrated` "
         f"fitted once on target days {lock['refit_recipe']['first_target']} to {per['train_last_target']}, no refit "
         "inside the period, no tuning or switching after the result. Retrospective evaluation under the availability "
         "assumptions in `docs/forecast-protocol.md`; operational claims need issuances recorded live (#6).", ""]
    for kind, k in res["results"].items():
        a = k["all"]
        L += [f"## {kind}{' (primary)' if kind == primary else ''}", "",
              "| Model | Daylight MAE | All-hours MAE | Daylight RMSE | Daylight bias | Daily MAE | Daily bias | Scored hours | Complete days |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        L += [f"| {c} | {_f(m['mae_daylight'])} | {_f(m['mae_all'])} | {_f(m['rmse_daylight'])} | {_f(m['bias_daylight'])} | "
              f"{_f(m['daily_mae'])} | {_f(m['daily_bias'])} | {m['hours']} | {m['complete_days']} |" for c, m in a.items()]
        L += ["", f"Daylight MAE of `{s}` minus each comparator on hours both score (negative: `{s}` is better), "
              "with the 95 % paired moving-block bootstrap interval (14-day blocks):", "",
              "| Comparator | Difference | 95 % interval | Days | Hours |", "| --- | --- | --- | --- | --- |"]
        L += [f"| {c} | {_f(d['difference'])} | {_f(d['low'])} to {_f(d['high'])} | {d['days']} | {d['hours']} |"
              for c, d in k["difference"].items()]
        L += ["", f"On scored hours with an Elia forecast ({k['elia_missing_scored_hours']} scored hours without one):", "",
              "| Model | Daylight MAE | Daylight bias | Daily MAE |", "| --- | --- | --- | --- |"]
        L += [f"| {c} | {_f(m['mae_daylight'])} | {_f(m['bias_daylight'])} | {_f(m['daily_mae'])} |"
              for c, m in k["elia_subset"].items()]
        L += ["", "Daylight MAE by slice (slices under 14 days are listed but not interpreted):", "",
              "| Slice | Days | " + " | ".join(k["all"]) + " | elia |", "| --- | --- |" + " --- |" * (len(k["all"]) + 1)]
        for dim, vals in k["slices"].items():
            for v, m in sorted(vals.items()):
                mark = "" if m["enough_days"] else " (under 14 days)"
                L.append(f"| {dim}: {v}{mark} | {m['days']} | " + " | ".join(_f(m["mae_daylight"][c]) for c in list(k["all"]) + ["elia"]) + " |")
        L.append("")
    vp = val["results"][primary]["pooled"]
    L += ["## Validation and test, primary kind", "",
          f"Validation: pooled over folds {', '.join(x['fold'] for x in val['splits'])} "
          f"({val['splits'][0]['start']} to {val['splits'][-1]['end']}), each fold fitted on its own training window; "
          "it informed the selection. Test: the period above, scored once after the lock. Elia is compared on its own "
          "subset in each section and is not in this table.", "",
          "| Model | Validation daylight MAE | Test daylight MAE |", "| --- | --- | --- |"]
    L += [f"| {c} | {_f(vp[c]['mae_daylight']) if c in vp else ''} | {_f(m['mae_daylight'])} |"
          for c, m in res["results"][primary]["all"].items()]
    ar = res["artifact"]
    L += ["", "## Artifact", "",
          f"`{ar['dir']}`: the `{s}` model fitted in this run ({ar['model_mb']:.2f} MB, sha256 {ar['model_sha256'][:12]}), "
          f"`manifest.json` (input schema, recipe, seed, output clipping, code, lock, dataset, protocol and dependency "
          f"references) and `fixtures.parquet` ({ar['fixture_rows']} feature rows with their predictions). A fresh "
          f"reload reproduced the scored predictions (largest difference {ar['parity_max_abs']:.1e}). Prediction "
          "intervals were not validated (`docs/model-selection.md`), so the artifact has point forecasts only.", "",
          "## Limitations", "",
          "- One test year: each season appears once, so seasonal differences rest on a single season each.",
          f"- The target source changes on 2026-10-26; that slice has few days and is not interpreted.",
          "- Belgian aggregate only. PVDAQ 2107 is a separate installation-level experiment and is not tested here; "
          "nothing here generalises to other sites.",
          "- Retrospective: weather runs and capacity as archived, under the protocol's availability assumptions."]
    return "\n".join(L) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m solar_forecast.final_test")
    ap.add_argument("experiment", choices=["belgium"])
    ap.add_argument("--rehearse", type=Path, help="run on the last validation fold into this directory")
    a = ap.parse_args(argv)
    res = run(a.experiment, a.rehearse)
    print(json.dumps({k: {c: m["mae_daylight"] for c, m in v["all"].items()} for k, v in res["results"].items()}, indent=1))


if __name__ == "__main__":
    main()
