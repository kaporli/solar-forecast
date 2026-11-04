"""Baselines for #4. Each candidate predicts energy per hour (MWh for belgium, kWh for pvdaq) from feature rows
(`features.CONTRACT` columns, no targets) and fits nothing. Assumptions are in config/evaluation.json, "physics".

Candidate interface, shared with the learned models of #5:
    name: str
    fit(train: DataFrame) -> None       examples (features and target) the fold may train on
    predict(rows: DataFrame) -> ndarray  one prediction per feature row
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pvlib

from . import sources
from .weather import HOUR, hour_weather

CONFIG = json.loads((sources.ROOT / "config/evaluation.json").read_text())


def _cell(p: dict) -> dict:
    model, kind = p["cell_temperature"].split(" ")
    return pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS[model][kind]


def dc_per_kw(hours, lat, lon, ghi, dni, dhi, temp_air, wind, p: dict) -> np.ndarray:
    """Mean DC power per kW of nameplate over [s, s + 1h): plane-of-array irradiance from the hour's mean GHI, DNI and
    DHI with the sun at s + 30 min, SAPM cell temperature, PVWatts temperature response. NaN weather stays NaN."""
    hours = pd.DatetimeIndex(hours)
    mid = hours + HOUR / 2
    sp = pvlib.solarposition.get_solarposition(mid, lat, lon)
    poa = pvlib.irradiance.get_total_irradiance(
        p["tilt_deg"], p["azimuth_deg"], sp["apparent_zenith"].to_numpy(), sp["azimuth"].to_numpy(),
        np.asarray(dni, float), np.asarray(ghi, float), np.asarray(dhi, float),
        dni_extra=pvlib.irradiance.get_extra_radiation(mid).to_numpy(), model=p["transposition"], albedo=p["albedo"],
    )["poa_global"]
    poa = np.clip(np.asarray(poa, float), 0, None)
    cell = pvlib.temperature.sapm_cell(poa, np.asarray(temp_air, float), np.asarray(wind, float), **_cell(p))
    return np.asarray(pvlib.pvsystem.pvwatts_dc(poa, cell, 1.0, p["gamma_pdc_per_c"]), float)


def installation_ac_kw(dc_kw: np.ndarray, p: dict) -> np.ndarray:
    """DC after PVWatts default losses, then the PVWatts inverter, clipped at the AC limit."""
    dc = np.asarray(dc_kw, float) * (1 - pvlib.pvsystem.pvwatts_losses() / 100)
    eta = p["inverter_eta_nominal"]
    ac = np.asarray(pvlib.inverter.pvwatts(dc, p["ac_limit_kw"] / eta, eta_inv_nom=eta), float)
    return np.where(np.isnan(dc), np.nan, np.clip(ac, 0, p["ac_limit_kw"]))


class Persistence:
    """Load factor of the same local clock hour on D-2 (else up to D-8) times the capacity known at issuance."""

    name = "persistence"

    def fit(self, train) -> None:
        pass

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        return (rows["persistence_lf"] * rows["capacity"]).to_numpy(float)


class PvdaqPhysics:
    """Installation reference: documented geometry and DC capacity, assumed losses, inverter efficiency and AC limit.
    Hourly energy (kWh) = mean AC power (kW) over the hour x 1 h."""

    name = "physics"

    def __init__(self, p: dict | None = None):
        self.p = p or CONFIG["physics"]["pvdaq"]
        site = sources.config("installation")
        self.lat, self.lon = site["latitude"], site["longitude"]

    def fit(self, train) -> None:
        pass

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        dc = self.p["dc_kw"] * dc_per_kw(rows["target_hour_utc"], self.lat, self.lon, rows["shortwave_radiation"],
                                         rows["direct_normal_irradiance"], rows["diffuse_radiation"],
                                         rows["temperature_2m"], rows["wind_speed_10m"], self.p)
        return installation_ac_kw(dc, self.p)  # mean kW over one hour = kWh


class BelgiumPhysics:
    """Aggregate reference: per province, PVWatts DC per kW for an assumed fleet geometry times a fixed performance
    ratio; national = monitored capacity x capacity-weighted mean over provinces. Uses the same weather run and the
    same capacity weights at the cutoff as the feature row, read from the snapshots in `inputs`."""

    name = "physics"

    def __init__(self, inputs, p: dict | None = None):
        self.inputs, self.p = inputs, p or CONFIG["physics"]["belgium"]

    def fit(self, train) -> None:
        pass

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        points = self.inputs.exp.points
        parts = []
        for (issuance, run, cutoff), g in rows.groupby(["issuance_utc", "weather_run_utc", "actuals_cutoff_utc"],
                                                       sort=False, dropna=False):
            national, per_point = self.inputs.capacity(None, cutoff)
            got = None if pd.isna(run) else self.inputs.weather.get(run, None)
            if got is None or not np.isfinite(per_point).all() or per_point.sum() <= 0:
                continue
            hours = pd.DatetimeIndex(g["target_hour_utc"])
            w = hour_weather(got[0], hours).reset_index()
            w["weight"] = w["point"].map(per_point / per_point.sum())
            w["national"] = national
            w["row"] = w["hour_utc"].map(pd.Series(g.index, index=hours))
            parts.append(w)
        out = pd.Series(np.nan, index=rows.index)
        if not parts:
            return out.to_numpy()
        w = pd.concat(parts, ignore_index=True)
        w["dc"] = np.nan
        for point, g in w.groupby("point"):
            lat, lon = points[point]
            w.loc[g.index, "dc"] = dc_per_kw(g["hour_utc"], lat, lon, g["shortwave_radiation"],
                                             g["direct_normal_irradiance"], g["diffuse_radiation"],
                                             g["temperature_2m"], g["wind_speed_10m"], self.p)
        w["part"] = w["dc"] * self.p["performance_ratio"] * w["weight"] * w["national"]
        out.loc[:] = w.groupby("row")["part"].sum(min_count=len(points)).reindex(rows.index).to_numpy()
        return out.to_numpy()


def candidates(exp_name: str, inputs) -> list:
    return [Persistence(), BelgiumPhysics(inputs) if exp_name == "belgium" else PvdaqPhysics()]
