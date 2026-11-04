import json
import uuid

import pandas as pd
import pytest

from solar_forecast import sources
from solar_forecast.sources import ROOT

from conftest import elia_csv, run_json


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
    p = sources.save("elia", "x", "csv", b"body", {"retrieved_at": "2026-11-03T10:00:00+00:00"}, tmp_path)
    p.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        sources.read_verified(sources.snapshots("elia", tmp_path)[0])


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
