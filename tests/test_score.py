import json

import pandas as pd

from solar_forecast import score as S

from conftest import elia_csv
from test_issue import ISSUANCE, TARGET, go, live_store

DAY = ("2026-11-06 23:00", "2026-11-07 23:00")  # the Brussels target day in UTC


def scored(store, tmp_path, now):
    return S.score("belgium", now, store.path, tmp_path / "forecasts", tmp_path / "scores")


def versions(tmp_path):
    return [json.loads(p.read_text()) for p in S._versions(tmp_path / "scores/belgium", f"{TARGET}_11h")]


def actuals(store, retrieved, dataset="ods210", **kw):
    name = f"{dataset}_total"
    extra = {"start": "2026-11-01", "end": "2026-11-20"} if dataset == "ods209" else {}
    store("elia", name, "csv", elia_csv(*DAY, **kw), retrieved, dataset=dataset, **extra)


def test_provisional_then_versions_on_change_then_final_from_one_retrieval(store, tmp_path):
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
    actuals(store, "2026-11-16 06:00Z", "ods209", measured=lambda t: 140.0)
    scored(store, tmp_path, "2026-11-16 07:00Z")
    v3 = versions(tmp_path)[-1]
    assert v3["status"] == "final" and v3["hours"][12]["actual_mwh"] == 130.0
    assert [r["retrieved_at"][:16] for r in v3["actuals"]["retrievals"]] == ["2026-11-15T06:00"]
    actuals(store, "2026-11-17 06:00Z", "ods209", measured=lambda t: 150.0)
    assert scored(store, tmp_path, "2026-11-17 07:00Z") == []  # final is not rescored


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


def test_summary_pools_the_latest_versions(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    scored(store, tmp_path, "2026-11-08 07:00Z")
    actuals(store, "2026-11-09 06:00Z", measured=lambda t: 110.0)
    scored(store, tmp_path, "2026-11-09 07:00Z")
    s = json.loads((tmp_path / "scores/belgium/summary.json").read_text())
    assert s["pools"]["11h_all"]["issuances"] == 1 and s["pools"]["11h_all"]["scored_hours"] == 24
    assert s["pools"]["11h_final"]["issuances"] == 0
    assert s["pools"]["11h_all"]["metrics"]["persistence"]["hours"] == 24
