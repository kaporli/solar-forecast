"""`solar-data issue belgium --kind 11h|18h`: one live issuance (#6), run just after the issuance time.

It uses only snapshots retrieved by the issuance time (`features_for_issuance(live=True)`): every Elia retrieval
that can hold a quarter-hour from 12 days before the target day (persistence needs D-2 to D-8, capacity the latest
value before the cutoff; `index.reaching`), so the window resolves as over the whole history, and the prescribed
weather run, or the latest earlier run labelled `fallback`, counting only complete responses
(`sources.run_complete`). The packaged model (`artifact`) predicts; persistence and physics are recorded beside it.

Each issuance is identified by (experiment, kind, target day) and published once to
data/forecasts/<experiment>/<target>_<kind>.json: written to a temporary file, flushed, then linked to that name,
which fails if it exists, so a record is complete or absent and a retry cannot replace it. An issuance must
complete within `LATE` of its time; a later one is not issued. A failed or late attempt publishes
<target>_<kind>.failed-<attempt>.json (status `failed` or `late`, unique attempt id) and no forecast; there is no
fallback model. Records hold the code and model identity and a checksum of every input snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from . import artifact as A
from . import baselines as B
from . import features as F
from . import index
from . import sources

ROOT = sources.ROOT
OUT = ROOT / "data/forecasts"
LATE = pd.Timedelta(minutes=30)  # an issuance completed later than this after its time is late and not issued
WEATHER_RUNS = pd.Timedelta(days=3)  # runs older than this before the issuance cannot cover the target day


class Late(Exception):
    pass


def record_path(out: Path, name: str, target, kind: str) -> Path:
    return out / name / f"{target}_{kind}.json"


def publish(path: Path, text: str) -> None:
    """Write `text` to a temporary file and flush it to disk, then link it to `path`. The link fails if `path`
    exists (FileExistsError), so `path` appears complete or not at all and is never replaced."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ref(m: dict) -> dict:
    return {"source": m["source"], "name": m["name"], "sidecar": Path(m["sidecar"]).name, "sha256": m["sha256"],
            "sidecar_sha256": m["sidecar_sha256"], "retrieved_at": m["retrieved_at"]}


def _iso(t) -> str | None:
    return None if t is None or pd.isna(t) else pd.Timestamp(t).isoformat()


def _num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def issue(name: str, kind: str, target=None, now=None, store: Path = sources.STORE, out: Path = OUT) -> Path:
    """Issue one forecast and return its record path. Refuses before the issuance time and when it was already
    issued; on failure or lateness publishes a failure record and raises."""
    exp = F.belgium()
    clock = time.monotonic()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    target = pd.Timestamp(target).date() if target is not None else (now.tz_convert(exp.tz) + pd.Timedelta(days=1)).date()
    issuance = F.issuance_time(exp, target, kind)
    deadline = issuance + LATE
    path = record_path(out, name, target, kind)
    if now < issuance:
        raise SystemExit(f"{target} {kind}: issuance time {issuance} has not passed")
    if path.exists():
        raise SystemExit(f"{path.name} exists: already issued")
    path.parent.mkdir(parents=True, exist_ok=True)
    attempt = f"{now.tz_convert('UTC'):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    base = {"experiment": name, "kind": kind, "target_date": str(target), "issuance_utc": issuance.isoformat(),
            "deadline_utc": deadline.isoformat(), "attempt": attempt, "started_at": now.isoformat(),
            "code_commit": _git("rev-parse", "HEAD"), "code_dirty": bool(_git("status", "--porcelain", "src", "config", "artifacts"))}
    completed = lambda: now + pd.Timedelta(seconds=time.monotonic() - clock)
    try:
        if now > deadline:
            raise Late(f"started at {now}, after the deadline {deadline}")
        since = pd.Timestamp(target, tz="UTC") - pd.Timedelta(days=12)
        elia = [m for m in sources.snapshots("elia", store)
                if m["dataset"] in ("ods209", "ods210") and F._ts(m["retrieved_at"]) <= issuance]
        if not elia:
            raise ValueError("no Elia snapshot retrieved by the issuance")
        elia = index.reaching(index.connect(index.path_for(elia)), elia, since)
        weather = [m for m in sources.snapshots(f"weather-{name}", store)
                   if F._ts(m["retrieved_at"]) <= issuance and F._ts(m["run"]) >= issuance - WEATHER_RUNS
                   and sources.run_complete(m, name)]
        inputs = F.BelgiumInputs(elia, weather, exp, since=since)
        rows = F.features_for_issuance(exp, inputs, target, kind, live=True)
        model, man = A.load(A.directory(name))
        forecast = A.predict(model, man, rows)
        cap = rows["capacity"].to_numpy(float)
        persistence = np.clip(B.Persistence().predict(rows), 0, cap)
        physics = np.clip(B.BelgiumPhysics(inputs, live=True).predict(rows[list(F.CONTRACT)]), 0, cap)
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
        rec["completed_at"] = completed().isoformat()
        text = json.dumps(rec, indent=1, allow_nan=False) + "\n"
        if completed() > deadline:
            raise Late(f"completed after the deadline {deadline}")
        try:
            publish(path, text)
        except FileExistsError:
            raise SystemExit(f"{path.name} exists: already issued") from None
    except Exception as e:
        fail = path.with_name(f"{path.stem}.failed-{attempt}.json")
        publish(fail, json.dumps(base | {"status": "late" if isinstance(e, Late) else "failed",
                                         "completed_at": completed().isoformat(), "error": f"{type(e).__name__}: {e}",
                                         "traceback": traceback.format_exc(limit=5)}, indent=1) + "\n")
        raise
    return path


def _git(*a) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
