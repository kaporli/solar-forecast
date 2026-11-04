import datetime as dt

import numpy as np
import pandas as pd
import pytest

from solar_forecast import evaluation as E
from solar_forecast import features as F

D = lambda s: pd.Timestamp(s).date()


def _ex(days=("2024-11-08", "2024-11-09", "2024-11-10", "2024-11-11", "2024-11-12", "2024-11-13", "2024-11-14")):
    """Two hours per day, one kind; actual 10, capacity 100, everything scored and daylight."""
    rows = []
    for d in days:
        for h in (11, 12):
            rows.append({c: np.nan for c in F.CONTRACT} | {
                "kind": "11h", "target_date": D(d), "target_hour_utc": pd.Timestamp(f"{d} {h}:00Z"),
                "issuance_utc": pd.Timestamp(d, tz="UTC") - pd.Timedelta(hours=14), "capacity": 100.0,
                "persistence_lf": 0.1, "weather_complete": True, "capacity_complete": True,
                "weather_run_utc": pd.Timestamp(d, tz="UTC") - pd.Timedelta(days=1),
                "actual": 10.0, "eligible": True, "day_complete": True, "scored": True, "daylight": True,
                "day_hours": 2})
    return pd.DataFrame(rows)


def test_metrics_on_a_fixture():
    r = _ex(("2025-01-01", "2025-01-02")).assign(prediction=[12.0, 7.0, 10.0, 14.0])
    r.loc[1, "daylight"] = False
    m = E.metrics(r)
    assert m["hours"] == 4 and m["daylight_hours"] == 3 and m["complete_days"] == 2
    assert m["mae_all"] == pytest.approx((2 + 3 + 0 + 4) / 4)
    assert m["mae_daylight"] == pytest.approx((2 + 0 + 4) / 3)
    assert m["rmse_daylight"] == pytest.approx(np.sqrt((4 + 0 + 16) / 3))
    assert m["bias_daylight"] == pytest.approx((2 + 0 + 4) / 3)
    assert m["daily_mae"] == pytest.approx((1 + 4) / 2) and m["daily_bias"] == pytest.approx((-1 + 4) / 2)
    r.loc[0, "scored"] = False  # unscored hours never count, and their day is no longer complete
    m = E.metrics(r)
    assert m["hours"] == 3 and m["complete_days"] == 1


def test_missing_predictions_fail_instead_of_dropping_hours():
    r = _ex(("2025-01-01",)).assign(prediction=[10.0, np.nan])
    with pytest.raises(ValueError, match="non-finite"):
        E.metrics(r)

    class Gaps(Recorder):
        def predict(self, rows):
            return np.where(np.arange(len(rows)) % 2, np.nan, 10.0)

    with pytest.raises(ValueError, match="non-finite predictions on scored hours"):
        E.backtest(Gaps(), _ex(), [E.Fold("V", D("2024-11-13"), D("2024-11-14"))])


def test_daily_totals_need_every_hour_of_the_local_day():
    r = _ex(("2025-01-01", "2025-01-02")).assign(prediction=10.0)
    assert E.metrics(r)["complete_days"] == 2
    assert E.metrics(r.drop(index=0))["complete_days"] == 1  # a subset without one hour drops that day
    r["day_hours"] = 3  # e.g. a 25-hour fall-back day with only 24 rows present
    assert E.metrics(r)["complete_days"] == 0


def test_folds_are_ordered_disjoint_and_test_last():
    fs = E.folds("belgium", include_test=True)
    assert [f.name for f in fs][-1] == "test" and fs[-1].test
    assert all(a.end < b.start for a, b in zip(fs, fs[1:]))
    assert fs[0].start == D("2024-11-13")  # target 2024-11-12 would use the 2024-11-11 hindcast run
    with pytest.raises(ValueError, match="overlap"):
        E.check_folds([E.Fold("a", D("2025-01-01"), D("2025-02-01")), E.Fold("b", D("2025-01-15"), D("2025-03-01"))])
    with pytest.raises(ValueError, match="hindcast"):
        E.check_folds([E.Fold("a", D("2024-11-12"), D("2024-12-01"))])


class Recorder:
    name = "recorder"

    def fit(self, train):
        self.train = train

    def predict(self, rows):
        self.columns = list(rows.columns)
        return np.full(len(rows), 1e9)


def test_training_labels_end_before_the_first_issuance_and_test_is_locked():
    ex = _ex()
    ex.loc[ex["target_date"] == D("2024-11-09"), "eligible"] = False
    fold = E.Fold("V", D("2024-11-13"), D("2024-11-14"))
    c = Recorder()
    out = E.backtest(c, ex, [fold])
    # first issuance 2024-11-12 at 11:00: target day 2024-11-12 is not complete yet, 2024-11-11 is
    assert c.train["target_date"].max() == D("2024-11-11")
    assert D("2024-11-09") not in set(c.train["target_date"])  # unavailable labels are not trained on
    assert "actual" not in c.columns and c.columns == list(F.CONTRACT)
    assert (out["prediction"] == 100.0).all()  # clipped to capacity x 1 h
    with pytest.raises(PermissionError):
        E.backtest(c, ex, [E.Fold("test", D("2024-11-13"), D("2024-11-14"), test=True)])


def test_block_bootstrap_is_paired_and_deterministic():
    ex = _ex(tuple(str(D("2025-01-01") + dt.timedelta(days=i)) for i in range(40)))
    a = ex.assign(prediction=ex["actual"] + 1.0)
    b = ex.assign(prediction=ex["actual"] + np.where(np.arange(len(ex)) % 2, 3.0, 1.0))
    r1, r2 = E.block_bootstrap(a, b), E.block_bootstrap(a, b)
    assert r1 == r2 and r1["difference"] == pytest.approx(-1.0) and r1["high"] < 0 and r1["days"] == 40
    gap = ~ex["target_date"].between(D("2025-01-10"), D("2025-01-13"))  # days without scored hours
    r3 = E.block_bootstrap(a[gap], b[gap])
    assert r3["calendar_days"] == 40 and r3["days"] == 36  # blocks run over calendar days, gap included
