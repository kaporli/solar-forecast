"""`solar-data issue belgium --kind 11h|18h`: one live issuance (#6), run just after the issuance time.

It uses only snapshots retrieved by the issuance time (`features_for_issuance(live=True)`): recent Elia retrievals,
queried from 12 days before the target day (persistence needs D-2 to D-8, capacity the latest value before the
cutoff), and the prescribed weather run, or the latest earlier run labelled `fallback`. The packaged model
(`artifact`) predicts; persistence and physics are recorded beside it for scoring.

Each issuance is identified by (experiment, kind, target day) and written once to
data/forecasts/<experiment>/<target>_<kind>.json, opened exclusively: a retry cannot replace an issued forecast.
A failed attempt writes data/forecasts/<experiment>/<target>_<kind>.failed-<UTC>.json and no forecast; there is
no fallback model. Records hold the code and model identity and a checksum of every input snapshot."""
from __future__ import annotations

import hashlib
import json
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import artifact as A
from . import baselines as B
from . import features as F
from . import sources

ROOT = sources.ROOT
OUT = ROOT / "data/forecasts"
LOOKBACK = pd.Timedelta(days=3)  # input snapshots retrieved up to this long before the issuance


def record_path(out: Path, name: str, target, kind: str) -> Path:
    return out / name / f"{target}_{kind}.json"


def _ref(m: dict) -> dict:
    return {"source": m["source"], "name": m["name"], "sidecar": Path(m["sidecar"]).name, "sha256": m["sha256"],
            "sidecar_sha256": m["sidecar_sha256"], "retrieved_at": m["retrieved_at"]}


def _iso(t) -> str | None:
    return None if t is None or pd.isna(t) else pd.Timestamp(t).isoformat()


def _num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def issue(name: str, kind: str, target=None, now=None, store: Path = sources.STORE, out: Path = OUT) -> Path:
    """Issue one forecast and return its record path. Refuses before the issuance time and when it was already
    issued; on failure writes a failure record and re-raises."""
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    target = pd.Timestamp(target).date() if target is not None else (now.tz_convert(exp.tz) + pd.Timedelta(days=1)).date()
    issuance = F.issuance_time(exp, target, kind)
    path = record_path(out, name, target, kind)
    if now < issuance:
        raise SystemExit(f"{target} {kind}: issuance time {issuance} has not passed")
    if path.exists():
        raise SystemExit(f"{path.name} exists: already issued")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"experiment": name, "kind": kind, "target_date": str(target), "issuance_utc": issuance.isoformat(),
            "computed_at": datetime.now(timezone.utc).isoformat(), "code_commit": _git("rev-parse", "HEAD"),
            "code_dirty": bool(_git("status", "--porcelain", "src", "config", "artifacts"))}
    try:
        recent = lambda m: issuance - LOOKBACK <= F._ts(m["retrieved_at"]) <= issuance
        elia = [m for m in sources.snapshots("elia", store) if m["dataset"] in ("ods209", "ods210") and recent(m)]
        weather = [m for m in sources.snapshots(f"weather-{name}", store) if recent(m)]
        if not elia:
            raise ValueError("no Elia snapshot retrieved in the three days before the issuance")
        inputs = F.BelgiumInputs(elia, weather, exp, since=pd.Timestamp(target, tz="UTC") - pd.Timedelta(days=12))
        rows = F.features_for_issuance(exp, inputs, target, kind, live=True)
        model, man = A.load(A.directory(name))
        forecast = A.predict(model, man, rows)
        cap = rows["capacity"].to_numpy(float)
        persistence = np.clip(B.Persistence().predict(rows), 0, cap)
        physics = np.clip(B.BelgiumPhysics(inputs, live=True).predict(rows[list(F.CONTRACT)]), 0, cap)
    except Exception as e:
        fail = path.with_name(f"{path.stem}.failed-{base['computed_at'].replace(':', '').replace('-', '')[:15]}Z.json")
        fail.write_text(json.dumps(base | {"status": "failed", "error": f"{type(e).__name__}: {e}",
                                           "traceback": traceback.format_exc(limit=5)}, indent=1) + "\n")
        raise
    first = rows.iloc[0]
    from .cli import content_hash

    rec = base | {
        "status": "issued",
        "model": {"candidate": man["candidate"], "model_sha256": man["model_sha256"],
                  "manifest_sha256": hashlib.sha256((A.directory(name) / "manifest.json").read_bytes()).hexdigest(),
                  "implementation_sha256": man["implementation_sha256"], "feature_version": F.FEATURE_VERSION},
        "inputs": {"weather_run_utc": _iso(first["weather_run_utc"]), "weather_availability": first["weather_availability"],
                   "weather_retrieved_at": _iso(first["weather_retrieved_at"]), "actuals_cutoff_utc": _iso(first["actuals_cutoff_utc"]),
                   "capacity_mw": _num(first["capacity"]), "capacity_complete": bool(first["capacity_complete"]),
                   "snapshots": [_ref(m) for m in elia + weather]},
        "features_sha256": content_hash(rows),
        "unit": "MWh per hour, measured and upscaled Belgium Total (Elia)",
        "hours": [{"target_hour_utc": _iso(r["target_hour_utc"]), "local_hour": int(r["local_hour"]),
                   "lead_hours": _num(r["lead_hours"]), "forecast_mwh": _num(f), "persistence_mwh": _num(p),
                   "physics_mwh": _num(q)}
                  for (_, r), f, p, q in zip(rows.iterrows(), forecast, persistence, physics)],
    }
    with open(path, "x") as fh:  # exclusive: an issued forecast is never replaced
        fh.write(json.dumps(rec, indent=1) + "\n")
    return path


def _git(*a) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
