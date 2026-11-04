import json

import numpy as np
import pandas as pd
import pytest

from solar_forecast import sources
from solar_forecast.sources import ROOT
from solar_forecast.targets import belgium_target, parse_pvdaq_meta, pvdaq_quarter_hours, pvdaq_target
from solar_forecast.weather import day_hours

from conftest import elia_csv

POINTS = [(p["lat"], p["lon"]) for p in sources.config("belgium")["weather_points"]["provinces"]]
COL = "meter_revenue_grade_ac_output_meter_149578"
SITE = (38.996306, -122.134111)


def test_dst_day_lengths():
    assert len(day_hours("2024-03-31", "Europe/Brussels")) == 23
    assert len(day_hours("2024-10-27", "Europe/Brussels")) == 25
    assert len(day_hours("2024-11-03", "America/Los_Angeles")) == 25
    assert len(day_hours("2025-03-09", "America/Los_Angeles")) == 23


def _belgium(measured):
    qh = sources.parse_elia(elia_csv("2024-06-09 22:00", "2024-06-10 22:00", measured), "ods032")
    return belgium_target(qh, "2024-06-10", "2024-06-10", POINTS)


def test_missing_quarter_hour_makes_the_hour_missing_not_zero():
    gap = pd.Timestamp("2024-06-10 11:15", tz="UTC")
    t = _belgium(lambda ts: None if ts == gap else 50.0).set_index("hour_utc")
    h = t.loc[pd.Timestamp("2024-06-10 11:00", tz="UTC")]
    assert not h["observed"] and np.isnan(h["measured_upscaled_mwh"]) and h["n_quarter_hours"] == 3
    assert np.isnan(h["night_zero_estimate"])  # daytime: no estimate
    assert t["observed"].sum() == 23 and not t["day_complete"].any()
    assert t.loc[pd.Timestamp("2024-06-10 12:00", tz="UTC"), "measured_upscaled_mwh"] == 50.0


def test_night_zero_estimate_kept_separate():
    night = pd.Timestamp("2024-06-10 21:45", tz="UTC")  # 23:45 local, as in 2024
    t = _belgium(lambda ts: None if ts == night else 0.0).set_index("hour_utc")
    h = t.loc[pd.Timestamp("2024-06-10 21:00", tz="UTC")]
    assert not h["observed"] and not h["eligible"] and np.isnan(h["measured_upscaled_mwh"])
    assert h["night_zero_estimate"] == 0.0


def test_implausible_values_excluded_with_reason():
    t = _belgium(lambda ts: -1.0 if ts.hour == 3 else (2000.0 if ts.hour == 12 else 50.0)).set_index("hour_utc")
    assert t.loc[pd.Timestamp("2024-06-10 03:00", tz="UTC"), "exclusion"] == "negative"
    assert t.loc[pd.Timestamp("2024-06-10 12:00", tz="UTC"), "exclusion"] == "above_capacity"
    assert not t.loc[pd.Timestamp("2024-06-10 12:00", tz="UTC"), "eligible"]


def _fixture():
    return (ROOT / "fixtures/pvdaq_2107_meter_dst.csv").read_bytes()


def test_pvdaq_fall_back_labels_dropped_and_spring_forward():
    qh = pvdaq_quarter_hours([_fixture()], COL, 892.8, "America/Los_Angeles")
    assert qh.attrs["labels_dropped"] == 4  # 01:00 to 01:45 on 2024-11-03 occur once and are ambiguous
    t = pvdaq_target(qh, "2024-11-03", "2024-11-03", 892.8, "America/Los_Angeles", SITE)
    # end label 01:00 belongs to 07 or 08 UTC, 01:15 to 01:45 to 08 or 09 UTC: those three hours are missing
    assert len(t) == 25 and t["observed"].sum() == 22 and not t["day_complete"].any()
    missing = t.loc[~t["observed"], "hour_utc"].dt.hour.tolist()
    assert missing == [7, 8, 9]
    assert t.loc[~t["observed"], "night_zero_estimate"].eq(0.0).all()  # night: estimate only, separate column
    t = pvdaq_target(qh, "2025-03-09", "2025-03-09", 892.8, "America/Los_Angeles", SITE)
    assert len(t) == 23 and t["day_complete"].all()


def test_pvdaq_energy_is_quarter_hour_mean_power_times_quarter_hours():
    body = "measured_on,%s\n" % COL + "".join(
        f"2025-06-01 {h:02d}:{m:02d}:00,{100.0}\n" for h in range(24) for m in (0, 15, 30, 45))
    qh = pvdaq_quarter_hours([body.encode()], COL, 892.8, "America/Los_Angeles")
    t = pvdaq_target(qh, "2025-05-31", "2025-05-31", 892.8, "America/Los_Angeles", SITE)
    # 2025-06-01 00:00 local ends the last quarter-hour of 2025-05-31; earlier quarter-hours are absent
    assert t["observed"].sum() == 0 and t.iloc[-1]["n_quarter_hours"] == 1
    t = pvdaq_target(qh, "2025-06-01", "2025-06-01", 892.8, "America/Los_Angeles", SITE)
    assert t.loc[t["observed"], "energy_kwh"].eq(100.0).all() and t["observed"].sum() == 23  # last hour needs 2025-06-02 00:00


def test_pvdaq_duplicates_units_and_outage():
    body = _fixture()
    lines = body.decode().splitlines()
    before = pvdaq_quarter_hours([body], COL, 892.8, "America/Los_Angeles").attrs["duplicates_dropped"]
    qh = pvdaq_quarter_hours([("\n".join(lines + lines[1:5]) + "\n").encode()], COL, 892.8, "America/Los_Angeles")
    assert qh.attrs["duplicates_dropped"] == before + 4
    bad = lines[1].rsplit(",", 1)[0] + ",999.0"
    with pytest.raises(ValueError, match="different values"):
        pvdaq_quarter_hours([("\n".join(lines + [bad]) + "\n").encode()], COL, 892.8, "America/Los_Angeles")
    meta = json.loads(json.dumps({"Metrics": {COL: {"units": "kwh", "aggregation_type": "sum"}}}))
    with pytest.raises(ValueError, match="expected kw avg"):
        parse_pvdaq_meta(meta, COL)
    zero = "measured_on,%s\n" % COL + "".join(
        f"2025-06-{d:02d} {h:02d}:{m:02d}:00,0.0\n" for d in (1, 2) for h in range(24) for m in (0, 15, 30, 45))
    t = pvdaq_target(pvdaq_quarter_hours([zero.encode()], COL, 892.8, "America/Los_Angeles"),
                     "2025-06-01", "2025-06-01", 892.8, "America/Los_Angeles", SITE)
    assert (t.loc[t["observed"], "exclusion"] == "outage_day").all() and not t["eligible"].any()
    assert t.loc[t["observed"], "energy_kwh"].eq(0.0).all()  # value kept, excluded with a reason
