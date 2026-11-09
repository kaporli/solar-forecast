"""`solar-data history belgium`: the public issuance history (#7), contract in docs/history.md.

data/feed/<experiment>-history.json, replaced atomically, written with the feed after every issue and score run.
One entry per issuance, oldest first:

- `issued`: the record's hours, each joined to the latest score version of that issuance when there is one (actual
  production, Elia's day-ahead forecast, whether it was scored), with that version's status, coverage and metrics.
- `failed` or `late`: the issuance left a failure record and no forecast (late wins, as in the feed).

Only public values: no paths, hosts, attempt ids, snapshot names or errors. A null value is missing, never zero."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import evaluation as E
from . import features as F
from .feed import ATTRIBUTION, OUT, TARGET, _failure, publish
from .issue import OUT as FORECASTS
from .score import OUT as SCORES, _versions

SCHEMA = "kaporli.solar-forecast.history"
VERSION = 1


def _score(scores: Path, stem: str) -> dict | None:
    vs = _versions(scores, stem)
    return json.loads(vs[-1].read_text()) if vs else None


def _issued(exp: F.Experiment, rec: dict, pub: dict | None, sc: dict | None) -> dict:
    by_hour = {h["target_hour_utc"]: h for h in sc["hours"]} if sc else {}
    day = pd.DatetimeIndex([pd.Timestamp(h["target_hour_utc"]) for h in rec["hours"]])
    hours = []
    for h, lit in zip(rec["hours"], E.daylight(day, exp)):
        s = by_hour.get(h["target_hour_utc"], {})
        start = pd.Timestamp(h["target_hour_utc"])
        hours.append({"start_utc": start.isoformat(), "end_utc": (start + pd.Timedelta(hours=1)).isoformat(),
                      "local_hour": h["local_hour"], "daylight": bool(lit),
                      "forecast_mwh": h["forecast_mwh"], "persistence_mwh": h["persistence_mwh"],
                      "physics_mwh": h["physics_mwh"], "elia_mwh": s.get("elia_mwh"),
                      "actual_mwh": s.get("actual_mwh"), "scored": s.get("scored", False)})
    return {
        "kind": rec["kind"], "target_date": rec["target_date"], "status": "issued",
        "issuance_utc": rec["issuance_utc"], "published_at": pub["published_at"] if pub else None,
        "weather": {"run_utc": rec["inputs"]["weather_run_utc"], "availability": rec["inputs"]["weather_availability"]},
        "capacity_mw": rec["inputs"]["capacity_mw"],
        "model": {k: rec["model"][k] for k in ("candidate", "model_sha256", "feature_version")},
        "code_commit": rec["code_commit"],
        "score": sc and {"status": sc["status"], "version": sc["version"], "scored_at": sc["scored_at"],
                         "coverage": {k: sc["coverage"][k] for k in ("hours", "observed", "scored", "day_complete")},
                         "metrics": sc["metrics"]},
        "hours": hours,
    }


def build(name: str = "belgium", now=None, forecasts: Path = FORECASTS, scores: Path = SCORES) -> dict:
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    d = forecasts / name
    entries = {}
    for p in d.glob("*_??h.json"):
        rec = json.loads(p.read_text())
        if rec["status"] != "issued":
            continue
        pub = p.with_name(f"{p.stem}.published.json")
        entries[p.stem] = _issued(exp, rec, json.loads(pub.read_text()) if pub.exists() else None,
                                  _score(scores / name, p.stem))
    for p in d.glob("*_??h.failed-*.json"):
        stem = p.name.split(".", 1)[0]
        if stem not in entries:
            target, kind = stem.split("_")
            entries[stem] = {"kind": kind, "target_date": target,
                             "status": _failure(forecasts, name, pd.Timestamp(target), kind),
                             "issuance_utc": F.issuance_time(exp, target, kind).isoformat(),
                             "published_at": None, "weather": None, "capacity_mw": None, "model": None,
                             "code_commit": None, "score": None, "hours": []}
    # the whole history in one file, about 9 kB per issuance (6.5 MB a year); split by month if that grows too large
    return {"schema": SCHEMA, "version": VERSION, "generated_at": now.isoformat(), "experiment": name,
            "target": TARGET, "attribution": ATTRIBUTION,
            "issuances": sorted(entries.values(), key=lambda e: e["issuance_utc"])}


def write(name: str = "belgium", now=None, forecasts: Path = FORECASTS, scores: Path = SCORES, out: Path = OUT) -> Path:
    return publish(out / f"{name}-history.json", build(name, now, forecasts, scores))
