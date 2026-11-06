"""`solar-data score belgium`: score issued forecasts against Elia actuals (#6, policy in docs/deployment.md).

For each issued record (`issue`) whose target day has started and that has no final score:

- **Provisional:** actuals resolved from every Elia retrieval stored so far (ods209 before ods210, newest non-null,
  `index.resolved`). An hour counts once all four quarter-hours are present (`targets.belgium_target`).
- **Final:** actuals from the first stored ods209 retrieval made at least `FINAL_AFTER` after the target day ends
  in Europe/Brussels and requesting that day, and from that retrieval alone. A final score is not rescored.

A score version is written only when its content (status and per-hour actuals and benchmark) differs from the
latest version: data/scores/<experiment>/<target>_<kind>.v<N>.json, published exclusively (`issue.publish`), so
versions are immutable and earlier ones stay. Each references the forecast record and the actuals' retrievals.
Missing hours stay missing and are counted in `coverage`; daily totals need a complete day (`evaluation.metrics`).
The forecast, persistence and physics are scored on the same hours; the Elia benchmark of the kind
(`dayahead11hforecast` for 11h, `dayaheadforecast` for 18h) on those of them where Elia published one.

`summary.json` pools the latest version of every scored issuance by kind, for all and for final scores only."""
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
BENCHMARK = {"11h": "elia_da11_mwh", "18h": "elia_da18_mwh"}
PREDICTORS = ("forecast", "persistence", "physics", "elia")


def day_end(exp: F.Experiment, target) -> pd.Timestamp:
    return (pd.Timestamp(target) + pd.Timedelta(days=1)).tz_localize(exp.tz).tz_convert("UTC")


def _requests(m: dict, target) -> bool:
    """The retrieval asked for the target day: unbounded, or [start, end) holds it."""
    d = str(pd.Timestamp(target).date())
    return "start" not in m or m["start"] <= d < m["end"]


def final_retrieval(exp: F.Experiment, metas: list[dict], target) -> dict | None:
    due = day_end(exp, target) + FINAL_AFTER
    return next((m for m in metas if m["dataset"] == "ods209" and F._ts(m["retrieved_at"]) >= due
                 and _requests(m, target)), None)


def _ref(m: dict) -> dict:
    return {"dataset": m["dataset"], "sidecar": Path(m["sidecar"]).name, "sha256": m["sha256"],
            "retrieved_at": m["retrieved_at"]}


def _versions(out: Path, stem: str) -> list[Path]:
    return sorted(out.glob(f"{stem}.v*.json"), key=lambda p: int(p.name.rsplit(".v", 1)[1][:-5]))


def _num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def score_record(exp: F.Experiment, rec: dict, rec_path: Path, qh: pd.DataFrame, status: str, refs: list[dict],
                 now: pd.Timestamp) -> dict:
    """One score version's content from a forecast record and resolved quarter-hours."""
    target, kind = rec["target_date"], rec["kind"]
    t = belgium_target(qh, target, target, exp.points).set_index("hour_utc")
    h = pd.DataFrame(rec["hours"])
    h["hour_utc"] = pd.to_datetime(h["target_hour_utc"], utc=True)
    t = t.reindex(h["hour_utc"])
    h["actual_mwh"] = t["measured_upscaled_mwh"].to_numpy()
    h["n_quarter_hours"] = t["n_quarter_hours"].fillna(0).astype(int).to_numpy()
    h["exclusion"] = t["exclusion"].fillna("").to_numpy()
    h["elia_mwh"] = t[BENCHMARK[kind]].to_numpy()
    h["daylight"] = E.daylight(pd.DatetimeIndex(h["hour_utc"]), exp)
    pred = {p: pd.to_numeric(h[f"{p}_mwh"], errors="coerce").to_numpy(float) for p in PREDICTORS}
    h["scored"] = t["eligible"].fillna(False).to_numpy(bool) & np.isfinite(pred["forecast"])
    frame = pd.DataFrame({"kind": kind, "target_date": target, "actual": h["actual_mwh"], "daylight": h["daylight"],
                          "day_hours": len(h), "day_complete": bool(t["day_complete"].fillna(False).all()), **pred})
    metrics = {}
    for p in PREDICTORS:
        rows = frame.assign(scored=h["scored"].to_numpy() & np.isfinite(pred[p]))
        metrics[p] = {k: (v if isinstance(v, int) else _num(v)) for k, v in E.metrics(rows, p).items()}
    hours = [{"target_hour_utc": r.target_hour_utc, "local_hour": int(r.local_hour), "lead_hours": r.lead_hours,
              "actual_mwh": _num(r.actual_mwh), "n_quarter_hours": int(r.n_quarter_hours), "exclusion": r.exclusion,
              "daylight": bool(r.daylight), "scored": bool(r.scored), "forecast_mwh": r.forecast_mwh,
              "persistence_mwh": r.persistence_mwh, "physics_mwh": r.physics_mwh, "elia_mwh": _num(r.elia_mwh)}
             for r in h.itertuples()]
    content = {"status": status, "hours": [{k: x[k] for k in ("target_hour_utc", "actual_mwh", "n_quarter_hours",
                                                                "exclusion", "elia_mwh")} for x in hours]}
    return {
        "experiment": rec["experiment"], "kind": kind, "target_date": target, "status": status,
        "content_sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
        "scored_at": now.isoformat(),
        "forecast": {"record": rec_path.name, "sha256": hashlib.sha256(rec_path.read_bytes()).hexdigest(),
                     "attempt": rec["attempt"], "issuance_utc": rec["issuance_utc"],
                     "model_sha256": rec["model"]["model_sha256"], "weather_availability": rec["inputs"]["weather_availability"]},
        "actuals": {"label": LABEL, "retrievals": refs},
        "coverage": {"hours": len(h), "observed": int((h["n_quarter_hours"] == 4).sum()), "scored": int(h["scored"].sum()),
                     "unscored_hours_utc": [x["target_hour_utc"] for x in hours if not x["scored"]],
                     "day_complete": bool(t["day_complete"].fillna(False).all())},
        "metrics": metrics,
        "hours": hours,
    }


def score(name: str = "belgium", now=None, store: Path = sources.STORE, forecasts: Path = FORECASTS,
          out: Path = OUT) -> list[Path]:
    """Write the new score versions and the summary; return the new version paths."""
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    out = out / name
    out.mkdir(parents=True, exist_ok=True)
    todo = []
    for p in sorted((forecasts / name).glob("*_??h.json")):
        rec = json.loads(p.read_text())
        start = pd.Timestamp(rec["target_date"]).tz_localize(exp.tz).tz_convert("UTC")
        latest = _versions(out, p.stem)
        if rec["status"] == "issued" and start <= now and not (latest and json.loads(latest[-1].read_text())["status"] == "final"):
            todo.append((p, rec, latest))
    written = []
    if todo:
        metas = [m for m in sources.snapshots("elia", store)
                 if m["dataset"] in ("ods209", "ods210") and F._ts(m["retrieved_at"]) <= now]
        since = min(pd.Timestamp(r["target_date"]) for _, r, _ in todo).tz_localize(exp.tz).tz_convert("UTC")
        con = index.connect(index.path_for(metas))
        metas = index.reaching(con, metas, since)
        ids = index.ingest(con, metas)
        provisional = index.resolved(con, ids, None, since)
        for p, rec, latest in todo:
            fin = final_retrieval(exp, metas, rec["target_date"])
            if fin:
                qh, status, refs = index.resolved(con, [ids[metas.index(fin)]], None, since), "final", [_ref(fin)]
            else:
                start = pd.Timestamp(rec["target_date"]).tz_localize(exp.tz).tz_convert("UTC")
                day = provisional[(provisional["datetime"] >= start) & (provisional["datetime"] < day_end(exp, rec["target_date"]))]
                used = set(day["retrieved_at"])
                qh, status = provisional, "provisional"
                refs = [_ref(m) for m in metas if F._ts(m["retrieved_at"]) in used]
            v = score_record(exp, rec, p, qh, status, refs, now)
            prev = json.loads(latest[-1].read_text()) if latest else None
            if prev and prev["content_sha256"] == v["content_sha256"]:
                continue
            n = len(latest) + 1
            v = {"version": n, "previous": {"version": prev["version"], "content_sha256": prev["content_sha256"]} if prev else None, **v}
            path = out / f"{p.stem}.v{n}.json"
            publish(path, json.dumps(v, indent=1, allow_nan=False) + "\n")
            written.append(path)
    _summary(out, now)
    return written


def _summary(out: Path, now: pd.Timestamp) -> None:
    """Pooled metrics over the latest version of each scored issuance, by kind; replaced atomically."""
    latest = {}
    for p in out.glob("*.v*.json"):
        v = json.loads(p.read_text())
        key = (v["target_date"], v["kind"])
        if key not in latest or v["version"] > latest[key]["version"]:
            latest[key] = v
    pools = {}
    for kind in sorted({k for _, k in latest}):
        for scope in ("all", "final"):
            vs = [v for (_, k), v in sorted(latest.items()) if k == kind and (scope == "all" or v["status"] == "final")]
            rows = pd.DataFrame([{**h, "kind": kind, "target_date": v["target_date"], "day_hours": len(v["hours"]),
                                  "day_complete": v["coverage"]["day_complete"]} for v in vs for h in v["hours"]])
            m = {}
            for pr in PREDICTORS:
                if rows.empty:
                    break
                r = rows.assign(actual=rows["actual_mwh"], **{pr: pd.to_numeric(rows[f"{pr}_mwh"], errors="coerce")})
                r["scored"] = r["scored"] & np.isfinite(r[pr].astype(float))
                m[pr] = {k: (x if isinstance(x, int) else _num(x)) for k, x in E.metrics(r, pr).items()}
            pools[f"{kind}_{scope}"] = {"issuances": len(vs), "final": sum(v["status"] == "final" for v in vs),
                                        "scored_hours": int(rows["scored"].sum()) if len(rows) else 0, "metrics": m}
    text = json.dumps({"generated_at": now.isoformat(), "label": LABEL, "pools": pools}, indent=1, allow_nan=False) + "\n"
    tmp = out / f".summary.json.{uuid.uuid4().hex}.tmp"
    tmp.write_text(text)
    os.replace(tmp, out / "summary.json")
