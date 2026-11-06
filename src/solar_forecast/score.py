"""`solar-data score belgium`: score issued forecasts against Elia actuals (#6, policy in docs/deployment.md).

Each issued record whose target day has started is scored when it has no version yet or when a retrieval made
since the last run holds rows in its target day:

- **Provisional:** every Elia retrieval stored so far, resolved field by field (ods209 before ods210, newest
  non-null, `index.resolved`). An hour counts once all four quarter-hours are present (`targets.belgium_target`).
- **Final:** once, the actuals (measured and capacity) of the first stored ods209 retrieval made at least
  `FINAL_AFTER` after the target day ends in Europe/Brussels whose requested UTC interval holds the whole local
  day. The Elia benchmark is resolved over all retrievals as before, independently of that retrieval.
- **Revised:** after the final version, a later correction or newly supplied observation (all retrievals again)
  appends a version; the final version stays.

A version is written only when the per-hour actuals or benchmark differ from the latest version (or on
finalization): data/scores/<experiment>/<target>_<kind>.v<N>.json, published exclusively (`issue.publish`), so
versions are immutable and earlier ones stay. Each references the forecast record and every retrieval a used field
came from. Missing hours stay missing and are counted in `coverage`; daily totals need a complete day.
The forecast, persistence and physics are scored on the same hours: an hour where one of them is missing is not
scored and is listed in `coverage.missing_predictions`. The Elia benchmark of the kind (`dayahead11hforecast` for
11h, `dayaheadforecast` for 18h) is scored on those of them where Elia published one.

`summary.json` pools by kind and weather availability (prescribed run `recorded`, `fallback`): the latest version
of each issuance (`all`) and its final version (`final`). It also holds `elia_through`, the newest retrieval
considered, which bounds the next run's work; without it every issuance is rescored once."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from . import evaluation as E
from . import features as F
from . import index
from . import sources
from .issue import OUT as FORECASTS
from .issue import publish
from .targets import belgium_target

OUT = sources.ROOT / "data/scores"
FINAL_AFTER = pd.Timedelta(days=7)
LABEL = "measured and upscaled Belgium Total (Elia), MWh per hour"
BENCHMARK = {"11h": "elia_da11_mw", "18h": "elia_da18_mw"}
ACTUALS = ["measured_mw", "capacity_mw"]
REQUIRED = ("forecast", "persistence", "physics")
PREDICTORS = (*REQUIRED, "elia")


def day_bounds(exp: F.Experiment, target) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The local target day as a UTC interval [start, end)."""
    d = pd.Timestamp(target)
    return tuple(t.tz_localize(exp.tz).tz_convert("UTC") for t in (d, d + pd.Timedelta(days=1)))


def _requests(m: dict, exp: F.Experiment, target) -> bool:
    """The retrieval asked for the whole local target day: unbounded, or its UTC dates [start, end) hold it."""
    if "start" not in m:
        return True
    a, b = day_bounds(exp, target)
    return pd.Timestamp(m["start"], tz="UTC") <= a and b <= pd.Timestamp(m["end"], tz="UTC")


def final_retrieval(exp: F.Experiment, metas: list[dict], target) -> dict | None:
    due = day_bounds(exp, target)[1] + FINAL_AFTER
    return min((m for m in metas if m["dataset"] == "ods209" and F._ts(m["retrieved_at"]) >= due
                and _requests(m, exp, target)), key=lambda m: F._ts(m["retrieved_at"]), default=None)


def _versions(out: Path, stem: str) -> list[Path]:
    return sorted(out.glob(f"{stem}.v*.json"), key=lambda p: int(p.name.rsplit(".v", 1)[1][:-5]))


def _num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def _sources(qh: pd.DataFrame, exp: F.Experiment, rec: dict, by_id: dict) -> list[dict]:
    """Every retrieval a used field of the target day came from, with the fields it supplied."""
    a, b = day_bounds(exp, rec["target_date"])
    day = qh[(qh["region"] == "Belgium") & (qh["datetime"] >= a) & (qh["datetime"] < b)]
    fields = {}
    for f in (*ACTUALS, BENCHMARK[rec["kind"]]):
        for i in day[f"{f}_from"].dropna().unique():
            fields.setdefault(int(i), []).append(f)
    out = [{"dataset": (m := by_id[i])["dataset"], "sidecar": Path(m["sidecar"]).name, "sha256": m["sha256"],
            "sidecar_sha256": m["sidecar_sha256"], "retrieved_at": m["retrieved_at"], "fields": f}
           for i, f in fields.items()]
    return sorted(out, key=lambda r: (F._ts(r["retrieved_at"]), r["sidecar"]))


def score_record(exp: F.Experiment, rec: dict, rec_path: Path, qh: pd.DataFrame, status: str, by_id: dict,
                 now: pd.Timestamp) -> dict:
    """One score version's content from a forecast record and resolved quarter-hours (with provenance)."""
    target, kind = rec["target_date"], rec["kind"]
    t = belgium_target(qh, target, target, exp.points).set_index("hour_utc")
    h = pd.DataFrame(rec["hours"])
    h["hour_utc"] = pd.to_datetime(h["target_hour_utc"], utc=True)
    t = t.reindex(h["hour_utc"])
    h["actual_mwh"] = t["measured_upscaled_mwh"].to_numpy()
    h["n_quarter_hours"] = t["n_quarter_hours"].fillna(0).astype(int).to_numpy()
    h["exclusion"] = t["exclusion"].fillna("").to_numpy()
    h["elia_mwh"] = t[BENCHMARK[kind] + "h"].to_numpy()
    h["daylight"] = E.daylight(pd.DatetimeIndex(h["hour_utc"]), exp)
    pred = {p: pd.to_numeric(h[f"{p}_mwh"], errors="coerce").to_numpy(float) for p in PREDICTORS}
    eligible = t["eligible"].fillna(False).to_numpy(bool)
    h["scored"] = eligible & np.logical_and.reduce([np.isfinite(pred[p]) for p in REQUIRED])
    day_complete = bool(t["day_complete"].fillna(False).all())
    frame = pd.DataFrame({"kind": kind, "target_date": target, "actual": h["actual_mwh"], "daylight": h["daylight"],
                          "day_hours": len(h), "day_complete": day_complete, **pred})
    metrics = {}
    for p in PREDICTORS:
        rows = frame.assign(scored=h["scored"].to_numpy() & np.isfinite(pred[p]))
        metrics[p] = {k: (v if isinstance(v, int) else _num(v)) for k, v in E.metrics(rows, p).items()}
    hours = [{"target_hour_utc": r.target_hour_utc, "local_hour": int(r.local_hour), "lead_hours": r.lead_hours,
              "actual_mwh": _num(r.actual_mwh), "n_quarter_hours": int(r.n_quarter_hours), "exclusion": r.exclusion,
              "daylight": bool(r.daylight), "scored": bool(r.scored), "forecast_mwh": _num(r.forecast_mwh),
              "persistence_mwh": _num(r.persistence_mwh), "physics_mwh": _num(r.physics_mwh), "elia_mwh": _num(r.elia_mwh)}
             for r in h.itertuples()]
    content = [{k: x[k] for k in ("target_hour_utc", "actual_mwh", "n_quarter_hours", "exclusion", "elia_mwh")}
               for x in hours]
    return {
        "experiment": rec["experiment"], "kind": kind, "target_date": target, "status": status,
        "content_sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
        "scored_at": now.isoformat(),
        "forecast": {"record": rec_path.name, "sha256": hashlib.sha256(rec_path.read_bytes()).hexdigest(),
                     "attempt": rec["attempt"], "issuance_utc": rec["issuance_utc"],
                     "model_sha256": rec["model"]["model_sha256"], "weather_availability": rec["inputs"]["weather_availability"]},
        "actuals": {"label": LABEL, "retrievals": _sources(qh, exp, rec, by_id)},
        "coverage": {"hours": len(h), "observed": int((h["n_quarter_hours"] == 4).sum()), "scored": int(h["scored"].sum()),
                     "unscored_hours_utc": [x["target_hour_utc"] for x in hours if not x["scored"]],
                     "missing_predictions": {p: h.loc[eligible & ~np.isfinite(pred[p]), "target_hour_utc"].tolist()
                                             for p in REQUIRED},
                     "day_complete": day_complete},
        "metrics": metrics,
        "hours": hours,
    }


def _finalized(act: pd.DataFrame, cur: pd.DataFrame) -> pd.DataFrame:
    """Actuals from the final retrieval, the benchmark fields as resolved over all retrievals."""
    bench = [*BENCHMARK.values()]
    bench += [f"{f}_from" for f in bench]
    return act.drop(columns=bench).merge(cur[["datetime", "region", *bench]], on=["datetime", "region"], how="outer")


def score(name: str = "belgium", now=None, store: Path = sources.STORE, forecasts: Path = FORECASTS,
          out: Path = OUT) -> list[Path]:
    """Write the new score versions and the summary; return the new version paths."""
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    out = out / name
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "summary.json"
    through = json.loads(summary.read_text()).get("elia_through") if summary.exists() else None
    metas = [m for m in sources.snapshots("elia", store)
             if m["dataset"] in ("ods209", "ods210") and F._ts(m["retrieved_at"]) <= now]
    records = [(p, rec) for p in sorted((forecasts / name).glob("*_??h.json"))
               if (rec := json.loads(p.read_text()))["status"] == "issued" and day_bounds(exp, rec["target_date"])[0] <= now]
    written = []
    if records and metas:
        con = index.connect(index.path_for(metas))
        new = [m for m in metas if through is None or F._ts(m["retrieved_at"]) > F._ts(through)]
        spans = index.spans(con, index.ingest(con, new))
        touched = lambda d: any(lo < d[1] and hi >= d[0] for lo, hi in spans)
        todo = [(p, rec) for p, rec in records
                if not _versions(out, p.stem) or touched(day_bounds(exp, rec["target_date"]))]
        if todo:
            since = min(day_bounds(exp, rec["target_date"])[0] for _, rec in todo)
            scope = index.reaching(con, metas, since)
            by_id = dict(zip(index.ingest(con, scope), scope))
            current = index.resolved(con, list(by_id), None, since, provenance=True)
            for p, rec in todo:
                latest = _versions(out, p.stem)
                prev = json.loads(latest[-1].read_text()) if latest else None
                finalized = prev is not None and prev["status"] in ("final", "revised")
                steps = []
                if not finalized and (fin := final_retrieval(exp, scope, rec["target_date"])):
                    fid = next(i for i, m in by_id.items() if m is fin)
                    steps.append(("final", _finalized(index.resolved(con, [fid], None, since, provenance=True), current)))
                steps.append(("revised" if finalized or steps else "provisional", current))
                for status, qh in steps:
                    v = score_record(exp, rec, p, qh, status, by_id, now)
                    if prev and status != "final" and prev["content_sha256"] == v["content_sha256"]:
                        continue
                    n = prev["version"] + 1 if prev else 1
                    v = {"version": n, "previous": {"version": prev["version"], "content_sha256": prev["content_sha256"]}
                         if prev else None, **v}
                    path = out / f"{p.stem}.v{n}.json"
                    publish(path, json.dumps(v, indent=1, allow_nan=False) + "\n")
                    written.append(path)
                    prev = v
    newest = max((m["retrieved_at"] for m in metas), key=F._ts, default=through)
    _summary(out, now, newest)
    return written


def _summary(out: Path, now: pd.Timestamp, through: str | None) -> None:
    """Pooled metrics by kind and weather availability, over the latest version of each issuance (`all`) and over
    its final version (`final`); replaced atomically."""
    latest, final = {}, {}
    for p in out.glob("*.v*.json"):
        v = json.loads(p.read_text())
        key = (v["target_date"], v["kind"])
        if key not in latest or v["version"] > latest[key]["version"]:
            latest[key] = v
        if v["status"] == "final":
            final[key] = v
    pools = {}
    for scope, chosen in (("all", latest), ("final", final)):
        groups = {}
        for key, v in sorted(chosen.items()):
            groups.setdefault(f"{v['kind']}_{v['forecast']['weather_availability']}_{scope}", []).append(v)
        for name, vs in groups.items():
            rows = pd.DataFrame([{**h, "kind": v["kind"], "target_date": v["target_date"], "day_hours": len(v["hours"]),
                                  "day_complete": v["coverage"]["day_complete"]} for v in vs for h in v["hours"]])
            m = {}
            for pr in PREDICTORS:
                r = rows.assign(actual=rows["actual_mwh"], **{pr: pd.to_numeric(rows[f"{pr}_mwh"], errors="coerce")})
                r["scored"] = r["scored"] & np.isfinite(r[pr].astype(float))
                m[pr] = {k: (x if isinstance(x, int) else _num(x)) for k, x in E.metrics(r, pr).items()}
            pools[name] = {"issuances": len(vs), "scored_hours": int(rows["scored"].sum()), "metrics": m}
    text = json.dumps({"generated_at": now.isoformat(), "label": LABEL, "elia_through": through, "pools": pools},
                      indent=1, allow_nan=False) + "\n"
    tmp = out / f".summary.json.{uuid.uuid4().hex}.tmp"
    tmp.write_text(text)
    os.replace(tmp, out / "summary.json")
