"""Versioned feature builder: one function per issuance, used by batch training, historical replay and inference.

Every row is computed from inputs permitted at its issuance (docs/forecast-protocol.md):
- actuals: only hours ending at or before the experiment's cutoff;
- capacity: the value on the latest quarter-hour at or before the cutoff;
- weather: the run named by the issuance rule.
`live=True` additionally drops every snapshot retrieved after the issuance and falls back to the latest
earlier run if the rule's run was not retrieved in time. Rows are row-local: no statistic is computed across
issuances, so the build fits nothing. Learned preprocessing (scalers, imputers) belongs inside the model
pipeline and is fitted per fold on training rows only.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

import numpy as np
import pandas as pd

from . import sources
from .targets import belgium_target, parse_pvdaq_meta, pvdaq_quarter_hours, pvdaq_target
from .weather import HOUR, day_hours, hour_weather, solar

FEATURE_VERSION = "v1"
WEATHER = list(sources.RADIATION + sources.INSTANT)

# name: (dtype, unit, meaning and missingness)
CONTRACT = {
    "feature_version": ("string", "", f"'{FEATURE_VERSION}'"),
    "experiment": ("string", "", "'belgium' or 'pvdaq'"),
    "kind": ("string", "", "issuance: '11h' or '18h' (belgium), '09h' (pvdaq)"),
    "issuance_utc": ("datetime64[ns, UTC]", "", "issuance time"),
    "target_date": ("date", "", "local target day D"),
    "target_hour_utc": ("datetime64[ns, UTC]", "", "start s of the target hour [s, s + 1h)"),
    "local_hour": ("int8", "h", "local clock hour of s; repeated on the 25-hour day"),
    "day_of_year": ("int16", "", "of the local target day"),
    "actuals_cutoff_utc": ("datetime64[ns, UTC]", "", "actuals must end at or before this time"),
    "weather_run_utc": ("datetime64[ns, UTC]", "", "initialisation of the run used; NaT if none"),
    "weather_availability": ("string", "", "recorded (retrieved before issuance), assumed (archive, retrieved later), fallback (live, earlier run), missing"),
    "weather_retrieved_at": ("datetime64[ns, UTC]", "", "retrieval of the weather snapshot used; NaT if none"),
    "lead_hours": ("float64", "h", "s minus the run initialisation; NaN if no run"),
    "capacity": ("float64", "MW (belgium), kW (pvdaq)", "monitored capacity known at the cutoff (belgium), DC capacity (pvdaq)"),
    "persistence_lf": ("float64", "1", "load factor of the same local clock hour on D-2, else the latest earlier day up to D-8 where it is eligible and ended before the cutoff; NaN if none"),
    "persistence_source_date": ("date", "", "local day the persistence value came from; NaT if none"),
    "shortwave_radiation": ("float64", "W/m2", "GHI, mean of the hour, label s + 1h; capacity-weighted over points"),
    "direct_normal_irradiance": ("float64", "W/m2", "as shortwave_radiation"),
    "diffuse_radiation": ("float64", "W/m2", "as shortwave_radiation"),
    "temperature_2m": ("float64", "degC", "mean of labels s and s + 1h; capacity-weighted"),
    "wind_speed_10m": ("float64", "m/s", "as temperature_2m"),
    "cloud_cover": ("float64", "%", "as temperature_2m"),
    "weather_complete": ("bool", "", "every weather column above is non-null"),
    "cos_zenith": ("float64", "1", "cosine of the apparent solar zenith at s + 30 min, at the capacity-weighted centroid, clipped at 0"),
    "solar_azimuth_deg": ("float64", "deg", "compass azimuth at s + 30 min, same point"),
    "ghi_clearsky": ("float64", "W/m2", "Haurwitz clear-sky GHI at s + 30 min, same point"),
}


@dataclass(frozen=True)
class Experiment:
    name: str
    tz: str
    points: list  # (lat, lon) in weather request order
    kinds: dict  # kind -> (local issuance time "HH:MM", run hour UTC, first target date)
    cutoff: Callable[[pd.Timestamp], pd.Timestamp]
    target_col: str
    capacity_col: str
    first_target: str = "2024-03-15"
    persistence_days: tuple = field(default=tuple(range(2, 9)))


def belgium() -> Experiment:
    pts = sources.config("belgium")["weather_points"]["provinces"]
    return Experiment("belgium", "Europe/Brussels", [(p["lat"], p["lon"]) for p in pts],
                      {"11h": ("11:00", 0, "2024-03-15"), "18h": ("18:00", 6, "2024-08-16")},
                      lambda i: i - pd.Timedelta(minutes=30), "measured_upscaled_mwh", "capacity_mw")


def pvdaq() -> Experiment:
    site = sources.config("installation")
    tz = site["timezone"]
    return Experiment("pvdaq", tz, [(site["latitude"], site["longitude"])], {"09h": ("09:00", 0, "2024-03-15")},
                      lambda i: i.tz_convert(tz).normalize().tz_convert("UTC"), "energy_kwh", "capacity_kw")


def issuance_time(exp: Experiment, target_date, kind: str) -> pd.Timestamp:
    local, _, _ = exp.kinds[kind]
    day = pd.Timestamp(target_date) - pd.Timedelta(days=1)
    return pd.Timestamp(f"{day.date()} {local}").tz_localize(exp.tz).tz_convert("UTC")


def rule_run(exp: Experiment, issuance: pd.Timestamp, kind: str) -> pd.Timestamp:
    _, hour, _ = exp.kinds[kind]
    return pd.Timestamp(issuance.tz_convert(exp.tz).date()).tz_localize("UTC") + pd.Timedelta(hours=hour)


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s).tz_convert("UTC")


class _Weather:
    def __init__(self, metas: list[dict], points: int):
        self.points = points
        self.runs: dict[pd.Timestamp, list[dict]] = {}
        for m in metas:
            self.runs.setdefault(_ts(m["run"]), []).append(m)

    @lru_cache(maxsize=4096)
    def _parse(self, path: str, sha: str) -> pd.DataFrame:
        return sources.parse_run(sources.read_verified({"path": path, "sha256": sha}), self.points)

    def get(self, run: pd.Timestamp, as_of):
        ok = [m for m in self.runs.get(run, []) if as_of is None or _ts(m["retrieved_at"]) <= as_of]
        if not ok:
            return None
        m = ok[-1]
        return self._parse(str(m["path"]), m["sha256"]), _ts(m["retrieved_at"])

    def latest_before(self, run: pd.Timestamp, as_of):
        for r in sorted((r for r in self.runs if r < run), reverse=True):
            got = self.get(r, as_of)
            if got:
                return r, got
        return None


class BelgiumInputs:
    """Elia quarter-hours from every snapshot (archive datasets take precedence over ods210, newer retrievals over
    older, column by column) and the weather snapshots."""

    PRIORITY = {"ods032": 0, "ods209": 0, "ods210": 1}

    def __init__(self, elia_metas: list[dict], weather_metas: list[dict], exp: Experiment):
        self.exp = exp
        frames = []
        self.stats = {"snapshots": {}, "duplicates_dropped": 0}
        for m in elia_metas:
            f = sources.parse_elia(sources.read_verified(m), m["dataset"])
            self.stats["duplicates_dropped"] += f.attrs["duplicates_dropped"]
            self.stats["snapshots"][m["name"]] = self.stats["snapshots"].get(m["name"], 0) + 1
            frames.append(f.assign(retrieved_at=_ts(m["retrieved_at"]), priority=self.PRIORITY[m["dataset"]]))
        self.qh = pd.concat(frames, ignore_index=True)
        self.weather = _Weather(weather_metas, len(exp.points))
        self.region_order = [p["ods209"] for p in sources.config("belgium")["weather_points"]["provinces"]]

    @lru_cache(maxsize=8)
    def resolved(self, as_of=None) -> pd.DataFrame:
        q = self.qh if as_of is None else self.qh[self.qh["retrieved_at"] <= as_of]
        q = q.sort_values(["priority", "retrieved_at"], ascending=[True, False])
        return q.groupby(["datetime", "region"], sort=True).first().reset_index().drop(columns="priority")

    def revisions(self) -> int:
        """Quarter-hours of region Belgium whose measured value differs between snapshots."""
        b = self.qh[self.qh["region"] == "Belgium"].dropna(subset=["measured_mw"])
        return int((b.groupby("datetime")["measured_mw"].agg(lambda s: s.max() - s.min()) > 1e-6).sum())

    @lru_cache(maxsize=8)
    def targets(self, as_of=None) -> pd.DataFrame:
        r = self.resolved(as_of)
        b = r[(r["region"] == "Belgium") & r["measured_mw"].notna()]
        last_end = b["datetime"].max() + pd.Timedelta(minutes=15)
        last_day = (last_end.tz_convert(self.exp.tz) - pd.Timedelta(seconds=1)).date()
        if (pd.Timestamp(last_day) + pd.Timedelta(days=1)).tz_localize(self.exp.tz) > last_end:
            last_day -= dt.timedelta(days=1)
        first = pd.Timestamp(self.exp.first_target) - pd.Timedelta(days=max(self.exp.persistence_days))
        return belgium_target(r, first, last_day, self.exp.points)

    @lru_cache(maxsize=8)
    def _capacity_series(self, as_of=None) -> dict:
        r = self.resolved(as_of).dropna(subset=["capacity_mw"])
        return {k: g.set_index("datetime")["capacity_mw"].sort_index() for k, g in r.groupby("region")}

    def capacity(self, as_of, cutoff) -> tuple[float, pd.Series]:
        s = self._capacity_series(as_of)
        last = cutoff - pd.Timedelta(minutes=15)  # a quarter-hour starting at `last` ends at the cutoff
        at = lambda k: (s[k].loc[:last].iloc[-1] if k in s and len(s[k].loc[:last]) else np.nan)
        w = pd.Series([at(k) for k in self.region_order], dtype=float)
        return at("Belgium"), w / w.sum()


class PvdaqInputs:
    """PVDAQ 2107 meter files (yearly batches: replay only) and the weather snapshots."""

    def __init__(self, pvdaq_metas: list[dict], weather_metas: list[dict], exp: Experiment, manifest: dict):
        self.exp = exp
        site = sources.config("installation")
        self.dc = site["dc_capacity_kw"]
        col = manifest["installation_experiment"]["production"]["column"]
        by = {m["name"]: m for m in pvdaq_metas}
        parse_pvdaq_meta(json.loads(sources.read_verified(by["2107_system_metadata"])), col)
        bodies = [sources.read_verified(by[n]) for n in sorted(by) if n.startswith("2107_meter_15m_data_")]
        self.qh = pvdaq_quarter_hours(bodies, col, self.dc, exp.tz)
        self.stats = dict(self.qh.attrs)
        self.weather = _Weather(weather_metas, 1)

    @lru_cache(maxsize=1)
    def targets(self, as_of=None) -> pd.DataFrame:
        if as_of is not None:
            raise ValueError("PVDAQ publishes yearly: there is no live issuance")
        last_day = (self.qh["end"].max() - pd.Timedelta(minutes=15)).tz_convert(self.exp.tz).date()
        first = pd.Timestamp(self.exp.first_target) - pd.Timedelta(days=max(self.exp.persistence_days))
        return pvdaq_target(self.qh, first, last_day, self.dc, self.exp.tz, self.exp.points[0])

    def capacity(self, as_of, cutoff) -> tuple[float, pd.Series]:
        return self.dc, pd.Series([1.0])


@lru_cache(maxsize=8)
def _persistence_lookup(inputs, as_of) -> dict:
    t = inputs.targets(as_of)
    t = t[t["eligible"]]
    lf = t[inputs.exp.target_col] / t[inputs.exp.capacity_col]
    out = {}
    for d, h, v, s in zip(t["local_date"], t["local_hour"], lf, t["hour_utc"]):
        out.setdefault((d, int(h)), (float(v), s + HOUR))  # fall-back source day: first occurrence
    return out


def features_for_issuance(exp: Experiment, inputs, target_date, kind: str, live: bool = False) -> pd.DataFrame:
    issuance = issuance_time(exp, target_date, kind)
    as_of = issuance if live else None
    hours = day_hours(target_date, exp.tz)
    cutoff = exp.cutoff(issuance)
    capacity, weights = inputs.capacity(as_of, cutoff)
    target_day = pd.Timestamp(target_date).date()

    run = rule_run(exp, issuance, kind)
    got = inputs.weather.get(run, as_of)
    availability = "missing"
    if got:
        availability = "recorded" if got[1] <= issuance else "assumed"
    elif live:
        fb = inputs.weather.latest_before(run, as_of)
        if fb:
            run, got, availability = fb[0], fb[1], "fallback"
    if got:
        per_point = hour_weather(got[0], hours)
        w = per_point.index.get_level_values("point").map(weights).to_numpy()[:, None]
        vals = pd.DataFrame(per_point[WEATHER].to_numpy() * w, index=per_point.index, columns=WEATHER)
        g_sum = vals.groupby(level="hour_utc").sum(min_count=1)
        g_n = per_point[WEATHER].notna().groupby(level="hour_utc").sum()
        wx = g_sum.where(g_n == len(exp.points)).reindex(hours)
        retrieved = got[1]
    else:
        wx = pd.DataFrame(np.nan, index=hours, columns=WEATHER)
        run, retrieved = pd.NaT, pd.NaT

    lat = float(np.dot(weights, [p[0] for p in exp.points]))
    lon = float(np.dot(weights, [p[1] for p in exp.points]))
    if np.isfinite(lat):
        sun = solar(hours, [lat], [lon]).droplevel("point")
    else:
        sun = pd.DataFrame(np.nan, index=hours, columns=["cos_zenith", "solar_azimuth_deg", "ghi_clearsky"])

    lookup = _persistence_lookup(inputs, as_of)
    local = hours.tz_convert(exp.tz)
    pers, src = [], []
    for h in local.hour:
        hit = None
        for k in exp.persistence_days:
            d = target_day - dt.timedelta(days=k)
            v = lookup.get((d, int(h)))
            if v and v[1] <= cutoff:
                hit = (v[0], d)
                break
        pers.append(hit[0] if hit else np.nan)
        src.append(hit[1] if hit else pd.NaT)

    f = pd.DataFrame({
        "feature_version": FEATURE_VERSION, "experiment": exp.name, "kind": kind,
        "issuance_utc": issuance, "target_date": target_day, "target_hour_utc": hours,
        "local_hour": local.hour.astype("int8"), "day_of_year": np.int16(pd.Timestamp(target_day).day_of_year),
        "actuals_cutoff_utc": cutoff, "weather_run_utc": run, "weather_availability": availability,
        "weather_retrieved_at": retrieved,
        "lead_hours": ((hours - run) / HOUR) if got else np.nan,
        "capacity": capacity, "persistence_lf": pers, "persistence_source_date": src,
    })
    for c in WEATHER:
        f[c] = wx[c].to_numpy()
    f["weather_complete"] = f[WEATHER].notna().all(axis=1)
    for c in ("cos_zenith", "solar_azimuth_deg", "ghi_clearsky"):
        f[c] = sun[c].to_numpy(dtype=float)
    return _typed(f)


def _typed(f: pd.DataFrame) -> pd.DataFrame:
    for c, (dtype, _, _) in CONTRACT.items():
        if dtype == "string":
            f[c] = f[c].astype("string")
        elif dtype.startswith("datetime64"):
            f[c] = pd.to_datetime(f[c], utc=True).astype("datetime64[ns, UTC]")
        elif dtype == "date":
            f[c] = pd.Series([None if pd.isna(x) else pd.Timestamp(x).date() for x in f[c]], index=f.index, dtype=object)
        else:
            f[c] = f[c].astype(dtype)
    return f[list(CONTRACT)]


def issuances(exp: Experiment, last_target) -> list[tuple]:
    out = []
    for d in pd.date_range(exp.first_target, last_target):
        for kind, (_, _, first) in exp.kinds.items():
            if d >= pd.Timestamp(first):
                out.append((d.date(), kind))
    return out


def build_features(exp: Experiment, inputs, items: list[tuple], live: bool = False) -> pd.DataFrame:
    """Batch entry point: the same per-issuance function over many issuances."""
    return pd.concat([features_for_issuance(exp, inputs, d, k, live) for d, k in items], ignore_index=True)


def examples(features: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """Features joined to the target of their hour (left join: hours without a target stay, with NaN)."""
    return features.merge(targets.drop(columns=["local_date", "local_hour"]), left_on="target_hour_utc",
                          right_on="hour_utc", how="left").drop(columns="hour_utc")


def contract_markdown() -> str:
    rows = "\n".join(f"| `{k}` | {t} | {u} | {d} |" for k, (t, u, d) in CONTRACT.items())
    return (f"# Feature contract {FEATURE_VERSION}\n\nGenerated by `uv run solar-data contract`; "
            "do not edit by hand. Rules: `src/solar_forecast/features.py`.\n\n"
            "| Column | dtype | Unit | Meaning, missingness and availability |\n| --- | --- | --- | --- |\n" + rows + "\n")
