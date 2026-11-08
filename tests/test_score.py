import json

import pandas as pd

from solar_forecast import issue as I
from solar_forecast import score as S

from conftest import elia_csv
from test_issue import TARGET, go, live_store

DAY = ("2026-11-06 23:00", "2026-11-07 23:00")  # the Brussels target day in UTC


def scored(store, tmp_path, now):
    return S.score("belgium", now, store.path, tmp_path / "forecasts", tmp_path / "scores")


def versions(tmp_path):
    return [json.loads(p.read_text()) for p in S._versions(tmp_path / "scores/belgium", f"{TARGET}_11h")]


def actuals(store, retrieved, dataset="ods210", day=DAY, request=("2026-11-01", "2026-11-20"), **kw):
    name = f"{dataset}_total"
    extra = {"start": request[0], "end": request[1]} if dataset == "ods209" else {}
    store("elia", name, "csv", elia_csv(*day, **kw), retrieved, dataset=dataset, **extra)


def test_provisional_then_versions_on_change_then_final_then_later_corrections(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    assert scored(store, tmp_path, "2026-11-06 22:00Z") == []  # target day not started
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    [p] = scored(store, tmp_path, "2026-11-08 07:00Z")
    v1 = json.loads(p.read_text())
    assert v1["status"] == "provisional" and v1["version"] == 1 and v1["previous"] is None
    assert v1["coverage"] == {**v1["coverage"], "hours": 24, "observed": 24, "scored": 24, "day_complete": True}
    assert v1["hours"][12]["actual_mwh"] == 100.0 and v1["hours"][12]["elia_mwh"] == 40.0  # dayahead11hforecast
    assert v1["metrics"]["forecast"]["complete_days"] == 1 and v1["metrics"]["elia"]["hours"] == 24
    assert v1["forecast"]["record"] == f"{TARGET}_11h.json"
    assert [r["retrieved_at"][:16] for r in v1["actuals"]["retrievals"]] == ["2026-11-08T06:00"]

    assert scored(store, tmp_path, "2026-11-08 08:00Z") == []  # unchanged: no new version
    actuals(store, "2026-11-09 06:00Z", measured=lambda t: 110.0)  # a correction
    scored(store, tmp_path, "2026-11-09 07:00Z")
    v2 = versions(tmp_path)[-1]
    assert v2["version"] == 2 and v2["previous"]["content_sha256"] == v1["content_sha256"]
    assert v2["hours"][12]["actual_mwh"] == 110.0 and versions(tmp_path)[0] == v1  # earlier version kept

    actuals(store, "2026-11-14 12:00Z", "ods209", measured=lambda t: 120.0)  # before day end + 7 days
    actuals(store, "2026-11-15 06:00Z", "ods209", measured=lambda t: 130.0)  # the first one after
    scored(store, tmp_path, "2026-11-15 07:00Z")
    v3 = versions(tmp_path)[-1]
    assert v3["status"] == "final" and v3["hours"][12]["actual_mwh"] == 130.0
    assert [r["retrieved_at"][:16] for r in v3["actuals"]["retrievals"]] == ["2026-11-15T06:00"]
    assert scored(store, tmp_path, "2026-11-15 08:00Z") == []

    actuals(store, "2026-11-17 06:00Z", "ods209", measured=lambda t: 150.0)  # a correction after finalization
    [p] = scored(store, tmp_path, "2026-11-17 07:00Z")
    v4 = json.loads(p.read_text())
    assert v4["status"] == "revised" and v4["version"] == 4 and v4["hours"][12]["actual_mwh"] == 150.0
    assert v4["previous"]["content_sha256"] == v3["content_sha256"] and versions(tmp_path)[2] == v3
    s = json.loads((tmp_path / "scores/belgium/summary.json").read_text())
    assert s["pools"]["11h_recorded_final"]["issuances"] == 1  # the final version, not the revision


def test_a_late_final_retrieval_and_a_later_one_in_the_same_run(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    actuals(store, "2026-11-15 06:00Z", "ods209", measured=lambda t: 130.0)
    actuals(store, "2026-11-16 06:00Z", "ods209", measured=lambda t: 140.0)
    vs = [json.loads(p.read_text()) for p in scored(store, tmp_path, "2026-11-16 07:00Z")]
    assert [(v["status"], v["hours"][12]["actual_mwh"]) for v in vs] == [("final", 130.0), ("revised", 140.0)]


def test_final_actuals_keep_the_captured_benchmark_and_cite_every_source(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    actuals(store, "2026-11-15 06:00Z", "ods209", measured=lambda t: 130.0, forecasts=("", ""))  # forecasts gone
    [v] = [json.loads(p.read_text()) for p in scored(store, tmp_path, "2026-11-15 07:00Z")]
    assert v["status"] == "final" and v["hours"][12]["actual_mwh"] == 130.0 and v["hours"][12]["elia_mwh"] == 40.0
    assert v["metrics"]["elia"]["hours"] == 24
    assert {(r["dataset"], tuple(r["fields"])) for r in v["actuals"]["retrievals"]} == {
        ("ods209", ("measured_mw", "capacity_mw")), ("ods210", ("elia_da11_mw",))}
    assert all(r["sidecar_sha256"] for r in v["actuals"]["retrievals"])


def test_final_retrieval_must_request_the_whole_local_day():
    exp = S.F.belgium()
    nov = {"start": "2026-11-01", "end": "2026-12-01"}
    assert not S._requests(nov, exp, "2026-11-01")  # the day starts 2026-10-31 23:00 UTC
    assert S._requests(nov, exp, "2026-11-02") and S._requests(nov, exp, "2026-11-30")
    assert not S._requests({"start": "2026-07-01", "end": "2026-08-01"}, exp, "2026-07-01")  # summer time: 22:00
    assert S._requests({"start": "2026-10-01", "end": "2026-11-01"}, exp, "2026-10-25")  # the 25-hour day


def test_a_missing_baseline_is_flagged_and_keeps_one_sample(store, tmp_path):
    live_store(store)
    rec_path = go(store, tmp_path)
    rec = json.loads(rec_path.read_text())
    rec["hours"][12]["physics_mwh"] = None
    rec_path.write_text(json.dumps(rec))
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    v = json.loads(scored(store, tmp_path, "2026-11-08 07:00Z")[0].read_text())
    assert v["coverage"]["scored"] == 23 and v["coverage"]["missing_predictions"]["physics"] == [rec["hours"][12]["target_hour_utc"]]
    assert {v["metrics"][p]["hours"] for p in S.REQUIRED} == {23}


def test_missing_quarter_hours_leave_the_hour_missing_and_the_day_incomplete(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    gap = pd.Timestamp("2026-11-07 11:15", tz="UTC")
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: None if t == gap else 100.0)
    v = json.loads(scored(store, tmp_path, "2026-11-08 07:00Z")[0].read_text())
    h = next(x for x in v["hours"] if x["target_hour_utc"].startswith("2026-11-07T11:00"))
    assert h["actual_mwh"] is None and h["n_quarter_hours"] == 3 and not h["scored"]
    assert v["coverage"]["scored"] == 23 and v["coverage"]["day_complete"] is False
    assert v["metrics"]["forecast"]["complete_days"] == 0 and v["metrics"]["forecast"]["daily_mae"] is None


def test_summary_pools_the_latest_versions_apart_by_weather_availability(store, tmp_path):
    live_store(store)  # the 06 UTC run is missing, so the 18h issuance falls back
    go(store, tmp_path)
    issue18 = S.F.issuance_time(S.F.belgium(), TARGET, "18h")
    I.issue("belgium", "18h", TARGET, issue18 + pd.Timedelta(minutes=1), store.path, tmp_path / "forecasts")
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    scored(store, tmp_path, "2026-11-08 07:00Z")
    actuals(store, "2026-11-09 06:00Z", measured=lambda t: 110.0)
    scored(store, tmp_path, "2026-11-09 07:00Z")
    s = json.loads((tmp_path / "scores/belgium/summary.json").read_text())
    assert set(s["pools"]) == {"11h_recorded_all", "18h_fallback_all"}
    assert s["pools"]["11h_recorded_all"]["issuances"] == 1 and s["pools"]["11h_recorded_all"]["scored_hours"] == 24
    assert s["pools"]["11h_recorded_all"]["metrics"]["persistence"]["hours"] == 24


def test_a_revision_needs_a_retrieval_after_the_final_one(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    gap = pd.Timestamp("2026-11-07 11:15", tz="UTC")
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 122.5)
    actuals(store, "2026-11-15 06:00Z", "ods209", measured=lambda t: None if t == gap else 130.0)
    vs = [json.loads(p.read_text()) for p in scored(store, tmp_path, "2026-11-15 07:00Z")]
    assert [v["status"] for v in vs] == ["final"]  # the older retrieval does not fill the final gap
    h = next(x for x in vs[0]["hours"] if x["target_hour_utc"].startswith("2026-11-07T11:00"))
    assert h["actual_mwh"] is None and h["n_quarter_hours"] == 3
    assert vs[0]["actuals"]["final_retrieval"].startswith("ods209")
    actuals(store, "2026-11-16 06:00Z", measured=lambda t: 125.0)  # a later observation fills it
    [p] = scored(store, tmp_path, "2026-11-16 07:00Z")
    v = json.loads(p.read_text())
    h = next(x for x in v["hours"] if x["target_hour_utc"].startswith("2026-11-07T11:00"))
    assert v["status"] == "revised" and h["actual_mwh"] == (130.0 * 3 + 125.0) / 4
    assert v["hours"][0]["actual_mwh"] == 130.0  # ods209 still wins where it has a value


def test_a_snapshot_found_later_with_an_earlier_retrieval_time_is_scored_like_a_rebuild(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    scored(store, tmp_path, "2026-11-08 07:00Z")
    actuals(store, "2026-11-08 05:00Z", "ods209", measured=lambda t: 150.0)  # imported later, retrieved earlier
    [p] = scored(store, tmp_path, "2026-11-08 08:00Z")
    assert json.loads(p.read_text())["hours"][12]["actual_mwh"] == 150.0
    (tmp_path / "scores/belgium/retrievals.json").unlink()  # a full rebuild agrees
    assert scored(store, tmp_path, "2026-11-08 09:00Z") == []


def test_monthly_reconciliation_finalizes_the_first_of_the_month(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    nov1 = ("2026-10-31 23:00", "2026-11-01 23:00")
    assert S.day_bounds(S.F.belgium(), "2026-11-01") == tuple(pd.Timestamp(t, tz="UTC") for t in nov1)
    rec = json.loads((tmp_path / f"forecasts/belgium/{TARGET}_11h.json").read_text())
    p = tmp_path / "forecasts/belgium/2026-11-01_11h.json"
    p.write_text(json.dumps({**rec, "target_date": "2026-11-01", "hours": [
        {**h, "target_hour_utc": (pd.Timestamp(h["target_hour_utc"]) - pd.Timedelta(days=6)).isoformat()} for h in rec["hours"]]}))
    a, b = [tuple(map(str, m)) for m in S.sources.elia_months("2026-11-20")]  # October, November
    actuals(store, "2026-11-20 06:00Z", "ods209", day=("2026-10-30", "2026-11-01"), request=a, measured=lambda t: 70.0)
    actuals(store, "2026-11-20 06:01Z", "ods209", day=("2026-10-31", "2026-11-20"), request=b, measured=lambda t: 70.0)
    out = [json.loads(x.read_text()) for x in scored(store, tmp_path, "2026-11-20 07:00Z") if "2026-11-01" in x.name]
    assert [v["status"] for v in out] == ["final"] and out[0]["coverage"]["observed"] == 24
    [r] = [r for r in out[0]["actuals"]["retrievals"] if "measured_mw" in r["fields"]]
    assert r["sidecar"] == out[0]["actuals"]["final_retrieval"] and r["retrieved_at"].startswith("2026-11-20T06:01")
