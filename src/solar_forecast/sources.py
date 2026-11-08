"""Retrieval into an append-only snapshot store, and parsing with validation.

Every retrieval gets its own sidecar, data/raw/snapshots/<source>/<name>_<UTC>_<uuid>.meta.json, holding the request,
SHA-256 of the response bytes, request_started_at and retrieved_at (response fully received). The response body is
stored once per distinct content, gzip-compressed and named by that SHA-256: data/raw/objects/<sha[:2]>/<sha>.<ext>.gz
(sidecar key `object`). Snapshots taken before 2026-11-05 keep their plain payload next to the sidecar (key `file`)
and are never rewritten, so existing dataset manifests still pin them. Nothing is overwritten. Builds read only from
this store.
"""
from __future__ import annotations

import gzip
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
    """Write the body once per content (an existing object is kept, not replaced) and a new sidecar for this
    retrieval. Returns the sidecar path."""
    out = store / source
    out.mkdir(parents=True, exist_ok=True)
    digest = sha256(body)
    obj = Path("objects") / digest[:2] / f"{digest}.{ext}.gz"
    path = store.parent / obj
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        tmp.write_bytes(gzip.compress(body, mtime=0))
        tmp.rename(path)  # atomic: a reader never sees a partial object
    stem = f"{name}_{meta['retrieved_at'].replace(':', '').replace('-', '').replace('+0000', 'Z')}_{uuid.uuid4().hex}"
    sidecar = out / f"{stem}.meta.json"
    with open(sidecar, "x") as f:  # exclusive: an existing retrieval record is never replaced
        json.dump({**meta, "source": source, "name": name, "object": str(obj),
                   "bytes": len(body), "sha256": digest}, f, indent=1)
    return sidecar


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
    """Sidecars of one source, with `path` (payload), `sidecar` and `sidecar_sha256`, oldest first."""
    out = [snapshot(p, store) for p in sorted((store / source).glob("*.meta.json"))]
    return sorted([m for m in out if m], key=lambda m: m["retrieved_at"])


def snapshot(sidecar: Path, store: Path = STORE) -> dict | None:
    """One sidecar as `snapshots` lists it (None for a file that is not a snapshot sidecar)."""
    meta = json.loads(sidecar.read_text())
    if "file" not in meta and "object" not in meta:
        return None
    path = store.parent / meta["object"] if "object" in meta else sidecar.parent / meta["file"]
    return {**meta, "path": path, "sidecar": sidecar, "sidecar_sha256": sha256(sidecar.read_bytes())}


def read_verified(meta: dict) -> bytes:
    """The original response bytes (decompressed for stored objects), checked against the sidecar's SHA-256."""
    path = Path(meta["path"])
    body = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
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


def fetch_elia(name: str, start=None, end=None, store: Path = STORE) -> Path:
    """One selection, optionally bounded to UTC dates [start, end), ordered by time and region so identical content
    gives identical bytes (and one stored object)."""
    s = dict(elia_selections()[name])
    if start is not None:
        s |= {"where": f"{s['where']} and datetime >= date'{start}' and datetime < date'{end}'",
              "start": str(start), "end": str(end)}
    q = urllib.parse.urlencode({"where": s["where"], "select": s["select"], "order_by": "datetime,region",
                                "delimiter": ","})
    return fetch("elia", name, "csv", f"{ELIA}/{s['dataset']}/exports/csv?{q}", s, store=store)


ODS209_FIRST_MONTH = "2026-10-01"  # ods209 starts 2026-10-26 (protocol A)


def elia_routine(today, store: Path = STORE) -> list[Path]:
    """Scheduled capture: the latest 14 days of ods209 (plus the forecast days Elia publishes ahead) and the ods210
    live window."""
    d = pd.Timestamp(today).date()
    return [fetch_elia("ods209_total", d - pd.Timedelta(days=14), d + pd.Timedelta(days=15), store),
            fetch_elia("ods210_total", store=store)]


def elia_months(today) -> list[tuple]:
    """Fixed calendar-month partitions of ods209 from its first month through the current one, in UTC dates, each
    starting a day early: a Brussels day starts on the previous UTC date (22:00 or 23:00), so each response holds
    every local day of its month whole."""
    first = pd.Period(ODS209_FIRST_MONTH, "M")
    return [((m.start_time - pd.Timedelta(days=1)).date(), (m + 1).start_time.date())
            for m in pd.period_range(first, pd.Timestamp(today).to_period("M"))]


def elia_reconcile(today, store: Path = STORE) -> list[Path]:
    """Monthly recheck of older ods209 history, one response per calendar month, to find later revisions. A
    revision becomes known at this retrieval; its availability is never backdated."""
    return [fetch_elia("ods209_total", a, b, store) for a, b in elia_months(today)]


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


LIVE_HOURS = 72  # live runs: the previous day's 00 UTC run still covers a target day (offsets 47 to 70 h)


def fetch_run(experiment: str, run: pd.Timestamp, store: Path = STORE, hours: int | None = None) -> Path:
    """`hours`: forecast length, by default the archive's (`weather_params`). Values for a given hour do not depend
    on it, so a longer live run gives the same features for the hours both cover."""
    params = {"models": "ecmwf_ifs", "timezone": "UTC", "wind_speed_unit": "ms", "hourly": HOURLY,
              **weather_params(experiment), "run": run.strftime("%Y-%m-%dT%H:%M")}
    if hours:
        params["forecast_hours"] = hours
    url = f"{SINGLE_RUNS}?{urllib.parse.urlencode(params, safe=',')}"
    return fetch(f"weather-{experiment}", run.strftime("%Y%m%dT%H"), "json", url,
                 {"run": run.isoformat(), "params": params}, check=check_run_body, store=store)


def fetch_new_runs(experiment: str, now, hours=(0, 6), store: Path = STORE) -> list[Path]:
    """Today's runs at `hours` UTC without a complete stored response (`run_complete`) and at least three hours old,
    with `LIVE_HOURS` so they can serve as the protocol's fallback the next day. A run Open-Meteo has not published
    yet is skipped, and an incomplete response is kept but does not count: both are tried again at the next
    capture."""
    now = pd.Timestamp(now)
    due = {r for h in hours if (r := now.normalize() + pd.Timedelta(hours=h)) <= now - pd.Timedelta(hours=3)}
    # only the due runs' payloads are read: the work does not grow with the archive
    have = {pd.Timestamp(m["run"]) for m in snapshots(f"weather-{experiment}", store)
            if pd.Timestamp(m["run"]) in due and run_complete(m, experiment)}
    out = []
    for run in sorted(due - have):
        try:
            out.append(fetch_run(experiment, run, store, LIVE_HOURS))
        except (ValueError, urllib.error.HTTPError) as e:
            print("skipped", run, e, flush=True)
    return out


def run_complete(meta: dict, experiment: str) -> bool:
    """A stored run response is usable: it parses, has every point, every hour it requested from the run on and
    every variable, with no missing value after the run hour (whose radiation, accumulated over the hour before,
    is always empty). Raises if the payload is missing or changed."""
    want = weather_params(experiment)
    points = len(str(want["latitude"]).split(","))
    hours = meta.get("params", {}).get("forecast_hours", want["forecast_hours"])
    body = read_verified(meta)
    try:
        df = parse_run(body, points)
    except (ValueError, KeyError, TypeError):
        return False
    labels = pd.date_range(pd.Timestamp(meta["run"]), periods=hours, freq="h")
    after = df[df["time"] > labels[0]] if set(UNITS) <= set(df.columns) else None
    return (after is not None and after[list(UNITS)].notna().all().all()
            and all(pd.DatetimeIndex(g["time"]).equals(labels) for _, g in df.groupby("point")))


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
        if sha256(read_verified(snapshot(p, store))) != f["sha256"]:  # the stored payload, not the sidecar
            raise ValueError(f"{p.name}: checksum changed upstream")
        out.append(p)
    return out
