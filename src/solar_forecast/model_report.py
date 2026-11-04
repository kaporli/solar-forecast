"""`solar-data compare <experiment>`: learned candidates against the baselines on the validation folds, the
frozen selection rule, resource measurements and the chronological interval check. Writes
data/evaluation/<experiment>-models.json and docs/evaluation/<experiment>-models.md. The test period is not
touched; `solar-data lock belgium` records the selection before any test score exists."""
from __future__ import annotations

import hashlib
import io
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from . import baselines as B
from . import evaluation as E
from . import features as F
from . import models as M
from .baseline_report import ELIA, _f, _sha, load

ROOT = E.sources.ROOT
LEVELS = (0.8, 0.95)


def with_physics(name: str, ex: pd.DataFrame, inputs) -> pd.DataFrame:
    phys = B.BelgiumPhysics(inputs) if name == "belgium" else B.PvdaqPhysics()
    return ex.assign(physics_mwh=phys.predict(ex[list(F.CONTRACT)]))


def hardware() -> str:
    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    gb = f", {int(mem) / 2**30:.0f} GB" if mem.isdigit() else ""
    return f"{cpu or platform.processor()}{gb}, {platform.system()} {platform.release()}, Python {platform.python_version()}"


_PROBE = """
import resource, sys, time, joblib, pandas as pd
t = time.perf_counter(); m = joblib.load(sys.argv[1]); load = time.perf_counter() - t
rows = pd.read_parquet(sys.argv[2]); ts = []
for _ in range(20):
    t = time.perf_counter(); m.predict(rows); ts.append(time.perf_counter() - t)
ts.sort()
print(load, ts[10], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


def resources(candidate, rows: pd.DataFrame) -> dict:
    """Artifact size, load time, median predict time for one issuance and peak RSS of a fresh process."""
    buf = io.BytesIO()
    joblib.dump(candidate, buf)
    with tempfile.TemporaryDirectory() as d:
        art, inp = Path(d) / "model.joblib", Path(d) / "rows.parquet"
        art.write_bytes(buf.getvalue())
        rows.to_parquet(inp)
        out = subprocess.run([sys.executable, "-c", _PROBE, str(art), str(inp)], capture_output=True, text=True,
                             check=True).stdout.split()
    rss = int(out[2]) / (2**20 if sys.platform == "darwin" else 2**10)  # ru_maxrss: bytes on macOS, KiB on Linux
    return {"artifact_mb": len(buf.getvalue()) / 2**20, "load_s": float(out[0]), "predict_s": float(out[1]),
            "peak_rss_mb": rss, "inputs": len(candidate.inputs)}


INTERVAL_METHODS = {
    # name: (scale of the load-factor residual, grouping of the quantiles)
    "load factor, per local hour": (lambda d: pd.Series(1.0, index=d.index), "local_hour"),
    "clear-sky scaled, pooled": (lambda d: (d["ghi_clearsky"] / 1000).where(d["ghi_clearsky"] > 10), None),
    "clear-sky scaled, per local hour": (lambda d: (d["ghi_clearsky"] / 1000).where(d["ghi_clearsky"] > 10), "local_hour"),
    "prediction scaled, pooled": (lambda d: (d["prediction"] / d["capacity"]).where(d["prediction"] / d["capacity"] > 0.005), None),
}
ACCEPT = 0.05  # a method is usable if coverage is within 5 points of nominal at every level and in every fold


def interval_check(sel: pd.DataFrame, fs: list) -> dict:
    """Chronological residual-quantile intervals (not split conformal: residuals of different fold models are pooled
    and plain interpolated quantiles are used, without a finite-sample rank correction). For fold k: quantiles of the
    scaled load-factor residuals of earlier folds, restricted to labels known at fold k's first issuance (target
    day <= fold start - label lag, as for model fitting). Coverage and width on scored daylight hours, overall, by
    fold and by lead hour."""
    names = [f.name for f in fs]
    r = sel[sel["scored"]].assign(res=lambda d: (d["actual"] - d["prediction"]) / d["capacity"])
    out = {}
    for method, (scale, group) in INTERVAL_METHODS.items():
        m = {}
        for lv in LEVELS:
            lo_q, hi_q = (1 - lv) / 2, 1 - (1 - lv) / 2
            parts = []
            for i, fold in enumerate(fs[1:], 1):
                f = fold.name
                cal = r[r["fold"].isin(names[:i]) & (r["target_date"] <= E.train_end(fold))]
                z = (cal["res"] / scale(cal)).rename("z")
                cal = cal.assign(z=z)[np.isfinite(z)]
                t = r[r["fold"] == f]
                if group:
                    q = cal.groupby(group)["z"].quantile([lo_q, hi_q]).unstack()
                    lo_z, hi_z = t[group].map(q[lo_q]), t[group].map(q[hi_q])
                else:
                    lo_z, hi_z = cal["z"].quantile(lo_q), cal["z"].quantile(hi_q)
                s = scale(t).fillna(0) * t["capacity"]
                lo = (t["prediction"] + lo_z * s).clip(lower=0)
                hi = (t["prediction"] + hi_z * s).clip(upper=t["capacity"])
                parts.append(t.assign(inside=(t["actual"] >= lo) & (t["actual"] <= hi), width=hi - lo))
            day = pd.concat(parts)
            day = day[day["daylight"]]
            m[str(lv)] = {"coverage": float(day["inside"].mean()), "mean_width": float(day["width"].mean()),
                          "by_fold": {f: float(g["inside"].mean()) for f, g in day.groupby("fold")},
                          "by_lead": {int(h): {"coverage": float(g["inside"].mean()), "width": float(g["width"].mean())}
                                      for h, g in day.groupby("lead_hours")},
                          "hours": int(len(day))}
        m["usable"] = all(abs(c - float(lv)) <= ACCEPT for lv, v in m.items() for c in v["by_fold"].values())
        out[method] = m
    return {"folds_evaluated": names[1:], "methods": out,
            "frozen": next((k for k, v in out.items() if v["usable"]), None)}


def select(primary: dict, fold_names: list[str], cfg: dict = E.CONFIG) -> dict:
    """The frozen rule of config/evaluation.json, "selection"."""
    s = cfg["selection"]
    base = min(("persistence", "physics"), key=lambda c: primary["pooled"][c]["mae_daylight"])
    ref = primary["pooled"][base]["mae_daylight"]
    rows = {}
    for c in primary["candidates"]:
        m = primary["pooled"][c]["mae_daylight"]
        better = sum(primary["folds"][c][f]["mae_daylight"] < primary["folds"][base][f]["mae_daylight"] for f in fold_names)
        b = primary["vs_best_baseline"][c]
        rows[c] = {"improvement": 1 - m / ref, "interval_high": b["high"], "folds_better": better,
                   "passes": bool(1 - m / ref >= s["improvement_threshold"] and b["high"] < 0
                                  and better >= len(fold_names) - 1)}
    passing = [c for c in rows if rows[c]["passes"]]
    if not passing:
        return {"best_baseline": base, "checks": rows, "selected": base, "reason": "no learned candidate passes"}
    best = min(primary["pooled"][c]["mae_daylight"] for c in passing)
    near = [c for c in passing if primary["pooled"][c]["mae_daylight"] <= best * (1 + s["simpler_preferred_within"])]
    near.sort(key=lambda c: (primary["resources"][c]["inputs"], primary["resources"][c]["predict_s"]))
    return {"best_baseline": base, "checks": rows, "selected": near[0], "within_2_percent": near,
            "reason": "passes the rule" + ("; fewest inputs among candidates within 2 %" if len(near) > 1 else "")}


def run(name: str) -> dict:
    exp, manifest, man_path, feats, targets, inputs = load(name)
    ex = with_physics(name, E.scored_examples(exp, feats, targets), inputs)
    fs = E.folds(name)
    names = [f.name for f in fs]
    primary_kind = E.CONFIG["experiments"][name]["primary_kind"]
    cands, abl = M.candidates(), M.ablation()
    rows = {c.name: E.backtest(c, ex, fs, extra=tuple(M.EXTRA)) for c in (B.Persistence(), _Fixed("physics"))}
    t0 = time.perf_counter()
    for c in cands + abl:
        rows[c.name] = E.backtest(c, ex, fs, extra=tuple(M.EXTRA))
    fit_seconds = time.perf_counter() - t0
    if name == "belgium":
        p = rows["persistence"]
        rows["elia"] = p.assign(candidate="elia", prediction=[r[ELIA[k]] for k, r in zip(p["kind"], p.to_dict("records"))])
    test_fold = E.folds(name, include_test=True)[-1]
    refit_rows = E.training_rows(ex, test_fold)
    one = ex[(ex["kind"] == primary_kind) & (ex["target_date"] == ex.loc[ex["scored"], "target_date"].max())]
    res = {"experiment": name, "built_at": datetime.now(timezone.utc).isoformat(), "command": f"uv run solar-data compare {name}",
           "code_commit": _git("rev-parse", "HEAD"), "code_dirty": bool(_git("status", "--porcelain", "src", "config")),
           "dataset_manifest": {"file": str(man_path.relative_to(ROOT)), "sha256": _sha(man_path)},
           "protocol": {"file": "config/evaluation.json", "sha256": _sha(ROOT / "config/evaluation.json")},
           "hardware": hardware(), "gbm_params": M.GBM_PARAMS, "fit_seconds_all_folds": fit_seconds,
           "fingerprint": fingerprint(), "recipes": {c.name: recipe(c) for c in cands},
           "splits": E.split_manifest(ex, fs), "candidates": [c.name for c in cands], "ablation": [c.name for c in abl],
           "test_period": E.CONFIG["experiments"][name]["test"] | {"scored": False}, "results": {}}
    res["resources"] = {}
    for c in cands:
        t = time.perf_counter()
        c.fit(refit_rows)
        fit_s = time.perf_counter() - t
        res["resources"][c.name] = resources(c, one[list(F.CONTRACT) + M.EXTRA]) | {
            "fit_s_refit_recipe": fit_s, "refit_rows": int(len(refit_rows)),
            "refit_last_target": str(refit_rows["target_date"].max())}
    for kind in sorted(ex["kind"].unique()):
        r = {c: v[v["kind"] == kind] for c, v in rows.items()}
        k = {"pooled": {c: E.metrics(v) for c, v in r.items() if c != "elia"},
             "folds": {c: {f: E.metrics(v[v["fold"] == f]) for f in names} for c, v in r.items() if c != "elia"},
             "candidates": [c.name for c in cands]}
        base = min(("persistence", "physics"), key=lambda c: k["pooled"][c]["mae_daylight"])
        k["vs_best_baseline"] = {c: E.block_bootstrap(r[c], r[base]) for c in [x.name for x in cands + abl]}
        if "elia" in r:
            has = r["elia"]["prediction"].notna().to_numpy()
            k["elia_subset"] = {c: E.metrics(v[has]) for c, v in r.items() if c in ("persistence", "physics", "elia")
                                or c in k["candidates"]}
        k["resources"] = res["resources"]
        res["results"][kind] = k
    prim = res["results"][primary_kind]
    res["selection"] = select(prim, names)
    res["selection"]["applies_to"] = primary_kind
    sel = rows[res["selection"]["selected"]]
    res["intervals"] = {kind: interval_check(sel[sel["kind"] == kind], fs) for kind in res["results"]}
    out = ROOT / "data/evaluation"
    (out / f"{name}-models.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    (ROOT / f"docs/evaluation/{name}-models.md").write_text(report(exp, res))
    return res


class _Fixed:
    """The physics baseline as a backtest candidate, reading the precomputed `physics_mwh`."""

    def __init__(self, name):
        self.name = name

    def fit(self, train):
        pass

    def predict(self, rows):
        return rows["physics_mwh"].to_numpy(float)


def _git(*a) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def report(exp: F.Experiment, res: dict) -> str:
    u = E.CONFIG["experiments"][exp.name]["unit"]
    primary = E.CONFIG["experiments"][exp.name]["primary_kind"]
    sel = res["selection"]
    L = [f"# Learned candidates: {exp.name}", "",
         f"Generated by `{res['command']}`; do not edit by hand. Code {res['code_commit'][:7]}"
         f"{' (uncommitted changes)' if res['code_dirty'] else ''}, dataset `{res['dataset_manifest']['file']}` "
         f"(sha256 {res['dataset_manifest']['sha256'][:12]}), protocol sha256 {res['protocol']['sha256'][:12]}. "
         f"Unit: {u} per hour.", "",
         f"Validation folds only ({res['splits'][0]['start']} to {res['splits'][-1]['end']}); the final test period "
         f"({res['test_period']['start']} to {res['test_period']['end']}) is not scored. Retrospective evaluation under "
         "the availability assumptions in `docs/forecast-protocol.md`.", "",
         "Candidates (fitted per fold on target days up to fold start minus two days, see "
         "`docs/evaluation-protocol.md`):", "",
         "- `physics_calibrated`: the physics baseline times one least-squares factor on load factor.",
         "- `gbm`: gradient-boosted trees (scikit-learn HistGradientBoosting, absolute-error loss) on load factor from "
         "forecast irradiance and clear-sky index, temperature, wind and cloud cover, solar geometry, day of year and "
         "local hour, lead time and persistence load factor.",
         "- `physics_residual_gbm`: calibrated physics plus trees on the remaining load factor, with the physics load "
         "factor as an extra input; the calibration is refitted inside each fold.",
         f"- Tree settings, fixed before validation and not tuned: `{json.dumps(res['gbm_params'])}`. Tuning on the "
         "validation folds would be allowed, but its scores would then no longer be an unbiased estimate (that needs "
         "nested chronological validation); the untouched final test evaluates the locked selection.", ""]
    names = [s["fold"] for s in res["splits"]]
    for kind, k in res["results"].items():
        L += [f"## {kind}{' (primary)' if kind == primary else ''}", "",
              "| Candidate | Daylight MAE | vs best baseline | 95 % interval of the difference | All-hours MAE | Daylight RMSE | Daylight bias | Daily MAE | Scored hours |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        base = min(("persistence", "physics"), key=lambda c: k["pooled"][c]["mae_daylight"])
        ref = k["pooled"][base]["mae_daylight"]
        for c, m in k["pooled"].items():
            b = k["vs_best_baseline"].get(c)
            rel = f"{(m['mae_daylight'] / ref - 1) * 100:+.1f} %"
            ci = f"{_f(b['low'])} to {_f(b['high'])}" if b else ""
            tag = " (ablation)" if c in res["ablation"] else ""
            L.append(f"| {c}{tag} | {_f(m['mae_daylight'])} | {rel} | {ci} | {_f(m['mae_all'])} | "
                     f"{_f(m['rmse_daylight'])} | {_f(m['bias_daylight'])} | {_f(m['daily_mae'])} | {m['hours']} |")
        L += ["", f"Best baseline: {base}. Daylight MAE by fold:", "", "| Candidate | " + " | ".join(names) + " |",
              "| --- |" + " --- |" * len(names)]
        L += [f"| {c} | " + " | ".join(_f(byf[n]["mae_daylight"]) for n in names) + " |" for c, byf in k["folds"].items()]
        if "elia_subset" in k:
            L += ["", "On scored hours with an Elia forecast:", "", "| Candidate | Daylight MAE | Daylight bias | Daily MAE |",
                  "| --- | --- | --- | --- |"]
            L += [f"| {c} | {_f(m['mae_daylight'])} | {_f(m['bias_daylight'])} | {_f(m['daily_mae'])} |"
                  for c, m in k["elia_subset"].items()]
        L.append("")
    L += ["## Selection (frozen rule, primary kind)", "",
          "| Candidate | Improvement over best baseline | Interval upper bound | Folds better | Passes |",
          "| --- | --- | --- | --- | --- |"]
    L += [f"| {c} | {v['improvement'] * 100:.1f} % | {_f(v['interval_high'])} | {v['folds_better']} of {len(names)} | "
          f"{'yes' if v['passes'] else 'no'} |" for c, v in sel["checks"].items()]
    L += ["", f"Selected: **{sel['selected']}** ({sel['reason']}). Ablation variants are reported, not selected from.", "",
          f"## Resources ({res['hardware']})", "",
          "Each candidate fitted with the refit recipe (target days up to the day before the test's first issuance), "
          "measured in a fresh Python process: one issuance of the primary kind, median of 20 predictions. Physics "
          "and feature construction are not included. Deployment-host measurements belong to #6.", "",
          "| Candidate | Inputs | Artifact MB | Load s | Predict s | Peak RSS MB | Fit s |", "| --- | --- | --- | --- | --- | --- | --- |"]
    L += [f"| {c} | {v['inputs']} | {v['artifact_mb']:.2f} | {v['load_s']:.3f} | {v['predict_s']:.4f} | {v['peak_rss_mb']:.0f} | "
          f"{v['fit_s_refit_recipe']:.1f} |" for c, v in res["resources"].items()]
    L += ["", "## Prediction intervals (chronological check)", "",
          "Chronological residual-quantile intervals for the selected model: quantiles of its load-factor residuals, "
          "scaled as named, from earlier folds and only from labels known at the fold's first issuance. This is not "
          "split conformal prediction (residuals of different fold models are pooled, plain quantiles, no finite-sample "
          "correction). Daylight scored hours of the evaluated folds; hourly only, no daily intervals. "
          f"A method is usable only if coverage is within {ACCEPT * 100:.0f} points of nominal at both levels in every "
          "fold.", ""]
    for kind, iv in res["intervals"].items():
        L += [f"{kind} (folds {', '.join(iv['folds_evaluated'])}):", "",
              "| Method | 80 %: coverage | by fold | mean width | 95 %: coverage | by fold | mean width | Usable |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for meth, m in iv["methods"].items():
            a, b = m["0.8"], m["0.95"]
            fold = lambda v: " / ".join(f"{x * 100:.0f}" for x in v["by_fold"].values())
            L.append(f"| {meth} | {a['coverage'] * 100:.1f} % | {fold(a)} | {_f(a['mean_width'])} | {b['coverage'] * 100:.1f} % | "
                     f"{fold(b)} | {_f(b['mean_width'])} | {'yes' if m['usable'] else 'no'} |")
        L.append("")
    frozen = {k: v["frozen"] for k, v in res["intervals"].items()}
    L.append("Result: " + ("; ".join(f"{k}: {v}" for k, v in frozen.items() if v) if any(frozen.values()) else
             "none of these methods is usable, so none is frozen and the forecast is published without uncertainty "
             "bands: prediction intervals have not been validated. With at most one year of chronological validation, "
             "each season is calibrated on other seasons. This rules out these residual-quantile intervals, not "
             "conformal prediction in general."))
    L += ["", "## Limitations", "",
          f"- Validation spans {res['splits'][0]['start']} to {res['splits'][-1]['end']}: each season appears at most "
          "once; season-specific conclusions are not drawn.",
          "- The ablation is not used for selection: removing lags or calendar inputs does not hurt here, but the "
          "selection is made among the three planned candidates only.",
          "- Feature importance is not reported; the ablation shows what removing a group does here, not why.",
          "- Historical scores are retrospective; operational claims need issuances recorded live (#6)."]
    if exp.name == "pvdaq":
        L.append("- One installation; these results do not validate the Belgian model or other sites.")
    return "\n".join(L) + "\n"


def recipe(c) -> dict:
    """What a candidate is, as evaluated: class, input groups, inputs, residual flag and tree settings."""
    return json.loads(json.dumps({"class": type(c).__name__, "name": c.name, "groups": getattr(c, "groups", None),
                                  "inputs": c.inputs, "physics": getattr(c, "physics", None),
                                  "residual": getattr(c, "residual", None), "params": getattr(c, "params", None)},
                                 default=str))


def fingerprint() -> dict:
    """Implementation and dependency identity: SHA-256 over every module of the package, uv.lock, and the versions
    of the libraries the candidates run on."""
    from importlib.metadata import version

    h = hashlib.sha256()
    for f in sorted((ROOT / "src/solar_forecast").glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    libs = ["numpy", "pandas", "scikit-learn", "pvlib", "pyarrow", "joblib"]
    return {"src_sha256": h.hexdigest(), "uv_lock_sha256": _sha(ROOT / "uv.lock"),
            "python": platform.python_version(), "versions": {k: version(k) for k in libs}}


def lock(name: str) -> dict:
    """Record the selection before the final test, from what the comparison evaluated: the candidate's recorded
    recipe, output constraints, refit recipe, final-test comparators and the checksums it rests on. Refuses if the
    comparison came from uncommitted code, or if the dataset, protocol, implementation, dependencies or the
    candidate's recipe differ now from when it was evaluated."""
    path = ROOT / f"data/evaluation/{name}-models.json"
    res = json.loads(path.read_text())
    man = ROOT / res["dataset_manifest"]["file"]
    drift = []
    if res["code_dirty"]:
        drift.append("comparison built from uncommitted code")
    if _sha(man) != res["dataset_manifest"]["sha256"]:
        drift.append("dataset manifest")
    if _sha(ROOT / "config/evaluation.json") != res["protocol"]["sha256"]:
        drift.append("protocol")
    if fingerprint() != res.get("fingerprint"):
        drift.append("implementation or dependencies (fingerprint)")
    sel = res["selection"]["selected"]
    current = {c.name: recipe(c) for c in M.candidates()}
    evaluated = res.get("recipes", {})
    if sel in current and current.get(sel) != evaluated.get(sel):
        drift.append(f"recipe of {sel}")
    if drift:
        raise SystemExit("cannot lock, changed since the comparison: " + "; ".join(drift) + ". Rerun compare.")
    test = E.folds(name, include_test=True)[-1]
    model = evaluated.get(sel)
    out = {
        "experiment": name, "locked_at": datetime.now(timezone.utc).isoformat(), "selected": sel,
        "selection": res["selection"],
        "model": None if model is None else model | {
            "target": "load factor: actual / capacity known at issuance; prediction x capacity",
            "seed": (model.get("params") or {}).get("random_state")},
        "postprocessing": E.CONFIG["scoring"]["postprocessing"],
        "refit_recipe": {"train_rule": E.CONFIG["training"]["rule"], "last_target": str(E.train_end(test)),
                         "first_target": E.CONFIG["training"]["first_target"],
                         "rows": "target eligible, weather_complete, capacity_complete",
                         "applies_to": "the selected candidate and every fitted comparator"},
        "final_test": {"start": str(test.start), "end": str(test.end), "kinds": "all; primary " +
                       E.CONFIG["experiments"][name]["primary_kind"],
                       "reported_with": ["persistence", "physics", "physics_calibrated"]
                       + (["elia"] if name == "belgium" else []),
                       "comparator_recipes": {c: evaluated[c] for c in ("physics_calibrated",) if c in evaluated and c != sel},
                       "slices": E.CONFIG["experiments"][name]["test_slices"],
                       "runs": "once; no tuning, switching or rerun with other settings against the test"},
        "intervals": res["intervals"][E.CONFIG["experiments"][name]["primary_kind"]]["frozen"],
        "references": {"comparison": {"file": str(path.relative_to(ROOT)), "sha256": _sha(path)},
                       "code_commit": res["code_commit"], "dataset_manifest": res["dataset_manifest"],
                       "protocol": res["protocol"], "fingerprint": res["fingerprint"]},
    }
    (ROOT / f"config/lock-{name}.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    return out
