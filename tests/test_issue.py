import json

import pandas as pd
import pytest

from solar_forecast import features as F
from solar_forecast import index, sources
from solar_forecast import issue as I
from solar_forecast.issue import issue

from conftest import elia_csv, run_json

TARGET = "2026-11-07"
ISSUANCE = F.issuance_time(F.belgium(), TARGET, "11h")  # 2026-11-06 10:00 UTC


def live_store(store, runs=("2026-11-06",), elia_retrieved="2026-11-06 09:35Z", hours=72, elia=True):
    if elia:
        store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-06 09:30"), elia_retrieved, dataset="ods210")
    for day in runs:
        run = pd.Timestamp(day, tz="UTC")
        store("weather-belgium", run.strftime("%Y%m%dT%H"), "json", run_json(run, hours=hours), f"{day} 08:00Z",
              run=run.isoformat(), params={"forecast_hours": hours})


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


def test_an_interrupted_write_leaves_no_record_and_a_retry_issues(store, tmp_path, monkeypatch):
    live_store(store)
    real = I.os.fsync
    def boom(fd):
        raise OSError("disk full")
    monkeypatch.setattr(I.os, "fsync", boom)
    with pytest.raises(OSError):
        go(store, tmp_path)
    monkeypatch.setattr(I.os, "fsync", real)
    out = tmp_path / "forecasts/belgium"
    assert not (out / f"{TARGET}_11h.json").exists() and not list(out.glob(".*.tmp"))
    assert json.loads(go(store, tmp_path).read_text())["status"] == "issued"


def test_a_serialization_failure_is_recorded(store, tmp_path, monkeypatch):
    live_store(store)
    monkeypatch.setattr(I, "_num", lambda x: float("nan"))  # NaN is not valid JSON
    with pytest.raises(ValueError, match="JSON"):
        go(store, tmp_path)
    out = tmp_path / "forecasts/belgium"
    assert not (out / f"{TARGET}_11h.json").exists() and len(list(out.glob(f"{TARGET}_11h.failed-*.json"))) == 1


def test_features_equal_full_history_when_older_retrievals_hold_values_newer_ones_lack(store, tmp_path):
    # the review's case: the newest retrievals lack November 2, which only an older one holds, so a three-day
    # retrieval window falls back to November 1
    live_store(store, elia=False)
    store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-03", measured=lambda t: 60.0),
          "2026-11-02 23:00Z", dataset="ods210")
    store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-06 09:30",
          measured=lambda t: 50.0 if t < pd.Timestamp("2026-11-01 23:00", tz="UTC") else None), "2026-11-06 09:35Z", dataset="ods210")
    rec = json.loads(go(store, tmp_path).read_text())
    exp = F.belgium()
    elia = [m for m in sources.snapshots("elia", store.path) if F._ts(m["retrieved_at"]) <= ISSUANCE]
    weather = sources.snapshots("weather-belgium", store.path)
    from solar_forecast.cli import content_hash
    full = F.features_for_issuance(exp, F.BelgiumInputs(elia, weather, exp), TARGET, "11h", live=True)
    assert rec["features_sha256"] == content_hash(full)
    assert any(s["retrieved_at"].startswith("2026-11-02") for s in rec["inputs"]["snapshots"])


def test_only_retrievals_reaching_the_window_are_read(store):
    store("elia", "ods210_total", "csv", elia_csv("2026-09-01", "2026-09-10"), "2026-09-10 00:00Z", dataset="ods210")
    store("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-03"), "2026-11-03 00:00Z", dataset="ods210")
    metas = sources.snapshots("elia", store.path)
    con = index.connect(index.path_for(metas))
    assert len(index.reaching(con, metas, "2026-10-26")) == 2  # not indexed yet: both
    index.ingest(con, metas)
    assert [m["retrieved_at"][:10] for m in index.reaching(con, metas, "2026-10-26")] == ["2026-11-03"]


def test_an_incomplete_prescribed_run_is_not_used(store, tmp_path):
    live_store(store, runs=("2026-11-05",))
    run = pd.Timestamp("2026-11-06", tz="UTC")
    store("weather-belgium", "20261106T00", "json", run_json(run, hours=24), "2026-11-06 08:00Z",
          run=run.isoformat(), params={"forecast_hours": 72})
    rec = json.loads(go(store, tmp_path).read_text())
    assert rec["inputs"]["weather_availability"] == "fallback"


def test_a_late_execution_is_not_issued(store, tmp_path):
    live_store(store)
    with pytest.raises(I.Late):
        go(store, tmp_path, now=ISSUANCE + I.LATE + pd.Timedelta(seconds=1))
    out = tmp_path / "forecasts/belgium"
    [fail] = list(out.glob(f"{TARGET}_11h.failed-*.json"))
    assert not (out / f"{TARGET}_11h.json").exists() and json.loads(fail.read_text())["status"] == "late"
    rec = json.loads(go(store, tmp_path, now=ISSUANCE + I.LATE - pd.Timedelta(minutes=5)).read_text())
    assert pd.Timestamp(rec["computed_at"]) <= pd.Timestamp(rec["deadline_utc"])


def test_failures_in_the_same_second_keep_separate_records(store, tmp_path):
    live_store(store, runs=())
    for _ in range(2):
        with pytest.raises(ValueError):
            go(store, tmp_path)
    fails = list((tmp_path / "forecasts/belgium").glob(f"{TARGET}_11h.failed-*.json"))
    assert len(fails) == 2 and len({json.loads(f.read_text())["attempt"] for f in fails}) == 2


def test_a_slow_flush_past_the_deadline_is_late(store, tmp_path, monkeypatch):
    live_store(store)
    clock = [0.0]
    monkeypatch.setattr(I.time, "monotonic", lambda: clock[0])
    real = I.os.fsync
    def slow(fd):
        clock[0] += 120
        real(fd)
    monkeypatch.setattr(I.os, "fsync", slow)
    with pytest.raises(I.Late, match="ready to publish"):
        go(store, tmp_path, now=ISSUANCE + I.LATE - pd.Timedelta(seconds=60))
    out = tmp_path / "forecasts/belgium"
    [fail] = list(out.glob(f"{TARGET}_11h.failed-*.json"))
    assert not (out / f"{TARGET}_11h.json").exists() and json.loads(fail.read_text())["status"] == "late"


def test_publication_time_is_recorded_after_the_computation(store, tmp_path):
    live_store(store)
    p = go(store, tmp_path)
    pub = json.loads(p.with_name(f"{TARGET}_11h.published.json").read_text())
    rec = json.loads(p.read_text())
    assert pub["attempt"] == rec["attempt"] and pub["durable"] is True
    assert rec["computed_at"] <= pub["published_at"] <= pub["deadline_utc"]


def test_a_sync_failure_after_the_commit_keeps_the_forecast(store, tmp_path, monkeypatch):
    live_store(store)
    real, calls = I._sync_dir, []
    def once(d):
        calls.append(d)
        if len(calls) == 1:
            raise OSError("sync failed")
        real(d)
    monkeypatch.setattr(I, "_sync_dir", once)
    with pytest.raises(SystemExit, match="sync error: OSError"):
        go(store, tmp_path)
    out = tmp_path / "forecasts/belgium"
    assert json.loads((out / f"{TARGET}_11h.json").read_text())["status"] == "issued"
    assert not list(out.glob(f"{TARGET}_11h.failed-*.json"))
    pub = json.loads((out / f"{TARGET}_11h.published.json").read_text())
    assert pub["durable"] is False and "sync failed" in pub["sync_error"]


def test_a_cleanup_failure_after_the_commit_keeps_the_forecast(store, tmp_path, monkeypatch):
    live_store(store)
    real = I.Path.unlink
    def unlink(self, missing_ok=False):
        if self.name.startswith(f".{TARGET}_11h.json.") and (self.parent / f"{TARGET}_11h.json").exists():
            raise OSError("cleanup failed")
        real(self, missing_ok=missing_ok)
    monkeypatch.setattr(I.Path, "unlink", unlink)
    with pytest.raises(SystemExit, match="cleanup error: OSError"):
        go(store, tmp_path)
    out = tmp_path / "forecasts/belgium"
    assert json.loads((out / f"{TARGET}_11h.json").read_text())["status"] == "issued"
    assert not list(out.glob(f"{TARGET}_11h.failed-*.json"))
    pub = json.loads((out / f"{TARGET}_11h.published.json").read_text())
    assert pub["durable"] is True and "cleanup failed" in pub["cleanup_error"]
