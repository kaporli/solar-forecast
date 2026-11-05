import json

import pandas as pd
import pytest

from solar_forecast import features as F
from solar_forecast.issue import issue

from conftest import elia_csv, run_json

TARGET = "2026-11-07"
ISSUANCE = F.issuance_time(F.belgium(), TARGET, "11h")  # 2026-11-06 10:00 UTC


def live_store(store, runs=("2026-11-06",), elia_retrieved="2026-11-06 09:35Z", hours=72):
    store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-06 09:30"), elia_retrieved, dataset="ods210")
    for day in runs:
        run = pd.Timestamp(day, tz="UTC")
        store("weather-belgium", run.strftime("%Y%m%dT%H"), "json", run_json(run, hours=hours), f"{day} 08:00Z", run=run.isoformat())


def go(store, tmp_path, now=ISSUANCE + pd.Timedelta(minutes=1)):
    return issue("belgium", "11h", TARGET, now, store.path, tmp_path / "forecasts")


def test_issues_once_and_a_retry_cannot_replace_it(store, tmp_path):
    live_store(store)
    p = go(store, tmp_path)
    rec = json.loads(p.read_text())
    assert rec["status"] == "issued" and rec["issuance_utc"] == ISSUANCE.isoformat() and len(rec["hours"]) == 24
    assert all(h["forecast_mwh"] is not None and h["persistence_mwh"] is not None for h in rec["hours"])
    assert rec["inputs"]["weather_availability"] == "recorded" and rec["model"]["model_sha256"]
    before = p.read_bytes()
    with pytest.raises(SystemExit, match="already issued"):
        go(store, tmp_path)
    assert p.read_bytes() == before


def test_refuses_before_the_issuance_time(store, tmp_path):
    live_store(store)
    with pytest.raises(SystemExit, match="has not passed"):
        go(store, tmp_path, now=ISSUANCE - pd.Timedelta(minutes=1))


def test_snapshots_retrieved_after_the_issuance_are_not_used(store, tmp_path):
    live_store(store)
    store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-06 10:30", measured=lambda t: 90.0),
          "2026-11-06 10:40Z", dataset="ods210")
    rec = json.loads(go(store, tmp_path).read_text())
    assert [s["retrieved_at"] for s in rec["inputs"]["snapshots"] if s["source"] == "elia"] == ["2026-11-06T09:35:00+00:00"]


def test_late_weather_run_falls_back_to_the_previous_run_labelled(store, tmp_path):
    live_store(store, runs=("2026-11-05",))
    rec = json.loads(go(store, tmp_path).read_text())
    assert rec["inputs"]["weather_availability"] == "fallback"
    assert rec["inputs"]["weather_run_utc"] == "2026-11-05T00:00:00+00:00"


def test_a_fallback_run_too_short_for_the_target_day_fails(store, tmp_path):
    live_store(store, runs=("2026-11-05",), hours=49)  # the archive length does not reach the target day
    with pytest.raises(ValueError, match="non-finite inputs"):
        go(store, tmp_path)


def test_failure_writes_a_failure_record_and_no_forecast(store, tmp_path):
    live_store(store, runs=())
    with pytest.raises(ValueError, match="non-finite inputs"):
        go(store, tmp_path)
    out = tmp_path / "forecasts/belgium"
    assert not (out / f"{TARGET}_11h.json").exists()
    [fail] = list(out.glob(f"{TARGET}_11h.failed-*.json"))
    assert json.loads(fail.read_text())["status"] == "failed"
