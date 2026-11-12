"""`solar-data local belgium`: the local view, contract in docs/local.md.

For the target day of the latest due issuance it computes, at every lattice point of config/local.json (391 points,
about one per cell of the weather model), the energy a system would produce per kWp for each listed tilt and
orientation: the physics reference of `baselines` (not fitted), on the ECMWF IFS HRES run the issuance rule names.
Nothing is measured below province level, so the file carries the closest measured level beside it: per province,
Elia's measured production and its own day-ahead forecast per kW of monitored capacity.

One immutable record per issuance, data/local/<experiment>/<target>_<kind>.json (published like a forecast record:
complete or absent, never replaced), and the public file data/feed/<experiment>-local.json, replaced atomically:
the latest record with the provinces' Elia values. A rerun with the record present only rewrites the public file.
There is no fallback run: if the prescribed run cannot be fetched the command fails and the previous file stands
until its target day ends (`end_utc`)."""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd
import pvlib

from . import baselines as B
from . import features as F
from . import index, sources
from .feed import ATTRIBUTION, KINDS, due_target
from .issue import _git, _ref, _staged, _sync_dir, publish, record_path
from .targets import belgium_target
from .weather import HOUR, day_hours, hour_weather

OUT = sources.ROOT / "data/local"
FEED = sources.ROOT / "data/feed"
SCHEMA = "kaporli.solar-forecast.local"
VERSION = 1
SOURCE = "weather-belgium-grid"
CHUNK = 200  # points per request: keeps the URL short and one run under Open-Meteo's 600 locations a minute
HOURS = 49  # as the archive's runs: covers the target day from either issuance's run
RECENT = 7  # past days listed per province
ELIA = {"11h": "elia_da11_mwh", "18h": "elia_da18_mwh"}


def fetch_grid(run: pd.Timestamp, store: Path = sources.STORE) -> list[Path]:
    """The run at every lattice point, one snapshot per `CHUNK` points, in lattice order."""
    pts = sources.config("local")["grid"]["points"]
    out = []
    for i in range(0, len(pts), CHUNK):
        part = pts[i:i + CHUNK]
        params = {"models": "ecmwf_ifs", "timezone": "UTC", "wind_speed_unit": "ms", "hourly": sources.HOURLY,
                  "latitude": ",".join(str(p[0]) for p in part), "longitude": ",".join(str(p[1]) for p in part),
                  "forecast_hours": HOURS, "run": run.strftime("%Y-%m-%dT%H:%M")}
        url = f"{sources.SINGLE_RUNS}?{urllib.parse.urlencode(params, safe=',')}"
        out.append(sources.fetch(SOURCE, f"{run:%Y%m%dT%H}_{i // CHUNK}", "json", url,
                                 {"run": run.isoformat(), "params": params, "points": len(part)},
                                 check=sources.check_run_body, store=store))
    return out


def energy(weather: pd.DataFrame, hours: pd.DatetimeIndex, points: list, geometries: list[dict], p: dict) -> np.ndarray:
    """Wh per kWp for each (point, geometry, hour [s, s + 1h)): `baselines.dc_per_kw` times the performance ratio,
    with the sun computed once per point. `weather`: parsed runs, `point` in the order of `points`."""
    w = hour_weather(weather, hours)  # rows by point, then hour
    mid = hours + HOUR / 2
    sun = [pvlib.solarposition.get_solarposition(mid, lat, lon) for lat, lon, *_ in points]
    zenith = np.concatenate([s["apparent_zenith"].to_numpy() for s in sun])
    azimuth = np.concatenate([s["azimuth"].to_numpy() for s in sun])
    extra = np.tile(pvlib.irradiance.get_extra_radiation(mid).to_numpy(), len(points))
    col = lambda c: w[c].to_numpy(float)
    dc = [B.dc_from_sun(zenith, azimuth, extra, col("shortwave_radiation"), col("direct_normal_irradiance"),
                        col("diffuse_radiation"), col("temperature_2m"), col("wind_speed_10m"), p | g)
          for g in geometries]
    return np.array(dc).reshape(len(geometries), len(points), len(hours)).transpose(1, 0, 2) * p["performance_ratio"] * 1000


def due(exp: F.Experiment, now: pd.Timestamp) -> tuple[pd.Timestamp, str]:
    """Target day and kind of the latest issuance at or before `now`."""
    return max(((due_target(exp, k, now), k) for k in KINDS), key=lambda tk: F.issuance_time(exp, tk[0], tk[1]))


def issue(name: str = "belgium", now=None, store: Path = sources.STORE, out: Path = OUT, fetch=fetch_grid) -> Path:
    """The record of the latest due issuance, computed and published unless it exists."""
    exp, cfg = F.belgium(), sources.config("local")
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    target, kind = due(exp, now)
    path = record_path(out, name, target.date(), kind)
    if path.exists():
        return path
    issuance = F.issuance_time(exp, target, kind)
    run = F.rule_run(exp, issuance, kind)
    metas = [sources.snapshot(p, store) for p in fetch(run, store)]
    frames, first = [], 0
    for m in metas:
        frames.append(sources.parse_run(sources.read_verified(m), m["points"]).assign(point=lambda d, k=first: d["point"] + k))
        first += m["points"]
    points, geometries = cfg["grid"]["points"], cfg["geometries"]
    if first != len(points):
        raise ValueError(f"expected {len(points)} lattice points, got {first}")
    p = B.CONFIG["physics"]["belgium"]
    day = day_hours(target, exp.tz)
    wh = energy(pd.concat(frames, ignore_index=True), day, points, geometries, p)
    if not np.isfinite(wh).all():
        raise ValueError(f"run {run}: weather missing for part of {target.date()}")
    wh = np.rint(wh).astype(int)
    lit = np.flatnonzero(wh.any(axis=(0, 1)))  # hours with production anywhere, first to last
    keep = slice(lit[0], lit[-1] + 1) if len(lit) else slice(0, 0)
    wh, hours = wh[:, :, keep], day[keep]
    ref = geometries.index(cfg["reference"])
    names = [pt[2] for pt in points]
    rec = {
        "schema": SCHEMA, "version": VERSION, "experiment": name, "kind": kind, "target_date": str(target.date()),
        "issuance_utc": issuance.isoformat(), "computed_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "code_commit": _git("rev-parse", "HEAD"),
        "weather": {"model": "ECMWF IFS HRES", "run_utc": run.isoformat(),
                    "retrieved_at": max(m["retrieved_at"] for m in metas), "snapshots": [_ref(m) for m in metas]},
        "unit": "Wh per kWp per hour",
        "assumptions": {k: p[k] for k in ("performance_ratio", "transposition", "albedo", "cell_temperature", "gamma_pdc_per_c")},
        "end_utc": (day[-1] + HOUR).isoformat(),
        "hours": [{"start_utc": h.isoformat(), "local_hour": int(h.tz_convert(exp.tz).hour)} for h in hours],
        "geometries": geometries, "reference": ref,
        "grid": {k: cfg["grid"][k] for k in ("lat_step", "lon_step")},
        "cells": [{"lat": lat, "lon": lon, "province": prov, "wh": wh[i].tolist()} for i, (lat, lon, prov) in enumerate(points)],
        "provinces": [{"id": prov, "cells": names.count(prov),
                       "wh": np.rint(wh[[i for i, n in enumerate(names) if n == prov], ref].mean(axis=0)).astype(int).tolist()}
                      for prov in dict.fromkeys(p["ods209"] for p in sources.config("belgium")["weather_points"]["provinces"])],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        publish(path, json.dumps(rec, separators=(",", ":"), allow_nan=False) + "\n")
    except FileExistsError:  # another run published it meanwhile
        pass
    return path


def _physics_day(out: Path, name: str, day, kind: str, province: str) -> float | None:
    """kWh per kWp of the reference geometry over `day` from that day's record of `kind`, else the other kind's."""
    for k in (kind, *[x for x in KINDS if x != kind]):
        p = record_path(out, name, day, k)
        if p.exists():
            return round(sum(next(x["wh"] for x in json.loads(p.read_text())["provinces"] if x["id"] == province)) / 1000, 3)
    return None


def build(name: str = "belgium", now=None, store: Path = sources.STORE, out: Path = OUT) -> dict:
    """The public file: the latest due record without its input snapshots, and per province Elia's values (kWh per
    kW of monitored capacity): its day-ahead forecast for the target day's hours and, for the last `RECENT` days
    before today, measured and day-ahead day totals beside the reference geometry's."""
    exp = F.belgium()
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    target, kind = due(exp, now)
    rec = json.loads(record_path(out, name, target.date(), kind).read_text())
    today = now.tz_convert(exp.tz).date()
    first = pd.Timestamp(today) - pd.Timedelta(days=RECENT)
    last = max(target, pd.Timestamp(today))
    since = first.tz_localize("UTC") - pd.Timedelta(days=1)
    elia = [m for m in sources.snapshots("elia", store) if m["dataset"] in ("ods209", "ods210")]
    resolved = None
    if elia:
        elia = index.reaching(index.connect(index.path_for(elia)), elia, since)
        resolved = F.BelgiumInputs(elia, [], exp, since=since).resolved()
    hours = pd.DatetimeIndex([h["start_utc"] for h in rec["hours"]])
    num = lambda x, n=3: None if x is None or not np.isfinite(x) else round(float(x), n)
    provinces = []
    for prov in rec["provinces"]:
        entry = {"id": prov["id"], "cells": prov["cells"], "wh": prov["wh"], "capacity_mw": None,
                 "elia_wh": [None] * len(hours), "recent": []}
        if resolved is not None and (resolved["region"] == prov["id"]).any():
            t = belgium_target(resolved, first, last, exp.points, region=prov["id"]).set_index("hour_utc")
            cap = t["capacity_mw"].dropna()
            entry["capacity_mw"] = num(cap.iloc[-1], 1) if len(cap) else None
            per_kw = lambda c: t[c] / t["capacity_mw"]  # MWh per MW = kWh per kW
            entry["elia_wh"] = [None if not np.isfinite(v) else int(round(v * 1000)) for v in per_kw(ELIA[kind]).reindex(hours)]
            for d in pd.date_range(first, periods=RECENT):
                g = t[t["local_date"] == d.date()]
                total = lambda c: per_kw(c)[g.index].sum(min_count=len(g)) if len(g) else np.nan
                entry["recent"].append({"date": str(d.date()),
                                        "measured_kwh": num(total("measured_upscaled_mwh")) if len(g) and g["day_complete"].all() else None,
                                        "elia_kwh": num(total(ELIA[kind])),
                                        "physics_kwh": _physics_day(out, name, d.date(), kind, prov["id"])})
        provinces.append(entry)
    rec["weather"] = {k: v for k, v in rec["weather"].items() if k != "snapshots"}
    return rec | {"generated_at": now.isoformat(), "provinces": provinces, "attribution": ATTRIBUTION + [
        {"source": "Eurostat GISCO NUTS 2021, (c) EuroGeographics for the administrative boundaries", "license": "non-commercial reuse with attribution", "use": "province of each lattice point"}]}


def write(name: str = "belgium", now=None, store: Path = sources.STORE, out: Path = OUT, feed: Path = FEED) -> Path:
    """Build the public file and replace data/feed/<name>-local.json atomically."""
    path = feed / f"{name}-local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _staged(path, json.dumps(build(name, now, store, out), separators=(",", ":"), allow_nan=False) + "\n")
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _sync_dir(path.parent)
    return path


def run(name: str = "belgium", now=None, store: Path = sources.STORE, out: Path = OUT, feed: Path = FEED, fetch=fetch_grid) -> Path:
    issue(name, now, store, out, fetch)
    return write(name, now, store, out, feed)
