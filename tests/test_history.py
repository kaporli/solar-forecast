import json

import pandas as pd

from solar_forecast import history as H

from test_issue import TARGET, go, live_store
from test_score import actuals, scored


def build(tmp_path, now="2026-11-08 08:00Z"):
    return H.build("belgium", pd.Timestamp(now), tmp_path / "forecasts", tmp_path / "scores")


def test_an_issuance_before_scoring_has_no_actuals(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    h = build(tmp_path)
    assert (h["schema"], h["version"]) == (H.SCHEMA, 1)
    [e] = h["issuances"]
    assert (e["kind"], e["target_date"], e["status"], e["score"]) == ("11h", TARGET, "issued", None)
    assert len(e["hours"]) == 24 and e["hours"][12]["forecast_mwh"] is not None
    assert all(x["actual_mwh"] is None and x["elia_mwh"] is None and x["scored"] is False for x in e["hours"])
    assert [x["daylight"] for x in e["hours"]][:7] == [False] * 7 and e["hours"][12]["daylight"]


def test_the_latest_score_version_is_joined(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    actuals(store, "2026-11-08 06:00Z", measured=lambda t: 100.0)
    scored(store, tmp_path, "2026-11-08 07:00Z")
    actuals(store, "2026-11-09 06:00Z", measured=lambda t: 110.0)  # a correction: version 2
    scored(store, tmp_path, "2026-11-09 07:00Z")
    [e] = build(tmp_path, "2026-11-09 08:00Z")["issuances"]
    assert e["score"]["version"] == 2 and e["score"]["status"] == "provisional"
    assert e["score"]["coverage"] == {"hours": 24, "observed": 24, "scored": 24, "day_complete": True}
    assert e["hours"][12]["actual_mwh"] == 110.0 and e["hours"][12]["elia_mwh"] == 40.0 and e["hours"][12]["scored"]
    assert e["score"]["metrics"]["forecast"]["hours"] == 24


def test_a_failed_issuance_is_listed_without_a_forecast_and_late_wins(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    d = tmp_path / "forecasts/belgium"
    (d / "2026-11-08_11h.failed-a.json").write_text(json.dumps({"status": "failed"}))
    (d / "2026-11-08_11h.failed-b.json").write_text(json.dumps({"status": "late"}))
    (d / f"{TARGET}_11h.failed-c.json").write_text(json.dumps({"status": "failed"}))  # retried and issued
    es = build(tmp_path)["issuances"]
    assert [(e["target_date"], e["status"]) for e in es] == [(TARGET, "issued"), ("2026-11-08", "late")]
    assert es[1]["hours"] == [] and es[1]["issuance_utc"] == "2026-11-07T10:00:00+00:00"


def test_the_file_holds_no_private_values(store, tmp_path):
    live_store(store)
    go(store, tmp_path)
    p = H.write("belgium", pd.Timestamp("2026-11-08 08:00Z"), tmp_path / "forecasts", tmp_path / "scores",
                tmp_path / "feed")
    text = p.read_text()
    assert p.name == "belgium-history.json" and json.loads(text)["issuances"]
    for private in ("attempt", "sidecar", "snapshot", str(tmp_path), "traceback", "error"):
        assert private not in text
