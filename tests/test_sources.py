import gzip
import json
import uuid

import pandas as pd
import pytest

from solar_forecast import sources
from solar_forecast.sources import ROOT

from conftest import elia_csv, run_json


D = lambda x: pd.Timestamp(x).date()


def test_ods209_decimal_comma_and_region_names():
    body = ("datetime,region,gridconnectiontype,measured_upscaled,monitoredcapacity\n"
            "2026-10-26T10:00:00+00:00,Antwerpen,Total,\"1234,5\",900\n"
            "2026-10-26T10:00:00+00:00,Belgium,Total,,12000\n").encode()
    f = sources.parse_elia(body, "ods209")
    assert f.loc[f["region"] == "Antwerpen", "measured_mw"].item() == 1234.5
    assert f.loc[f["region"] == "Belgium", "measured_mw"].isna().item()  # missing stays missing


def test_ods032_names_map_to_ods209():
    f = sources.parse_elia(elia_csv("2024-03-10", "2024-03-10 00:15"), "ods032")
    assert set(f["region"]) == {"Belgium"} | {p["ods209"] for p in sources.config("belgium")["weather_points"]["provinces"]}


def test_duplicates_identical_dropped_conflicting_raise():
    body = elia_csv("2024-03-10", "2024-03-10 00:15")
    lines = body.decode().splitlines()
    f = sources.parse_elia(("\n".join(lines + [lines[1]]) + "\n").encode(), "ods032")
    assert f.attrs["duplicates_dropped"] == 1
    with pytest.raises(ValueError, match="different values"):
        sources.parse_elia(("\n".join(lines + [lines[1].replace(",50.0,", ",51.0,")]) + "\n").encode(), "ods032")


@pytest.mark.parametrize("change, match", [
    ((",PT15M,", ",PT60M,"), "resolution"),
    (("T00:00:00+00:00", "T00:07:00+00:00"), "grid"),
    (("monitoredcapacity", "capacity"), "missing columns"),
])
def test_elia_schema_resolution_and_timestamps(change, match):
    with pytest.raises(ValueError, match=match):
        sources.parse_elia(elia_csv("2024-03-10", "2024-03-10 00:15").decode().replace(*change).encode(), "ods032")


def test_ods209_unparsable_value_raises():
    body = b"datetime,region,measured_upscaled,monitoredcapacity\n2026-10-26T10:00:00+00:00,Belgium,n/a,1\n"
    with pytest.raises(ValueError, match="unparsable"):
        sources.parse_elia(body, "ods209")


def test_weather_units_labels_points_and_errors():
    run = pd.Timestamp("2025-06-15", tz="UTC")
    assert len(sources.parse_run(run_json(run), 11)) == 11 * 49
    j = json.loads(run_json(run))
    j[0]["hourly_units"]["temperature_2m"] = "°F"
    with pytest.raises(ValueError, match="units"):
        sources.parse_run(json.dumps(j).encode(), 11)
    j = json.loads(run_json(run))
    j[3]["hourly"]["time"][1] = j[3]["hourly"]["time"][0]
    with pytest.raises(ValueError, match="duplicate"):
        sources.parse_run(json.dumps(j).encode(), 11)
    with pytest.raises(ValueError, match="expected 11"):
        sources.parse_run(run_json(run, points=1), 11)
    for body in (b'{"error":true,"reason":"The service is overloaded"}',
                 b"Unexpected error while streaming data: timeoutReached"):
        with pytest.raises(ValueError):
            sources.parse_run(body, 11)


def test_real_fixture_run_parses():
    body = (ROOT / "fixtures/ecmwf_ifs_2025-06-15T00.json").read_bytes()
    assert sources.parse_run(body, 1)["time"].is_unique


def test_snapshots_never_overwrite(tmp_path, monkeypatch):
    meta = {"retrieved_at": "2026-11-03T10:00:00+00:00", "url": "test"}
    sources.save("elia", "x", "csv", b"first", meta, tmp_path)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=0))
    sources.save("elia", "x", "csv", b"second", meta, tmp_path)
    with pytest.raises(FileExistsError):
        sources.save("elia", "x", "csv", b"third", meta, tmp_path)
    bodies = sorted(sources.read_verified(m) for m in sources.snapshots("elia", tmp_path))
    assert bodies == [b"first", b"second"]


def test_tampered_snapshot_rejected(tmp_path):
    sources.save("elia", "x", "csv", b"body", {"retrieved_at": "2026-11-03T10:00:00+00:00"}, tmp_path)
    m = sources.snapshots("elia", tmp_path)[0]
    m["path"].write_bytes(gzip.compress(b"changed"))
    with pytest.raises(ValueError, match="checksum"):
        sources.read_verified(m)


def test_identical_responses_share_one_object_but_keep_every_retrieval(tmp_path):
    store = tmp_path / "snapshots"
    for t in ("2026-11-05T10:30:00+00:00", "2026-11-05T17:30:00+00:00"):
        sources.save("elia", "x", "csv", b"same body", {"retrieved_at": t}, store)
    ms = sources.snapshots("elia", store)
    assert [m["retrieved_at"] for m in ms] == ["2026-11-05T10:30:00+00:00", "2026-11-05T17:30:00+00:00"]
    assert ms[0]["path"] == ms[1]["path"] and len(list((tmp_path / "objects").rglob("*.gz"))) == 1
    assert ms[0]["sidecar"] != ms[1]["sidecar"]
    assert sources.read_verified(ms[1]) == b"same body"  # hash of the original, decompressed bytes


def test_legacy_plain_payloads_stay_readable(tmp_path):
    body = b"legacy"
    (tmp_path / "elia").mkdir()
    (tmp_path / "elia/x_1.csv").write_bytes(body)
    (tmp_path / "elia/x_1.meta.json").write_text(json.dumps(
        {"retrieved_at": "2026-11-03T10:00:00+00:00", "file": "x_1.csv", "sha256": sources.sha256(body)}))
    assert sources.read_verified(sources.snapshots("elia", tmp_path)[0]) == body


def test_ods209_reconciliation_uses_fixed_calendar_months():
    assert sources.elia_months("2026-12-03") == [(D("2026-10-01"), D("2026-11-01")), (D("2026-11-01"), D("2026-12-01")),
                                                 (D("2026-12-01"), D("2027-01-01"))]


def test_get_does_not_retry_permanent_client_errors(monkeypatch):
    import urllib.error
    calls = []

    def fail(url, timeout):
        calls.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(sources.urllib.request, "urlopen", fail)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        sources.get("https://example.invalid")
    assert len(calls) == 1


def test_pinned_rebuild_refuses_a_changed_sidecar(store, monkeypatch):
    from solar_forecast import cli
    run = pd.Timestamp("2024-03-17", tz="UTC")
    store("weather-belgium", "20240317T00", "json", run_json(run), "2024-03-17 08:00Z", run=run.isoformat())
    m = sources.snapshots("weather-belgium", store.path)[0]
    real = sources.snapshots
    monkeypatch.setattr(cli.sources, "snapshots", lambda s: real(s, store.path))
    monkeypatch.setattr(cli, "_rel", str)
    pinned = {"inputs": [{"source": "weather-belgium", "file": str(m["path"]), "sha256": m["sha256"],
                          "sidecar_sha256": m["sidecar_sha256"]}]}
    assert len(cli._select("weather-belgium", pinned)) == 1
    meta = json.loads(m["sidecar"].read_text())
    meta["retrieved_at"] = "2024-03-17T10:30:00+00:00"  # after the issuance: availability would change
    m["sidecar"].write_text(json.dumps(meta))
    with pytest.raises(SystemExit, match="sidecar differs"):
        cli._select("weather-belgium", pinned)


def test_report_period_follows_the_weather_run():
    from solar_forecast.cli import period
    assert period(pd.Timestamp("2024-11-11 06:00Z")).startswith("hindcast")  # used for target day 2024-11-12
    assert period(pd.Timestamp("2024-11-12 00:00Z")).startswith("runs from")
    assert period(pd.NaT) == "no run"


def test_fresh_pvdaq_download_is_checked_against_the_manifest(tmp_path, monkeypatch):
    body = b"timestamp,ac_power\n"
    manifest = {"installation_experiment": {"production": {"base_url": "https://example.invalid", "files": [
        {"path": "2107_meter_15m_data_2024.csv", "sha256": sources.sha256(body)}]}}}
    monkeypatch.setattr(sources, "get", lambda url: body)
    store = tmp_path / "snapshots"
    [p] = sources.fetch_pvdaq(manifest, store)
    assert sources.read_verified(sources.snapshot(p, store)) == body
    assert sources.fetch_pvdaq(manifest, store) == []  # already stored
    manifest["installation_experiment"]["production"]["files"][0]["path"] = "2107_meter_15m_data_2025.csv"
    monkeypatch.setattr(sources, "get", lambda url: b"changed upstream")
    with pytest.raises(ValueError, match="changed upstream"):
        sources.fetch_pvdaq(manifest, store)


def test_an_incomplete_weather_response_is_kept_and_fetched_again(tmp_path, monkeypatch):
    run = pd.Timestamp("2026-11-06", tz="UTC")
    bodies = [run_json(run, hours=24), run_json(run, hours=sources.LIVE_HOURS)]
    calls = []
    monkeypatch.setattr(sources, "get", lambda url: calls.append(url) or bodies[len(calls) - 1])
    now = run + pd.Timedelta(hours=4)
    sources.fetch_new_runs("belgium", now, hours=(0,), store=tmp_path)
    [short] = sources.snapshots("weather-belgium", tmp_path)
    assert not sources.run_complete(short, "belgium")
    sources.fetch_new_runs("belgium", now, hours=(0,), store=tmp_path)
    assert len(calls) == 2
    assert [sources.run_complete(m, "belgium") for m in sources.snapshots("weather-belgium", tmp_path)] == [False, True]
    sources.fetch_new_runs("belgium", now, hours=(0,), store=tmp_path)
    assert len(calls) == 2  # a complete run is not fetched again


def test_a_capture_reads_only_the_due_runs(tmp_path, monkeypatch):
    old = pd.Timestamp("2025-01-15", tz="UTC")
    sources.save("weather-belgium", "20250115T00", "json", run_json(old), {"retrieved_at": old.isoformat(), "url": "test",
                 "run": old.isoformat(), "params": {"forecast_hours": 49}}, tmp_path)
    [m] = sources.snapshots("weather-belgium", tmp_path)
    m["path"].unlink()  # an unrelated payload is missing
    run = pd.Timestamp("2026-11-06", tz="UTC")
    calls = []
    monkeypatch.setattr(sources, "get", lambda url: calls.append(url) or run_json(run, hours=sources.LIVE_HOURS))
    sources.fetch_new_runs("belgium", run + pd.Timedelta(hours=4), hours=(0,), store=tmp_path)
    assert len(calls) == 1
