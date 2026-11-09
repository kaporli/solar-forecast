import json
import sys
from pathlib import Path

import pandas as pd

from solar_forecast import baselines as B
from solar_forecast import issue as I
from solar_forecast import sources

from conftest import elia_csv
from test_issue import ISSUANCE, TARGET, live_store

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import deploy_check as DC  # noqa: E402


def published(tmp_path):
    root = tmp_path / "data/raw/snapshots"
    def put(source, name, ext, body, retrieved, **extra):
        sources.save(source, name, ext, body, {"retrieved_at": pd.Timestamp(retrieved).isoformat(), "url": "test", **extra}, root)
    put.path = root
    live_store(put)
    I.issue("belgium", "11h", TARGET, ISSUANCE + pd.Timedelta(minutes=1), root, tmp_path / "data/forecasts")
    return put


def run(tmp_path, capsys):
    code = DC.main(["--data", str(tmp_path / "data")])
    return code, json.loads(capsys.readouterr().out)


def test_the_check_passes_on_unchanged_code(tmp_path, capsys):
    published(tmp_path)
    code, out = run(tmp_path, capsys)
    assert code == 0 and out["failed"] == [] and out["hours_match"] and out["inputs_match"]
    assert out["feed_states"]["11h"] in ("fresh", "stale", "unavailable")


def test_a_baseline_change_fails(tmp_path, capsys, monkeypatch):
    published(tmp_path)
    real = B.BelgiumPhysics.predict
    monkeypatch.setattr(B.BelgiumPhysics, "predict", lambda self, x: real(self, x) + 1.0)
    code, out = run(tmp_path, capsys)
    assert code == 1 and "hours differ" in out["failed"] and out["features_sha256_match"]


def test_a_snapshot_imported_later_with_an_earlier_time_is_not_replayed(tmp_path, capsys):
    put = published(tmp_path)
    put("elia", "ods210_total", "csv", elia_csv("2026-10-25", "2026-11-06 09:30", measured=lambda t: 999.0),
        "2026-11-06 09:40Z", dataset="ods210")  # retrieved before the 10:00 issuance, stored after it
    code, out = run(tmp_path, capsys)
    assert code == 0 and out["failed"] == []


def test_a_changed_snapshot_fails(tmp_path, capsys):
    put = published(tmp_path)
    sidecar = next((put.path / "elia").glob("*.meta.json"))
    sidecar.write_text(sidecar.read_text().replace('"url": "test"', '"url": "changed"'))
    code, out = run(tmp_path, capsys)
    assert code == 1 and any("sidecar differs from the record" in f for f in out["failed"])


def test_a_missing_feed_fails(tmp_path, capsys, monkeypatch):
    published(tmp_path)
    monkeypatch.setitem(sys.modules, "solar_forecast.feed", None)  # import fails
    code, out = run(tmp_path, capsys)
    assert code == 1 and any(f.startswith("feed: ") for f in out["failed"])
