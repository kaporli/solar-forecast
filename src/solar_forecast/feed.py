"""`solar-data feed belgium`: the public JSON feed (#6), contract in docs/feed.md.

One file per experiment, data/feed/<experiment>.json, replaced atomically (written and flushed to a temporary
file, then renamed). It holds, for each issuance kind, the forecast a visitor should see now and its state:

- `fresh`: the latest due issuance, made with its prescribed weather run.
- `fallback`: the same, made with an earlier weather run (`weather_availability = fallback`).
- `stale`: the latest due issuance failed, was late or is missing past its deadline; the previous forecast is kept
  while its target day lasts.
- `unavailable`: no forecast for a target day that has not ended.

Until an issuance's deadline (`issue.LATE`) has passed without a failure, the previous forecast keeps its state.
A saved feed ages without regeneration by two rules (`age`, docs/feed.md): past `valid_until` a fresh or fallback
entry is stale; from its forecast's `end_utc` an entry is unavailable. Only public values are written: no paths,
hosts or input snapshot names."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pandas as pd

from . import features as F
from . import sources
from .issue import LATE, OUT as FORECASTS, _staged, _sync_dir, record_path

OUT = sources.ROOT / "data/feed"
SCHEMA = "kaporli.solar-forecast.feed"
VERSION = 1
KINDS = ("11h", "18h")
TARGET = {"label": "measured and upscaled Belgium Total (Elia)", "area": "Belgium", "unit": "MWh per hour",
          "interval": "hourly, [start_utc, end_utc)", "timezone": "Europe/Brussels"}
ATTRIBUTION = [
    {"source": "Elia Open Data (Elia Transmission Belgium SA)", "license": "CC BY 4.0", "use": "production and capacity"},
    {"source": "Open-Meteo.com, ECMWF IFS HRES", "license": "CC BY 4.0", "use": "weather forecasts"},
]


def _day(target) -> pd.Timestamp:
    return pd.Timestamp(pd.Timestamp(target).date())


def due_target(exp: F.Experiment, kind: str, now: pd.Timestamp) -> pd.Timestamp:
    """The target day of the latest issuance of `kind` at or before `now`."""
    tomorrow = _day(now.tz_convert(exp.tz)) + pd.Timedelta(days=1)
    return tomorrow if F.issuance_time(exp, tomorrow, kind) <= now else tomorrow - pd.Timedelta(days=1)


def _issued(forecasts: Path, name: str, target, kind: str) -> tuple[dict, dict | None] | None:
    p = record_path(forecasts, name, target.date(), kind)
    if not p.exists():
        return None
    rec = json.loads(p.read_text())
    pub = p.with_name(f"{p.stem}.published.json")
    return (rec, json.loads(pub.read_text()) if pub.exists() else None) if rec["status"] == "issued" else None


def _failure(forecasts: Path, name: str, target, kind: str) -> str | None:
    """`late` or `failed` when an attempt for this issuance left a failure record (late wins)."""
    got = {json.loads(p.read_text())["status"]
           for p in (forecasts / name).glob(f"{target.date()}_{kind}.failed-*.json")}
    return "late" if "late" in got else "failed" if got else None


def _hours(a, b) -> float:
    return (pd.Timestamp(b) - pd.Timestamp(a)) / pd.Timedelta(hours=1)


def _forecast(rec: dict, pub: dict | None) -> dict:
    run = rec["inputs"]["weather_run_utc"]
    last = pd.Timestamp(rec["hours"][-1]["target_hour_utc"]) + pd.Timedelta(hours=1)
    return {
        "target_date": rec["target_date"], "issuance_utc": rec["issuance_utc"],
        "published_at": pub["published_at"] if pub else None, "computed_at": rec["computed_at"],
        "weather": {"run_utc": rec["inputs"]["weather_run_utc"], "availability": rec["inputs"]["weather_availability"],
                    "retrieved_at": rec["inputs"]["weather_retrieved_at"]},
        "actuals_cutoff_utc": rec["inputs"]["actuals_cutoff_utc"], "capacity_mw": rec["inputs"]["capacity_mw"],
        "model": {"candidate": rec["model"]["candidate"], "model_sha256": rec["model"]["model_sha256"],
                  "feature_version": rec["model"]["feature_version"]},
        "code_commit": rec["code_commit"], "end_utc": last.isoformat(),
        "hours": [{"start_utc": h["target_hour_utc"],
                   "end_utc": (pd.Timestamp(h["target_hour_utc"]) + pd.Timedelta(hours=1)).isoformat(),
                   "local_hour": h["local_hour"],
                   "hours_after_issuance": _hours(rec["issuance_utc"], h["target_hour_utc"]),
                   "weather_lead_hours": _hours(run, h["target_hour_utc"]) if run else None,
                   "forecast_mwh": h["forecast_mwh"], "persistence_mwh": h["persistence_mwh"],
                   "physics_mwh": h["physics_mwh"]} for h in rec["hours"]],
    }


def entry(exp: F.Experiment, name: str, kind: str, now: pd.Timestamp, forecasts: Path) -> dict:
    target = due_target(exp, kind, now)
    due = F.issuance_time(exp, target, kind)
    deadline = due + LATE
    base = {"expected_issuance_utc": due.isoformat(), "deadline_utc": deadline.isoformat()}
    state = lambda rec: "fallback" if rec["inputs"]["weather_availability"] == "fallback" else "fresh"
    if got := _issued(forecasts, name, target, kind):
        nxt = F.issuance_time(exp, target + pd.Timedelta(days=1), kind) + LATE
        return {"state": state(got[0]), "reason": None, **base, "valid_until": nxt.isoformat(), "forecast": _forecast(*got)}
    failure = _failure(forecasts, name, target, kind)
    prev = _issued(forecasts, name, target - pd.Timedelta(days=1), kind)
    if prev and failure is None and now <= deadline:  # the new one is not due yet: the previous one stands
        return {"state": state(prev[0]), "reason": None, **base, "valid_until": deadline.isoformat(),
                "forecast": _forecast(*prev)}
    reason = failure or "missing"
    if prev and now < pd.Timestamp(_forecast(*prev)["end_utc"]):
        return {"state": "stale", "reason": reason, **base, "valid_until": None, "forecast": _forecast(*prev)}
    return {"state": "unavailable", "reason": reason, **base, "valid_until": None, "forecast": None}


def _latest(kinds: dict) -> str | None:
    shown = [k for k in KINDS if kinds[k]["forecast"]]
    return max(shown, key=lambda k: kinds[k]["forecast"]["issuance_utc"], default=None)


def age(f: dict, now) -> dict:
    """A saved feed as a consumer must read it at `now` without regeneration (docs/feed.md, "Reading a saved feed")."""
    now, f = pd.Timestamp(now), copy.deepcopy(f)
    for e in f["issuances"].values():
        if e["forecast"] and now >= pd.Timestamp(e["forecast"]["end_utc"]):
            e.update(state="unavailable", reason=e["reason"] or "expired", valid_until=None, forecast=None)
        elif e["state"] in ("fresh", "fallback") and now > pd.Timestamp(e["valid_until"]):
            e.update(state="stale", reason="expired", valid_until=None)
    f["latest"] = _latest(f["issuances"])
    return f


def build(name: str = "belgium", now=None, forecasts: Path = FORECASTS) -> dict:
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    kinds = {k: entry(exp, name, k, now, forecasts) for k in KINDS}
    return {"schema": SCHEMA, "version": VERSION, "generated_at": now.isoformat(), "experiment": name,
            "target": TARGET, "latest": _latest(kinds), "issuances": kinds, "attribution": ATTRIBUTION}


def publish(path: Path, obj: dict) -> Path:
    """Replace `path` with `obj` as JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _staged(path, json.dumps(obj, indent=1, allow_nan=False) + "\n")
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(path.parent)
    return path


def write(name: str = "belgium", now=None, forecasts: Path = FORECASTS, out: Path = OUT) -> Path:
    """Build the feed and replace data/feed/<name>.json atomically."""
    return publish(out / f"{name}.json", build(name, now, forecasts))
