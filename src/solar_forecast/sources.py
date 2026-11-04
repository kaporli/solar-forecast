"""Retrieval into an append-only snapshot store, and parsing with validation.

Every response is written once to data/raw/snapshots/<source>/<name>_<UTC>_<uuid>.<ext> with a sidecar (.meta.json)
holding the request, SHA-256, request_started_at and retrieved_at (response fully received). Nothing is
overwritten. Builds read only from this store.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data/raw/snapshots"
CONFIG = ROOT / "config"
ELIA = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets"
SINGLE_RUNS = "https://single-runs-api.open-meteo.com/v1/forecast"
HOURLY = "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m,wind_speed_10m,cloud_cover"
UNITS = {
    "shortwave_radiation": "W/m²", "direct_normal_irradiance": "W/m²", "diffuse_radiation": "W/m²",
    "temperature_2m": "°C", "wind_speed_10m": "m/s", "cloud_cover": "%",
}
RADIATION = ("shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation")
INSTANT = ("temperature_2m", "wind_speed_10m", "cloud_cover")


def config(name: str) -> dict:
    return json.loads((CONFIG / f"{name}.json").read_text())


def get(url: str) -> bytes:
    for attempt in range(6):  # Open-Meteo returns 5xx when overloaded; back off up to about 5 minutes
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return r.read()
        except Exception as e:  # a 4xx other than 429 is permanent, e.g. a run Open-Meteo never published
            if attempt == 5 or (isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429):
                raise
            time.sleep(10 * 2 ** attempt)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def save(source: str, name: str, ext: str, body: bytes, meta: dict, store: Path = STORE) -> Path:
    out = store / source
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{name}_{meta['retrieved_at'].replace(':', '').replace('-', '').replace('+0000', 'Z')}_{uuid.uuid4().hex}"
    path = out / f"{stem}.{ext}"
    with open(path, "xb") as f:  # exclusive: an existing snapshot is never replaced
        f.write(body)
    with open(out / f"{stem}.meta.json", "x") as f:
        json.dump({**meta, "source": source, "name": name, "file": path.name,
                   "bytes": len(body), "sha256": sha256(body)}, f, indent=1)
    return path


def fetch(source: str, name: str, ext: str, url: str, extra: dict, check=None, store: Path = STORE) -> Path:
    started = datetime.now(timezone.utc)
    for attempt in range(4):
        body = get(url)
        try:
            if check:
                check(body)
            break
        except ValueError:  # e.g. a server timeout sent as HTTP 200 with a text body
            if attempt == 3:
                raise
            time.sleep(30 * (attempt + 1))
    retrieved = datetime.now(timezone.utc)
    meta = {"url": url, **extra, "request_started_at": started.isoformat(), "retrieved_at": retrieved.isoformat()}
    return save(source, name, ext, body, meta, store)


def snapshots(source: str, store: Path = STORE) -> list[dict]:
    """Sidecars of one source, with `path`, oldest first."""
    out = []
    for p in sorted((store / source).glob("*.meta.json")):
        meta = json.loads(p.read_text())
        if "file" in meta:
            out.append({**meta, "path": p.parent / meta["file"]})
    return sorted(out, key=lambda m: m["retrieved_at"])


def read_verified(meta: dict) -> bytes:
    body = Path(meta["path"]).read_bytes()
    if sha256(body) != meta["sha256"]:
        raise ValueError(f"{meta['path']}: checksum does not match its sidecar")
    return body


# Elia

def elia_selections() -> dict[str, dict]:
    provinces = config("belgium")["weather_points"]["provinces"]
    old = " or ".join(f"region='{p['ods032']}'" for p in provinces)
    return {
        "ods032_belgium": {"dataset": "ods032", "where": "region='Belgium' and datetime >= date'2024-03-01'",
                           "select": "datetime,resolutioncode,region,measured,monitoredcapacity,dayahead11hforecast,dayaheadforecast"},
        "ods032_provinces": {"dataset": "ods032", "where": f"({old}) and datetime >= date'2024-03-01'",
                             "select": "datetime,resolutioncode,region,monitoredcapacity"},
        "ods209_total": {"dataset": "ods209", "where": "gridconnectiontype='Total'",
                         "select": "datetime,region,measured_upscaled,monitoredcapacity,dayahead11hforecast,dayaheadforecast"},
        "ods210_total": {"dataset": "ods210", "where": "gridconnectiontype='Total'",
                         "select": "datetime,resolutioncode,region,measured,monitoredcapacity,dayahead11hforecast,dayaheadforecast"},
    }


def fetch_elia(name: str, store: Path = STORE) -> Path:
    s = elia_selections()[name]
    q = urllib.parse.urlencode({"where": s["where"], "select": s["select"], "order_by": "datetime", "delimiter": ","})
    return fetch("elia", name, "csv", f"{ELIA}/{s['dataset']}/exports/csv?{q}", s, store=store)


ELIA_COLUMNS = {"measured": "measured_mw", "measured_upscaled": "measured_mw", "monitoredcapacity": "capacity_mw",
                "dayahead11hforecast": "elia_da11_mw", "dayaheadforecast": "elia_da18_mw"}


def parse_elia(body: bytes, dataset: str) -> pd.DataFrame:
    """Quarter-hours as datetime (UTC start), region (ods209 names), measured_mw, capacity_mw, elia_da11_mw,
    elia_da18_mw. Raises on schema, resolution, timestamp or value errors and on conflicting duplicates."""
    df = pd.read_csv(io.BytesIO(body), encoding="utf-8-sig", dtype={"measured_upscaled": str},
                     keep_default_na=False, na_values=[""])  # only empty is missing; "n/a" must fail parsing
    missing = {"datetime", "region", "monitoredcapacity"} - set(df.columns)
    if missing:
        raise ValueError(f"{dataset}: missing columns {sorted(missing)}")
    if "resolutioncode" in df and not (df["resolutioncode"] == "PT15M").all():
        raise ValueError(f"{dataset}: resolution other than PT15M: {sorted(set(df['resolutioncode']) - {'PT15M'})}")
    if "measured_upscaled" in df:  # ods209 publishes text with a decimal comma
        text = df["measured_upscaled"].str.strip().str.replace(",", ".", regex=False)
        num = pd.to_numeric(text, errors="coerce")
        bad = text.notna() & (text != "") & num.isna()
        if bad.any():
            raise ValueError(f"{dataset}: unparsable measured_upscaled {df.loc[bad, 'measured_upscaled'].iloc[0]!r}")
        df["measured_upscaled"] = num
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    off = df["datetime"] != df["datetime"].dt.floor("15min")
    if off.any():
        raise ValueError(f"{dataset}: timestamps off the 15-minute grid, e.g. {df.loc[off, 'datetime'].iloc[0]}")
    rename = {p["ods032"]: p["ods209"] for p in config("belgium")["weather_points"]["provinces"]}
    df["region"] = df["region"].replace(rename)
    df = df.rename(columns=ELIA_COLUMNS)
    cols = ["datetime", "region"] + [c for c in ("measured_mw", "capacity_mw", "elia_da11_mw", "elia_da18_mw") if c in df]
    df = df[cols]
    dup = df.duplicated(["datetime", "region"], keep=False)
    if dup.any():
        conflicting = df[dup].groupby(["datetime", "region"]).nunique(dropna=False).gt(1).any(axis=1)
        if conflicting.any():
            raise ValueError(f"{dataset}: duplicate quarter-hours with different values, e.g. {conflicting[conflicting].index[0]}")
    out = df.drop_duplicates(["datetime", "region"]).reset_index(drop=True)
    out.attrs["duplicates_dropped"] = int(len(df) - len(out))
    return out


# Open-Meteo single runs

def weather_params(experiment: str) -> dict:
    if experiment == "belgium":
        pts = config("belgium")["weather_points"]["provinces"]
        return {"latitude": ",".join(str(p["lat"]) for p in pts), "longitude": ",".join(str(p["lon"]) for p in pts),
                "forecast_hours": 49}
    site = config("installation")
    return {"latitude": site["latitude"], "longitude": site["longitude"], "forecast_hours": 57}


def check_run_body(body: bytes) -> None:
    try:
        j = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON: {body[:80]!r}") from e
    if isinstance(j, dict) and j.get("error"):
        raise ValueError(j.get("reason"))


def fetch_run(experiment: str, run: pd.Timestamp, store: Path = STORE) -> Path:
    params = {"models": "ecmwf_ifs", "timezone": "UTC", "wind_speed_unit": "ms", "hourly": HOURLY,
              **weather_params(experiment), "run": run.strftime("%Y-%m-%dT%H:%M")}
    url = f"{SINGLE_RUNS}?{urllib.parse.urlencode(params, safe=',')}"
    return fetch(f"weather-{experiment}", run.strftime("%Y%m%dT%H"), "json", url,
                 {"run": run.isoformat(), "params": params}, check=check_run_body, store=store)


def parse_run(body: bytes, points: int) -> pd.DataFrame:
    """Long frame: point (0-based, request order), time (UTC label), one column per variable.
    Raises on errors, unit changes, duplicate labels or a wrong number of points."""
    check_run_body(body)
    j = json.loads(body)
    locs = j if isinstance(j, list) else [j]
    if len(locs) != points:
        raise ValueError(f"expected {points} points, got {len(locs)}")
    frames = []
    for i, loc in enumerate(locs):
        units = loc.get("hourly_units", {})
        wrong = {v: units.get(v) for v in UNITS if units.get(v) != UNITS[v]}
        if wrong:
            raise ValueError(f"point {i}: unexpected units {wrong}")
        h = pd.DataFrame(loc["hourly"])
        h["time"] = pd.to_datetime(h["time"], utc=True)
        if not h["time"].is_unique:
            raise ValueError(f"point {i}: duplicate labels")
        frames.append(h.assign(point=i))
    return pd.concat(frames, ignore_index=True)


# PVDAQ 2107

def fetch_pvdaq(manifest: dict, store: Path = STORE) -> list[Path]:
    """Download each manifest file once; refuse a file whose checksum changed upstream."""
    prod = manifest["installation_experiment"]["production"]
    have = {m["name"] for m in snapshots("pvdaq", store) if m.get("sha256") in {f["sha256"] for f in prod["files"]}}
    out = []
    for f in prod["files"]:
        name = Path(f["path"]).stem
        if name in have:
            continue
        p = fetch("pvdaq", name, Path(f["path"]).suffix[1:], f"{prod['base_url']}/{f['path']}", {"manifest_sha256": f["sha256"]}, store=store)
        if sha256(p.read_bytes()) != f["sha256"]:
            raise ValueError(f"{p.name}: checksum changed upstream")
        out.append(p)
    return out
