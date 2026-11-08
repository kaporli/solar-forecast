import json

import pandas as pd

from solar_forecast import feed as FD

from test_issue import ISSUANCE, TARGET, go, live_store

NEXT = FD.F.issuance_time(FD.F.belgium(), "2026-11-08", "11h")  # 2026-11-07 10:00 UTC


def records(store, tmp_path, availability="recorded"):
    """The 2026-11-07 11h record."""
    live_store(store)
    p = go(store, tmp_path)
    r = json.loads(p.read_text())
    r["inputs"]["weather_availability"] = availability
    p.write_text(json.dumps(r))
    return tmp_path / "forecasts"


def next_record(forecasts):
    """A copy of it as the 2026-11-08 one."""
    p = forecasts / f"belgium/{TARGET}_11h.json"
    r = json.loads(p.read_text()) | {"target_date": "2026-11-08", "issuance_utc": NEXT.isoformat()}
    p.with_name("2026-11-08_11h.json").write_text(json.dumps(r))


def state(forecasts, now):
    e = FD.build("belgium", pd.Timestamp(now), forecasts)["issuances"]["11h"]
    return e["state"], e["reason"], e["forecast"] and e["forecast"]["target_date"], e["valid_until"]


def test_fresh_then_kept_until_the_next_deadline(store, tmp_path):
    f = records(store, tmp_path)
    assert state(f, ISSUANCE + pd.Timedelta(minutes=2)) == ("fresh", None, TARGET, (NEXT + FD.LATE).isoformat())
    assert state(f, NEXT + pd.Timedelta(minutes=5))[:3] == ("fresh", None, TARGET)  # the next one is not due yet
    next_record(f)
    assert state(f, NEXT + pd.Timedelta(minutes=5))[:3] == ("fresh", None, "2026-11-08")


def test_a_fallback_weather_run_is_its_own_state(store, tmp_path):
    f = records(store, tmp_path, "fallback")
    assert state(f, ISSUANCE + pd.Timedelta(minutes=2))[0] == "fallback"


def test_failed_late_and_missing_issuances_keep_the_previous_forecast_as_stale(store, tmp_path):
    f = records(store, tmp_path)
    fail = f / "belgium/2026-11-08_11h.failed-x.json"
    fail.write_text(json.dumps({"status": "failed"}))
    assert state(f, NEXT + pd.Timedelta(minutes=2)) == ("stale", "failed", TARGET, None)  # before the deadline
    (f / "belgium/2026-11-08_11h.failed-y.json").write_text(json.dumps({"status": "late"}))
    assert state(f, NEXT + pd.Timedelta(minutes=40))[:2] == ("stale", "late")
    for p in (f / "belgium").glob("*.failed-*.json"):
        p.unlink()
    assert state(f, NEXT + pd.Timedelta(minutes=31))[:3] == ("stale", "missing", TARGET)  # past the deadline
    assert state(f, "2026-11-07 23:00Z") == ("unavailable", "missing", None, None)  # its target day is over


def test_no_records_is_unavailable_and_the_file_is_replaced_whole(store, tmp_path):
    out = tmp_path / "feed"
    p = FD.write("belgium", ISSUANCE, tmp_path / "forecasts", out)
    f = json.loads(p.read_text())
    assert {k: e["state"] for k, e in f["issuances"].items()} == {"11h": "unavailable", "18h": "unavailable"}
    assert f["latest"] is None and f["schema"] == FD.SCHEMA and f["version"] == 1
    records(store, tmp_path)
    FD.write("belgium", ISSUANCE + pd.Timedelta(minutes=2), tmp_path / "forecasts", out)
    assert [x.name for x in out.iterdir()] == ["belgium.json"]  # no temporary file left


def test_the_payload_holds_only_public_fields(store, tmp_path):
    f = FD.build("belgium", ISSUANCE + pd.Timedelta(minutes=2), records(store, tmp_path))
    text = json.dumps(f)
    assert f["latest"] == "11h" and len(f["issuances"]["11h"]["forecast"]["hours"]) == 24
    for word in ("sidecar", "snapshots", "/srv", "/Users", str(tmp_path), "attempt", "traceback"):
        assert word not in text
    h = f["issuances"]["11h"]["forecast"]["hours"][0]
    assert pd.Timestamp(h["end_utc"]) - pd.Timestamp(h["start_utc"]) == pd.Timedelta(hours=1)


def test_fixtures_match_the_contract():
    from pathlib import Path
    root = Path(__file__).parents[1] / "fixtures/feed"
    seen = set()
    for p in sorted(root.glob("belgium-*.json")):
        f = json.loads(p.read_text())
        assert f["schema"] == FD.SCHEMA and f["version"] == FD.VERSION and set(f["issuances"]) == set(FD.KINDS)
        seen |= {e["state"] for e in f["issuances"].values()}
    assert seen == {"fresh", "fallback", "stale", "unavailable"}


def test_hours_are_counted_from_issuance_and_from_the_weather_run(store, tmp_path):
    e = FD.build("belgium", ISSUANCE + pd.Timedelta(minutes=2), records(store, tmp_path))["issuances"]["11h"]["forecast"]
    first, last = e["hours"][0], e["hours"][-1]
    assert first["start_utc"] == "2026-11-06T23:00:00+00:00" and first["hours_after_issuance"] == 13.0  # 10:00 UTC issuance
    assert first["weather_lead_hours"] == 23.0  # the 00 UTC run of 2026-11-06
    assert last["hours_after_issuance"] == 36.0 and e["end_utc"] == "2026-11-07T23:00:00+00:00"


def test_fixtures_count_hours_from_their_own_issuance_and_run():
    from pathlib import Path
    for p in (Path(__file__).parents[1] / "fixtures/feed").glob("belgium-*.json"):
        for e in json.loads(p.read_text())["issuances"].values():
            if f := e["forecast"]:
                for h in f["hours"]:
                    start = pd.Timestamp(h["start_utc"])
                    assert h["hours_after_issuance"] == (start - pd.Timestamp(f["issuance_utc"])) / pd.Timedelta(hours=1)
                    assert h["weather_lead_hours"] == (start - pd.Timestamp(f["weather"]["run_utc"])) / pd.Timedelta(hours=1)


def test_a_saved_feed_ages_like_a_regenerated_one(store, tmp_path):
    f = records(store, tmp_path)
    (f / "belgium/2026-11-08_11h.failed-x.json").write_text(json.dumps({"status": "failed"}))
    saved = FD.build("belgium", NEXT + pd.Timedelta(minutes=2), f)  # 11h stale, showing 2026-11-07
    assert saved["issuances"]["11h"]["state"] == "stale" and saved["latest"] == "11h"
    for now in ("2026-11-07 18:00Z", "2026-11-07 23:00Z", "2026-11-08 03:00Z"):  # the evening-to-morning gap
        aged, fresh = FD.age(saved, now), FD.build("belgium", pd.Timestamp(now), f)
        assert aged["issuances"]["11h"]["state"] == fresh["issuances"]["11h"]["state"]
        assert aged["latest"] == fresh["latest"]
    assert FD.age(saved, "2026-11-07 23:00Z")["issuances"]["11h"]["forecast"] is None


def test_a_saved_fresh_feed_goes_stale_after_valid_until(store, tmp_path):
    saved = FD.build("belgium", ISSUANCE + pd.Timedelta(minutes=2), records(store, tmp_path))
    e = FD.age(saved, NEXT + FD.LATE)["issuances"]["11h"]
    assert e["state"] == "fresh"
    e = FD.age(saved, NEXT + FD.LATE + pd.Timedelta(seconds=1))["issuances"]["11h"]
    assert (e["state"], e["reason"], e["forecast"]["target_date"]) == ("stale", "expired", TARGET)


def test_a_scoring_failure_still_refreshes_the_feed(monkeypatch, tmp_path):
    import pytest
    from solar_forecast import cli, score
    calls = []
    def boom(name):
        raise RuntimeError("scoring broke")
    monkeypatch.setattr(score, "score", boom)
    monkeypatch.setattr(cli, "_feed", lambda name: calls.append(name) or True)
    with pytest.raises(RuntimeError, match="scoring broke"):
        cli.main(["score", "belgium"])
    assert calls == ["belgium"]
