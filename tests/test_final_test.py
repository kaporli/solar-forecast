import json
import shutil

import pandas as pd
import pytest

from solar_forecast import artifact as A
from solar_forecast import final_test as T
from solar_forecast import models as M

LOCK = json.loads((T.ROOT / "config/lock-belgium.json").read_text())
ART = A.directory("belgium")
needs_artifact = pytest.mark.skipif(not (ART / "manifest.json").exists(), reason="artifact not packaged yet")


def test_the_final_test_runs_once(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/lock-belgium.json").write_text("{}")
    (tmp_path / "data/evaluation").mkdir(parents=True)
    (tmp_path / "data/evaluation/belgium-test.json").write_text("{}")
    monkeypatch.setattr(T, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="runs once"):
        T.run("belgium")


def test_drift_names_a_changed_dataset():
    lock = json.loads(json.dumps(LOCK))
    lock["references"]["dataset_manifest"]["sha256"] = "0" * 64
    assert "dataset manifest" in T.drift(lock)


def test_build_refuses_code_that_no_longer_gives_the_locked_recipe(monkeypatch):
    assert T.build(LOCK["model"]).inputs == LOCK["model"]["inputs"]
    monkeypatch.setitem(M.GROUPS, "lags", ["persistence_lf", "capacity"])
    with pytest.raises(SystemExit, match="locked recipe"):
        T.build(LOCK["model"])


@needs_artifact
def test_reloaded_artifact_reproduces_the_fixtures():
    assert A.parity(ART) == 0.0


@needs_artifact
def test_artifact_refuses_a_changed_model_or_missing_inputs(tmp_path):
    shutil.copytree(ART, tmp_path / "a")
    model, man = A.load(tmp_path / "a")
    fx = pd.read_parquet(tmp_path / "a/fixtures.parquet")
    with pytest.raises(ValueError, match="lack"):
        A.predict(model, man, fx.drop(columns="cloud_cover"))
    assert (A.predict(model, man, fx.assign(capacity=1.0)) <= 1.0).all()  # output clipped to capacity
    man["model_sha256"] = "0" * 64
    (tmp_path / "a/manifest.json").write_text(json.dumps(man))
    with pytest.raises(ValueError, match="checksum"):
        A.load(tmp_path / "a")


@needs_artifact
@pytest.mark.parametrize("change,match", [
    (dict(capacity=-1.0), "positive"), (dict(capacity=float("nan")), "non-finite inputs"),
    (dict(cloud_cover=float("inf")), "non-finite inputs"), (dict(cloud_cover="x"), "non-numeric")])
def test_artifact_rejects_inputs_outside_the_contract(change, match):
    model, man = A.load(ART)
    fx = pd.read_parquet(ART / "fixtures.parquet")
    with pytest.raises(ValueError, match=match):
        A.predict(model, man, fx.assign(**change))


@needs_artifact
def test_artifact_refuses_changed_prediction_code(monkeypatch):
    monkeypatch.setattr(A, "implementation", lambda: {"models.py": "0" * 64, "artifact.py": "0" * 64})
    with pytest.raises(ValueError, match="prediction code"):
        A.load(ART)


def test_elia_slices_compare_every_model_on_the_same_hours():
    from test_models import _ex

    ex = _ex(days=40, start="2026-01-01").assign(cloud_cover=50.0)
    pred = lambda f: ex.assign(prediction=ex["actual"] * f)
    r = {"gbm": pred(1.1), "persistence": pred(1.5), "elia": pred(0.9)}
    r["elia"].loc[r["elia"]["target_date"] < pd.Timestamp("2026-01-05").date(), "prediction"] = float("nan")
    s = T.slices(r, ["before 2026-10-26 (ods032)", "from 2026-10-26 (ods209)"])["source"]["before 2026-10-26 (ods032)"]
    assert (s["days"], s["elia_subset"]["days"]) == (40, 36)
    assert s["elia_subset"]["daylight_hours"] == 36 * 12 < s["daylight_hours"]
    sub = r["gbm"][r["gbm"]["target_date"] >= pd.Timestamp("2026-01-05").date()]
    assert s["elia_subset"]["mae_daylight"]["gbm"] == pytest.approx((sub["prediction"] - sub["actual"]).abs().mean())
