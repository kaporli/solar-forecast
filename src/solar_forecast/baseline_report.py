"""`solar-data evaluate <experiment>`: baselines over the validation folds of the built dataset; writes
data/evaluation/<experiment>-baselines.json and docs/evaluation/<experiment>-baselines.md. The test period is not
scored."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import baselines as B
from . import evaluation as E
from . import features as F
from . import sources

ROOT = sources.ROOT
ELIA = {"11h": "elia_da11_mwh", "18h": "elia_da18_mwh"}


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name: str):
    from .cli import _select, content_hash, experiment

    exp = experiment(name)
    man_path = ROOT / f"data/datasets/{name}-{F.FEATURE_VERSION}.json"
    manifest = json.loads(man_path.read_text())
    out = ROOT / f"data/build/{name}-{F.FEATURE_VERSION}"
    feats, targets = pd.read_parquet(out / "features.parquet"), pd.read_parquet(out / "targets.parquet")
    for f, df in (("features", feats), ("targets", targets)):
        want = manifest["outputs"][f"data/build/{name}-{F.FEATURE_VERSION}/{f}.parquet"]["content_sha256"]
        if content_hash(df) != want:
            raise SystemExit(f"{f}.parquet differs from {man_path.name}: rebuild with --inputs first")
    inputs = None
    if name == "belgium":
        inputs = F.BelgiumInputs(_select("elia", manifest), _select("weather-belgium", manifest), exp)
    return exp, manifest, man_path, feats, targets, inputs


def run(name: str) -> dict:
    exp, manifest, man_path, feats, targets, inputs = load(name)
    ex = E.scored_examples(exp, feats, targets)
    fs = E.folds(name)
    rows = {c.name: E.backtest(c, ex, fs) for c in B.candidates(name, inputs)}
    if name == "belgium":
        p = rows["persistence"]
        rows["elia"] = p.assign(candidate="elia", prediction=[r[ELIA[k]] for k, r in zip(p["kind"], p.to_dict("records"))])
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "src", "config"], cwd=ROOT, capture_output=True,
                                text=True).stdout)
    res = {
        "experiment": name, "built_at": datetime.now(timezone.utc).isoformat(), "code_commit": commit,
        "code_dirty": dirty, "command": f"uv run solar-data evaluate {name}",
        "dataset_manifest": {"file": str(man_path.relative_to(ROOT)), "sha256": _sha(man_path)},
        "protocol": {"file": "config/evaluation.json", "sha256": _sha(ROOT / "config/evaluation.json")},
        "splits": E.split_manifest(ex, fs),
        "test_period": E.CONFIG["experiments"][name]["test"] | {"scored": False},
        "results": {},
    }
    for kind in sorted(ex["kind"].unique()):
        r = {c: v[v["kind"] == kind] for c, v in rows.items()}
        k = {"pooled": {}, "folds": {}}
        for c, v in r.items():
            if c == "elia":
                continue
            k["pooled"][c] = E.metrics(v)
            k["folds"][c] = {f.name: E.metrics(v[v["fold"] == f.name]) for f in fs}
        k["physics_minus_persistence"] = E.block_bootstrap(r["physics"], r["persistence"])
        if "elia" in r:
            has = r["elia"]["prediction"].notna()
            k["elia_subset"] = {c: E.metrics(v[has.to_numpy()]) for c, v in r.items()}
            k["elia_missing_scored_hours"] = int((r["elia"]["scored"] & ~has).sum())
        k["lead"] = {c: {int(h): float(g["e"].abs().mean()) for h, g in
                         v[v["scored"] & v["daylight"]].assign(e=v["prediction"] - v["actual"]).groupby("lead_hours")}
                     for c, v in r.items() if c != "elia"}
        k["slices"] = _slices(r)
        res["results"][kind] = k
    out = ROOT / "data/evaluation"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}-baselines.json").write_text(json.dumps(res, indent=1) + "\n")
    doc = ROOT / "docs/evaluation"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / f"{name}-baselines.md").write_text(report(exp, res))
    return res


def _slices(r: dict) -> dict:
    min_days = E.CONFIG["scoring"]["slices"]["min_days"]
    p = r["persistence"]
    labels = {"season": p["target_date"].map(E.season), "sky": E.sky(p)}
    out = {}
    for s, lab in labels.items():
        for value in sorted(lab.dropna().unique()):
            mask = (lab == value).fillna(False).to_numpy(bool)
            days = p.loc[mask & p["scored"].to_numpy(), "target_date"].nunique()
            if days < min_days:
                out[f"{s}={value}"] = {"days": int(days), "reported": False}
                continue
            out[f"{s}={value}"] = {"days": int(days), "reported": True} | {
                c: E.metrics(v[mask])["mae_daylight"] for c, v in r.items() if c != "elia"}
    return out


def _f(x, nd=1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


def report(exp: F.Experiment, res: dict) -> str:
    u = E.CONFIG["experiments"][exp.name]["unit"]
    primary = E.CONFIG["experiments"][exp.name]["primary_kind"]
    L = [f"# Baseline validation: {exp.name}", "",
         f"Generated by `{res['command']}`; do not edit by hand. Code {res['code_commit'][:7]}"
         f"{' (uncommitted changes)' if res['code_dirty'] else ''}, dataset `{res['dataset_manifest']['file']}` "
         f"(sha256 {res['dataset_manifest']['sha256'][:12]}), protocol `config/evaluation.json` "
         f"(sha256 {res['protocol']['sha256'][:12]}). Unit: {u} per hour. Primary kind: {primary}.", "",
         f"The final test period ({res['test_period']['start']} to {res['test_period']['end']}) is not scored here.", "",
         "Retrospective evaluation under the availability assumptions in `docs/forecast-protocol.md`. Every "
         "validation row uses a run initialised from 2024-11-12 00Z (49r1 operational); training rows may use "
         "hindcast runs.", "",
         "## Splits", "", "| Fold | Target days | Trained on target days up to | Rows (all kinds) | Scored hours (all kinds) | Rows on hindcast runs |",
         "| --- | --- | --- | --- | --- | --- |"]
    L += [f"| {s['fold']} | {s['start']} to {s['end']} | {s['train_last_target']} | {s['rows']} | {s['scored_hours']} | "
          f"{s['rows_on_hindcast_runs']} |" for s in res["splits"]]
    for kind, k in res["results"].items():
        L += ["", f"## {kind}{' (primary)' if kind == primary else ''}", "",
              f"Pooled over validation folds. MAE, RMSE and bias in {u} per hour; daily in {u} per day.", "",
              "| Candidate | Daylight MAE | All-hours MAE | Daylight RMSE | All-hours RMSE | Daylight bias | Daily MAE | Daily bias | Scored hours | Daylight hours | Complete days |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for c, m in k["pooled"].items():
            L.append(f"| {c} | {_f(m['mae_daylight'])} | {_f(m['mae_all'])} | {_f(m['rmse_daylight'])} | "
                     f"{_f(m['rmse_all'])} | {_f(m['bias_daylight'])} | {_f(m['daily_mae'])} | {_f(m['daily_bias'])} | "
                     f"{m['hours']} | {m['daylight_hours']} | {m['complete_days']} |")
        names = [f["fold"] for f in res["splits"]]
        L += ["", "Daylight MAE by fold:", "", "| Candidate | " + " | ".join(names) + " |",
              "| --- |" + " --- |" * len(names)]
        for c, byf in k["folds"].items():
            L.append(f"| {c} | " + " | ".join(_f(byf[n]["mae_daylight"]) for n in names) + " |")
        b = k["physics_minus_persistence"]
        L += ["", f"Physics minus persistence, daylight MAE: {_f(b['difference'], 2)} {u} per hour, 95 % paired "
              f"block-bootstrap interval {_f(b['low'], 2)} to {_f(b['high'], 2)} ({b['days']} days, {b['hours']} hours, "
              f"{E.CONFIG['resampling']['block_days']}-day blocks). Autocorrelation of the daily difference at lags 1 "
              f"to {E.CONFIG['resampling']['block_days']}: "
              + ", ".join(_f(x, 2) for x in b["daily_difference_autocorrelation"]) + "."]
        if "elia_subset" in k:
            L += ["", f"Elia benchmark on scored hours with an Elia forecast ({k['elia_missing_scored_hours']} scored "
                  "hours without one are left out of this comparison only):", "",
                  "| Candidate | Daylight MAE | Daylight bias | Daily MAE | Scored hours |", "| --- | --- | --- | --- | --- |"]
            L += [f"| {c} | {_f(m['mae_daylight'])} | {_f(m['bias_daylight'])} | {_f(m['daily_mae'])} | {m['hours']} |"
                  for c, m in k["elia_subset"].items()]
        leads = sorted(set().union(*[set(v) for v in k["lead"].values()]))
        L += ["", "Daylight MAE by lead hour (hours after the run's initialisation):", "",
              "| Lead | " + " | ".join(k["lead"]) + " |", "| --- |" + " --- |" * len(k["lead"])]
        L += [f"| {h} | " + " | ".join(_f(k["lead"][c].get(h)) for c in k["lead"]) + " |" for h in leads]
        L += ["", f"Slices (daylight MAE; reported with at least {E.CONFIG['scoring']['slices']['min_days']} scored days):", "",
              "| Slice | Days | " + " | ".join(k["pooled"]) + " |", "| --- | --- |" + " --- |" * len(k["pooled"])]
        for s, v in k["slices"].items():
            cells = [_f(v.get(c)) if v["reported"] else "not reported" for c in k["pooled"]]
            L.append(f"| {s} | {v['days']} | " + " | ".join(cells) + " |")
    L += ["", "## Limitations", "",
          f"- Validation covers target days {res['splits'][0]['start']} to {res['splits'][-1]['end']}, one year at "
          "most: each season slice comes from a single year.",
          "- Physics uses assumed parameters (`config/evaluation.json`, physics), not fitted ones; calibration belongs "
          "to the learned-model comparison in #5.",
          ]
    if exp.name == "belgium":
        L += ["- The fleet geometry and performance ratio are assumptions for the whole Belgian fleet; one point per "
              "province stands for each province.",
              "- Elia forecasts are taken as archived in ods032 and ods209; whether they equal what Elia published at "
              "the time is unverified."]
    else:
        L += ["- One installation: no result here says anything about other sites or about the Belgian aggregate.",
              "- The test period is half a year (2025-07-01 to 2025-12-31); it will not cover a full season cycle."]
    return "\n".join(L) + "\n"
