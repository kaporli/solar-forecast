"""Learned candidates for #5, on the candidate interface of `baselines`. They predict load factor (target /
capacity known at issuance) and return energy per hour. Every fitted quantity (calibration factor, trees) is
fitted inside `fit` on the fold's training rows only.

Inputs: the feature contract plus `physics_mwh`, the uncalibrated physics baseline for the same row. Physics is
a deterministic function of the issuance's inputs (`features.issuance_inputs`), so it is available at issuance.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

EXTRA = ["physics_mwh"]

# Declared before any validation result and not tuned on the validation folds.
GBM_PARAMS = dict(loss="absolute_error", learning_rate=0.05, max_iter=400, max_leaf_nodes=31, min_samples_leaf=50,
                  l2_regularization=1.0, early_stopping=False, random_state=0)

GROUPS = {
    "irradiance": ["shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation", "clearness"],
    "temperature_cloud": ["temperature_2m", "wind_speed_10m", "cloud_cover"],
    "geometry": ["cos_zenith", "solar_azimuth_deg", "ghi_clearsky"],
    "calendar": ["day_of_year", "local_hour"],
    "lead": ["lead_hours"],
    "lags": ["persistence_lf"],
}
ALL = [g for g in GROUPS]


def design(rows: pd.DataFrame, groups: list[str], physics: bool) -> pd.DataFrame:
    """Row-local inputs: contract columns, clear-sky index of the forecast GHI, physics load factor."""
    x = pd.DataFrame(index=rows.index)
    for g in groups:
        for c in GROUPS[g]:
            if c == "clearness":
                cs = rows["ghi_clearsky"].to_numpy(float)
                x[c] = np.where(cs > 10, rows["shortwave_radiation"].to_numpy(float) / np.maximum(cs, 10), 0.0).clip(0, 1.5)
            else:
                x[c] = rows[c].to_numpy(float)
    if physics:
        x["physics_lf"] = physics_lf(rows)
    return x


def physics_lf(rows: pd.DataFrame) -> np.ndarray:
    return rows["physics_mwh"].to_numpy(float) / rows["capacity"].to_numpy(float)


def load_factor(train: pd.DataFrame) -> np.ndarray:
    return train["actual"].to_numpy(float) / train["capacity"].to_numpy(float)


def calibration_factor(train: pd.DataFrame) -> float:
    """Least-squares scale of physics to the observed load factor, no intercept (physics is zero at night)."""
    p, y = physics_lf(train), load_factor(train)
    ok = np.isfinite(p) & np.isfinite(y)
    return float((p[ok] * y[ok]).sum() / (p[ok] ** 2).sum())


class CalibratedPhysics:
    """Physics baseline times one factor fitted on the fold's training rows."""

    def __init__(self, name: str = "physics_calibrated"):
        self.name, self.inputs = name, ["physics_lf"]

    def fit(self, train: pd.DataFrame) -> None:
        self.a = calibration_factor(train)

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        return self.a * physics_lf(rows) * rows["capacity"].to_numpy(float)


class GBM:
    """Gradient-boosted trees on load factor. `residual=True`: physics calibrated on the same training rows plus
    trees on the remaining load factor, so no calibration from later data reaches an earlier fold."""

    def __init__(self, name: str, groups: list[str], physics: bool = False, residual: bool = False,
                 params: dict | None = None):
        self.name, self.groups, self.physics, self.residual = name, list(groups), physics or residual, residual
        self.params = params or GBM_PARAMS
        self.inputs = list(design(pd.DataFrame(columns=_ALL_COLUMNS), self.groups, self.physics).columns)

    def fit(self, train: pd.DataFrame) -> None:
        x, y = design(train, self.groups, self.physics), load_factor(train)
        self.a = calibration_factor(train) if self.residual else 0.0
        if self.residual:
            y = y - self.a * physics_lf(train)
        ok = np.isfinite(y)
        self.model = HistGradientBoostingRegressor(**self.params).fit(x[ok], y[ok])

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        lf = self.model.predict(design(rows, self.groups, self.physics))
        if self.residual:
            lf = lf + self.a * physics_lf(rows)
        return lf * rows["capacity"].to_numpy(float)


_ALL_COLUMNS = [c for g in GROUPS.values() for c in g if c != "clearness"] + ["capacity", "physics_mwh"]


def candidates() -> list:
    """The three planned candidates; selection is made among these only."""
    return [CalibratedPhysics(), GBM("gbm", ALL), GBM("physics_residual_gbm", ALL, residual=True)]


def ablation() -> list:
    """Direct GBM without one group at a time; reported, not selected from."""
    drop = ["lags", "temperature_cloud", "irradiance", "calendar"]
    return [GBM(f"gbm_without_{g}", [h for h in ALL if h != g]) for g in drop] + [
        GBM("gbm_without_weather", ["geometry", "calendar", "lead", "lags"])]
