"""Interval alignment of forecast weather and solar geometry for hours [s, s + 1h), keyed by UTC start s."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from .sources import INSTANT, RADIATION

HOUR = pd.Timedelta(hours=1)


def day_hours(day, tz: str) -> pd.DatetimeIndex:
    """UTC starts of every hour of local calendar day `day` (23, 24 or 25 hours)."""
    d = pd.Timestamp(day).date()
    start = pd.Timestamp(d).tz_localize(tz)
    end = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(tz)
    return pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq="h", inclusive="left")


def hour_weather(run: pd.DataFrame, hours: pd.DatetimeIndex) -> pd.DataFrame:
    """Per point and hour: radiation at label s + 1h (mean of the hour ending there); temperature, wind and cloud
    cover as the mean of labels s and s + 1h. A missing label or value gives NaN."""
    out = []
    for point, g in run.groupby("point"):
        g = g.set_index("time")
        end = g.reindex(hours + HOUR)
        start = g.reindex(hours)
        f = pd.DataFrame(index=pd.Index(hours, name="hour_utc"))
        for v in RADIATION:
            f[v] = end[v].to_numpy(dtype=float)
        for v in INSTANT:
            f[v] = (start[v].to_numpy(dtype=float) + end[v].to_numpy(dtype=float)) / 2
        out.append(f.assign(point=point))
    return pd.concat(out).reset_index().set_index(["point", "hour_utc"])


def solar(hours: pd.DatetimeIndex, lats, lons) -> pd.DataFrame:
    """Per point and hour: cosine of the apparent zenith and azimuth at s + 30 min, clear-sky GHI (Haurwitz),
    and `night`: the sun's upper limb below the horizon at both s and s + 1h."""
    mid = hours + HOUR / 2
    out = []
    for point, (lat, lon) in enumerate(zip(lats, lons)):
        sp = pvlib.solarposition.get_solarposition(mid, lat, lon)
        edge = pvlib.solarposition.get_solarposition(hours.append(hours + HOUR), lat, lon)["apparent_elevation"].to_numpy()
        n = len(hours)
        out.append(pd.DataFrame({
            "point": point, "hour_utc": hours,
            "cos_zenith": np.cos(np.radians(sp["apparent_zenith"].to_numpy())).clip(min=0),
            "solar_azimuth_deg": sp["azimuth"].to_numpy(),
            "ghi_clearsky": pvlib.clearsky.haurwitz(sp["apparent_zenith"])["ghi"].to_numpy(),
            "night": (edge[:n] < -0.833) & (edge[n:] < -0.833),
        }))
    return pd.concat(out, ignore_index=True).set_index(["point", "hour_utc"])


def weighted(per_point: pd.DataFrame, weights: pd.Series) -> pd.DataFrame:
    """Weighted mean over points per hour; NaN if any point is NaN. `weights` is indexed by point and sums to 1."""
    w = per_point.index.get_level_values("point").map(weights).to_numpy()[:, None]
    vals = per_point.to_numpy(dtype=float) * w
    frame = pd.DataFrame(vals, index=per_point.index, columns=per_point.columns)
    g = frame.groupby(level="hour_utc")
    return g.sum(min_count=1).where(~g.apply(lambda x: x.isna().any()), np.nan)
