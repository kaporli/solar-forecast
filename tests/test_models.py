import io

import joblib
import numpy as np
import pandas as pd
import pytest

from solar_forecast import evaluation as E
from solar_forecast import features as F
from solar_forecast import models as M
from solar_forecast.model_report import select

D = lambda s: pd.Timestamp(s).date()


def _ex(days=60, start="2024-10-01", ratio=lambda d: 0.5, seed=0):
    """Hourly rows with random weather; load factor = ratio(day) x physics load factor."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(days):
        d = D(start) + pd.Timedelta(days=i)
        for h in range(6, 18):
            p = max(0.0, np.sin((h - 6) / 12 * np.pi)) * rng.uniform(0.2, 1.0)
            rows.append({c: np.nan for c in F.CONTRACT} | {
                "kind": "11h", "target_date": d, "target_hour_utc": pd.Timestamp(f"{d} {h}:00Z"), "capacity": 1000.0,
                "shortwave_radiation": 800 * p, "direct_normal_irradiance": 600 * p, "diffuse_radiation": 100 * p,
                "temperature_2m": rng.uniform(0, 30), "wind_speed_10m": rng.uniform(0, 10), "cloud_cover": rng.uniform(0, 100),
                "cos_zenith": p, "solar_azimuth_deg": 90 + 15 * (h - 6), "ghi_clearsky": 900 * p + 1,
                "day_of_year": d.timetuple().tm_yday, "local_hour": h, "lead_hours": 24 + h, "persistence_lf": rng.uniform(0, 0.5),
                "weather_complete": True, "capacity_complete": True, "physics_mwh": 1000 * p,
                "actual": 1000 * p * ratio(d), "eligible": True, "day_complete": True, "scored": True, "daylight": True,
                "day_hours": 12})
    return pd.DataFrame(rows)


def test_calibration_is_fitted_on_training_rows_only():
    # the ratio changes in the second fold; the first fold's factor must not know it
    ex = _ex(ratio=lambda d: 0.5 if d < D("2024-11-01") else 0.9)
    fs = [E.Fold("A", D("2024-10-20"), D("2024-10-31")), E.Fold("B", D("2024-11-10"), D("2024-11-29"))]
    out = E.backtest(M.CalibratedPhysics(), ex, fs, extra=tuple(M.EXTRA))
    a = out[out["fold"] == "A"]
    np.testing.assert_allclose(a["prediction"], 0.5 * a["physics_mwh"])
    b = out[out["fold"] == "B"]
    b = b[b["physics_mwh"] > 0]
    ratio = (b["prediction"] / b["physics_mwh"]).iloc[0]
    assert 0.5 < ratio < 0.9  # days up to 2024-11-08 only: a mix of both regimes


def test_residual_model_adds_trees_to_fold_calibrated_physics():
    ex = _ex()
    m = M.GBM("r", M.ALL, residual=True)
    m.fit(ex)
    assert m.a == pytest.approx(0.5)  # residual target y - a x physics is zero here
    np.testing.assert_allclose(m.predict(ex), 0.5 * ex["physics_mwh"], atol=1.0)


def test_gbm_is_deterministic_and_reloads_identically():
    ex = _ex(ratio=lambda d: 0.4 + 0.01 * (d.day % 7))
    a, b = M.GBM("gbm", M.ALL), M.GBM("gbm", M.ALL)
    a.fit(ex)
    b.fit(ex)
    np.testing.assert_array_equal(a.predict(ex), b.predict(ex))
    buf = io.BytesIO()
    joblib.dump(a, buf)
    buf.seek(0)
    np.testing.assert_array_equal(joblib.load(buf).predict(ex), a.predict(ex))
    assert a.inputs == list(M.design(ex, M.ALL, False).columns) and "physics_lf" not in a.inputs


def _primary(maes, highs, folds):
    names = list(folds["physics"])
    return {"pooled": {c: {"mae_daylight": v} for c, v in maes.items()},
            "folds": {c: {f: {"mae_daylight": v} for f, v in fv.items()} for c, fv in folds.items()},
            "vs_best_baseline": {c: {"high": h} for c, h in highs.items()},
            "candidates": list(highs),
            "resources": {c: {"inputs": n, "predict_s": 0.01} for c, n in zip(highs, (1, 14, 15))}}, names


def test_selection_rule():
    folds = {"persistence": {"V1": 9, "V2": 9}, "physics": {"V1": 10, "V2": 10},
             "physics_calibrated": {"V1": 9.4, "V2": 9.4}, "gbm": {"V1": 8.0, "V2": 8.5}, "physics_residual_gbm": {"V1": 7.9, "V2": 8.4}}
    maes = {c: sum(v.values()) / 2 for c, v in folds.items()}
    p, names = _primary(maes, {"physics_calibrated": -0.1, "gbm": -1.0, "physics_residual_gbm": -1.1}, folds)
    s = select(p, names)
    assert s["best_baseline"] == "persistence"
    assert not s["checks"]["physics_calibrated"]["passes"]  # under 5 % better than the best baseline
    assert s["selected"] == "gbm"  # within 2 % of the best and fewer inputs than the residual model
    p["vs_best_baseline"]["gbm"]["high"] = 0.2  # interval crosses zero
    assert select(p, names)["selected"] == "physics_residual_gbm"
    for c in ("gbm", "physics_residual_gbm"):
        p["vs_best_baseline"][c]["high"] = 0.2
    assert select(p, names)["selected"] == "persistence"  # nothing passes: the best baseline stays


def test_lock_refuses_a_recipe_that_differs_from_the_evaluated_one(monkeypatch):
    from solar_forecast import model_report as R

    evaluated = R.recipe(M.GBM("gbm", M.ALL))
    monkeypatch.setattr(M, "GBM_PARAMS", M.GBM_PARAMS | {"max_iter": 1})
    assert R.recipe(M.GBM("gbm", M.ALL)) != evaluated
    if (R.ROOT / "data/evaluation/belgium-models.json").exists():
        with pytest.raises(SystemExit, match="recipe of gbm"):
            R.lock("belgium")


def test_interval_calibration_uses_only_labels_known_at_the_fold_start():
    from solar_forecast.model_report import interval_check

    rng = np.random.default_rng(1)
    days = [D("2025-01-01") + pd.Timedelta(days=i) for i in range(40)]
    sel = pd.DataFrame([{"target_date": d, "fold": "V1" if d <= D("2025-01-20") else "V2", "scored": True,
                         "daylight": True, "local_hour": h, "lead_hours": 30 + h, "capacity": 1000.0,
                         "ghi_clearsky": 500.0, "prediction": 400.0, "actual": 400.0 + rng.normal(0, 50)}
                        for d in days for h in (10, 12)])
    fs = [E.Fold("V1", D("2025-01-01"), D("2025-01-20")), E.Fold("V2", D("2025-01-21"), D("2025-02-09"))]
    base = interval_check(sel, fs)
    late = sel.copy()  # 2025-01-20 is in V1, but its labels are not complete at V2's first issuance (2025-01-20)
    late.loc[late["target_date"] == D("2025-01-20"), "actual"] = 5000.0
    assert interval_check(late, fs) == base
    early = sel.copy()
    early.loc[early["target_date"] == D("2025-01-19"), "actual"] = 5000.0  # known by then: it counts
    assert interval_check(early, fs) != base
