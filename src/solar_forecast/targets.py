"""Hourly targets with validity. Missing stays missing (NaN, observed False); exclusions keep their reason.

Neither selected source is a cumulative meter (Elia: mean MW per quarter-hour; PVDAQ 2107: mean kW per
quarter-hour), so no differencing is done and no first-reading or reset handling applies.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from .weather import HOUR, day_hours, solar

QUARTER = pd.Timedelta(minutes=15)


def _hourly(qh: pd.DataFrame, value: str, hours: pd.DatetimeIndex, tz: str, start_of: pd.Series) -> pd.DataFrame:
    """Aggregate valid quarter-hours (value not NaN, no exclusion) into hours; an hour needs four."""
    valid = qh[value].notna() & (qh["exclusion"] == "")
    key = start_of.dt.floor("h")
    g = qh.assign(_valid=valid, _hour=key).groupby("_hour")
    n_valid = g["_valid"].sum().reindex(hours, fill_value=0).astype(int)
    reasons = (qh[qh["exclusion"] != ""].groupby(key[qh["exclusion"] != ""])["exclusion"]
               .agg(lambda s: ",".join(sorted(set(s)))).reindex(hours).fillna(""))
    local = hours.tz_convert(tz)
    return pd.DataFrame({
        "hour_utc": hours, "local_date": local.date, "local_hour": local.hour.astype("int8"),
        "n_quarter_hours": n_valid.to_numpy(), "observed": (n_valid == 4).to_numpy(), "exclusion": reasons.to_numpy(),
    })


def _night_zero(t: pd.DataFrame, points) -> pd.Series:
    """0.0 for an unobserved hour with the sun below the horizon at every point, else NaN."""
    sun = solar(pd.DatetimeIndex(t["hour_utc"]), [p[0] for p in points], [p[1] for p in points])
    night = sun["night"].groupby(level="hour_utc").all().reindex(t["hour_utc"]).to_numpy()
    return pd.Series(np.where(night & ~t["observed"].to_numpy(), 0.0, np.nan), index=t.index)


def _finish(t: pd.DataFrame) -> pd.DataFrame:
    t["eligible"] = t["observed"] & (t["exclusion"] == "")
    t["day_complete"] = t.groupby("local_date")["observed"].transform("all")
    return t


def belgium_target(qh: pd.DataFrame, first_day, last_day, points) -> pd.DataFrame:
    """Hourly MWh for region Belgium from resolved quarter-hours (datetime = interval start, UTC)."""
    b = qh[qh["region"] == "Belgium"].sort_values("datetime").reset_index(drop=True)
    b["exclusion"] = np.select([b["measured_mw"] < 0, b["measured_mw"] > b["capacity_mw"]],
                               ["negative", "above_capacity"], "")
    hours = pd.DatetimeIndex([]).append([day_hours(d, "Europe/Brussels") for d in pd.date_range(first_day, last_day)])
    hours = hours.tz_convert("UTC")
    t = _hourly(b, "measured_mw", hours, "Europe/Brussels", b["datetime"])
    by = b.assign(_hour=b["datetime"].dt.floor("h")).groupby("_hour")
    full = lambda c: by[c].mean().where(by[c].count() == 4).reindex(hours).to_numpy()
    mean = lambda c: by[c].mean().reindex(hours).to_numpy()
    t["measured_upscaled_mwh"] = np.where(t["observed"], full("measured_mw"), np.nan)
    t["capacity_mw"] = mean("capacity_mw")
    t["elia_da11_mwh"] = full("elia_da11_mw")
    t["elia_da18_mwh"] = full("elia_da18_mw")
    t["night_zero_estimate"] = _night_zero(t, points)
    return _finish(t)


def parse_pvdaq_meta(meta: dict, column: str) -> None:
    m = meta["Metrics"][column]
    if (m.get("units"), m.get("aggregation_type")) != ("kw", "avg"):
        raise ValueError(f"{column}: expected kw avg, got {m.get('units')} {m.get('aggregation_type')}")


def pvdaq_quarter_hours(bodies: list[bytes], column: str, dc_kw: float, tz: str) -> pd.DataFrame:
    """Rows as end (UTC interval end), kw, exclusion. Identical duplicates are dropped, conflicting ones raise;
    labels that are ambiguous (fall-back) or nonexistent (spring-forward) are dropped, not assigned."""
    raw = pd.concat([pd.read_csv(io.BytesIO(b)) for b in bodies], ignore_index=True)
    missing = {"measured_on", column} - set(raw.columns)
    if missing:
        raise ValueError(f"missing columns {sorted(missing)}")
    raw["t"] = pd.to_datetime(raw["measured_on"])
    if (raw["t"] != raw["t"].dt.floor("15min")).any():
        raise ValueError("timestamps off the 15-minute grid")
    if raw.groupby("t")[column].nunique(dropna=False).max() > 1:
        raise ValueError("duplicate timestamps with different values")
    d = raw.drop_duplicates("t").sort_values("t")
    end = d["t"].dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    out = pd.DataFrame({"end": end.dt.tz_convert("UTC"), "kw": d[column].to_numpy(dtype=float)}).dropna(subset=["end"])
    out["exclusion"] = np.select([out["kw"] < 0, out["kw"] > dc_kw], ["negative", "above_dc_capacity"], "")
    out = out.reset_index(drop=True)
    out.attrs.update(rows=len(raw), duplicates_dropped=len(raw) - len(d), labels_dropped=int(end.isna().sum()))
    return out


def pvdaq_target(qh: pd.DataFrame, first_day, last_day, dc_kw: float, tz: str, point) -> pd.DataFrame:
    """Hourly kWh = sum of kW x 0.25 h over the four rows ending s + 15 min to s + 60 min. A local day with zero
    energy over at least six observed daylight hours is an outage: values kept, hours excluded (`outage_day`)."""
    hours = pd.DatetimeIndex([]).append([day_hours(d, tz) for d in pd.date_range(first_day, last_day)]).tz_convert("UTC")
    start = qh["end"] - QUARTER
    t = _hourly(qh, "kw", hours, tz, start)
    energy = (qh["kw"] * 0.25).where(qh["exclusion"] == "").groupby(start.dt.floor("h")).sum(min_count=1)
    t["energy_kwh"] = np.where(t["observed"], energy.reindex(hours).to_numpy(), np.nan)
    t["capacity_kw"] = dc_kw
    t["night_zero_estimate"] = _night_zero(t, [point])
    sun = solar(hours, [point[0]], [point[1]])["night"].to_numpy()
    day = t.assign(_day=~sun)
    daylight = day[day["_day"] & day["observed"]].groupby("local_date")["energy_kwh"].agg(["sum", "count"])
    outage = set(daylight[(daylight["sum"] == 0) & (daylight["count"] >= 6)].index)
    t["exclusion"] = np.where(t["local_date"].isin(outage),
                              np.where(t["exclusion"] == "", "outage_day", t["exclusion"] + ",outage_day"), t["exclusion"])
    return _finish(t)
