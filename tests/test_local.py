import json

import numpy as np
import pandas as pd

from solar_forecast import baselines as B
from solar_forecast import local as L
from solar_forecast import sources
from solar_forecast.weather import day_hours, hour_weather

from conftest import PROVINCES, run_json

NOW = pd.Timestamp("2026-11-07 10:40Z")  # after the 11:00 issuance for 2026-11-08, whose run is 2026-11-07 00 UTC
POINTS = sources.config("local")["grid"]["points"]


def fake_fetch(calls):
    def fetch(run, store):
        calls.append(run)
        out = []
        for i in range(0, len(POINTS), L.CHUNK):
            n = len(POINTS[i:i + L.CHUNK])
            out.append(sources.save(L.SOURCE, f"{run:%Y%m%dT%H}_{i // L.CHUNK}", "json", run_json(run, points=n),
                                    {"retrieved_at": "2026-11-07T10:35:00+00:00", "url": "test", "run": run.isoformat(), "points": n}, store))
        return out
    return fetch


def province_csv(start: str, end: str) -> bytes:
    """ods210-format quarter-hours: every province measures 5 % of its capacity and Elia forecast 4 % (11:00)."""
    rows = [[t.isoformat(), "PT15M", p["ods209"], 0.05 * cap, cap, 0.04 * cap, 0.03 * cap]
            for t in pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
            for cap, p in ((100.0 * (i + 1), p) for i, p in enumerate(PROVINCES))]
    return pd.DataFrame(rows, columns=["datetime", "resolutioncode", "region", "measured", "monitoredcapacity",
                                       "dayahead11hforecast", "dayaheadforecast"]).to_csv(index=False).encode()


def test_energy_is_the_physics_reference_per_kwp():
    run = pd.Timestamp("2026-06-20", tz="UTC")
    hours = day_hours("2026-06-21", "Europe/Brussels")
    weather = sources.parse_run(run_json(run, points=2), 2)
    p = B.CONFIG["physics"]["belgium"]
    geometries = [{"tilt_deg": 35, "azimuth_deg": 180}, {"tilt_deg": 15, "azimuth_deg": 90}]
    wh = L.energy(weather, hours, POINTS[:2], geometries, p)
    assert wh.shape == (2, 2, 24)
    w = hour_weather(weather, hours).loc[1]
    lat, lon, _ = POINTS[1]
    expected = B.dc_per_kw(hours, lat, lon, w["shortwave_radiation"], w["direct_normal_irradiance"], w["diffuse_radiation"],
                           w["temperature_2m"], w["wind_speed_10m"], p | geometries[1]) * p["performance_ratio"] * 1000
    assert np.allclose(wh[1, 1], expected)


def test_one_record_per_issuance_and_the_public_file_beside_it(store, tmp_path):
    calls = []
    store("elia", "ods210_total", "csv", province_csv("2026-10-28", "2026-11-09"), "2026-11-07 10:30Z", dataset="ods210")
    path = L.run("belgium", NOW, store.path, tmp_path / "local", tmp_path / "feed", fake_fetch(calls))
    rec = json.loads((tmp_path / "local/belgium/2026-11-08_11h.json").read_text())
    assert calls == [pd.Timestamp("2026-11-07", tz="UTC")]
    assert rec["weather"]["run_utc"] == "2026-11-07T00:00:00+00:00" and len(rec["weather"]["snapshots"]) == 2
    assert len(rec["cells"]) == len(POINTS) and rec["geometries"][rec["reference"]] == {"tilt_deg": 35, "azimuth_deg": 180}
    assert all(len(c["wh"]) == len(rec["geometries"]) and len(c["wh"][0]) == len(rec["hours"]) for c in rec["cells"])
    assert {p["id"] for p in rec["provinces"]} == {p["ods209"] for p in PROVINCES}

    feed = json.loads(path.read_text())
    assert path.name == "belgium-local.json" and feed["schema"] == "kaporli.solar-forecast.local" and feed["version"] == 1
    assert "snapshots" not in feed["weather"] and feed["target_date"] == "2026-11-08" and feed["kind"] == "11h"
    antwerp = feed["provinces"][0]
    assert antwerp["id"] == "Antwerpen" and antwerp["capacity_mw"] == 100.0
    assert set(antwerp["elia_wh"]) == {40}  # 4 % of capacity every hour: 40 Wh per kW
    assert [d["date"] for d in antwerp["recent"]] == [f"2026-10-31", *[f"2026-11-0{d}" for d in range(1, 7)]]
    assert {d["measured_kwh"] for d in antwerp["recent"]} == {1.2}  # 24 h at 5 % of capacity
    assert {d["elia_kwh"] for d in antwerp["recent"]} == {0.96} and {d["physics_kwh"] for d in antwerp["recent"]} == {None}

    L.run("belgium", NOW + pd.Timedelta(hours=1), store.path, tmp_path / "local", tmp_path / "feed", fake_fetch(calls))
    assert len(calls) == 1  # the record exists: nothing is fetched or recomputed

    # the next day's file lists this record's reference total for 2026-11-08
    later = pd.Timestamp("2026-11-09 08:00Z")  # before the 11:00 issuance: the 18:00 issuance of the 8th is the latest due
    feed = json.loads(L.run("belgium", later, store.path, tmp_path / "local", tmp_path / "feed", fake_fetch(calls)).read_text())
    assert feed["target_date"] == "2026-11-09" and feed["kind"] == "18h"
    day = next(d for d in feed["provinces"][0]["recent"] if d["date"] == "2026-11-08")
    assert day["physics_kwh"] == round(sum(rec["provinces"][0]["wh"]) / 1000, 3)


def test_without_elia_snapshots_the_forecast_is_still_published(store, tmp_path):
    feed = json.loads(L.run("belgium", NOW, store.path, tmp_path / "local", tmp_path / "feed", fake_fetch([])).read_text())
    assert len(feed["cells"]) == len(POINTS)
    assert feed["provinces"][0]["capacity_mw"] is None and feed["provinces"][0]["recent"] == []
