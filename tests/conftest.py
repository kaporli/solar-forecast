"""Synthetic snapshot stores in the real formats. No network."""
import json

import numpy as np
import pandas as pd
import pytest

from solar_forecast import sources

PROVINCES = sources.config("belgium")["weather_points"]["provinces"]


def elia_csv(start: str, end: str, measured=lambda t: 50.0, capacity=1000.0, forecasts=(40.0, 45.0)) -> bytes:
    """ods032-format quarter-hours for Belgium and the 11 provinces (provinces carry only capacity)."""
    times = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    rows = []
    for t in times:
        m = measured(t)
        rows.append([t.isoformat(), "PT15M", "Belgium", "" if m is None else m, capacity, *forecasts])
        for i, p in enumerate(PROVINCES):
            rows.append([t.isoformat(), "PT15M", p["ods032"], "", 10.0 * (i + 1), "", ""])
    df = pd.DataFrame(rows, columns=["datetime", "resolutioncode", "region", "measured", "monitoredcapacity",
                                     "dayahead11hforecast", "dayaheadforecast"])
    return df.to_csv(index=False).encode()


def run_json(run: pd.Timestamp, points: int = 11, hours: int = 49) -> bytes:
    """Every variable equals the label's hour offset from the run, so alignment is checkable by value."""
    labels = [run + pd.Timedelta(hours=k) for k in range(hours)]
    loc = {"hourly_units": dict(sources.UNITS, time="iso8601"),
           "hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in labels],
                      **{v: [float(k) for k in range(hours)] for v in sources.UNITS}}}
    return json.dumps([loc] * points if points > 1 else loc).encode()


@pytest.fixture
def store(tmp_path):
    def put(source, name, ext, body, retrieved, **extra):
        sources.save(source, name, ext, body, {"retrieved_at": pd.Timestamp(retrieved).isoformat(), "url": "test", **extra}, tmp_path)
    put.path = tmp_path
    return put
