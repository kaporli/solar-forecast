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

from . import features as F
from . import sources
from .weather import HOUR, hour_weather

CONFIG = json.loads((sources.ROOT / "config/evaluation.json").read_text())


def _cell(p: dict) -> dict:
    model, kind = p["cell_temperature"].split(" ")
    return pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS[model][kind]


def dc_per_kw(hours, lat, lon, ghi, dni, dhi, temp_air, wind, p: dict) -> np.ndarray:
    """Mean DC power per kW of nameplate over [s, s + 1h): plane-of-array irradiance from the hour's mean GHI, DNI and
    DHI with the sun at s + 30 min, SAPM cell temperature, PVWatts temperature response. NaN weather stays NaN."""
    mid = pd.DatetimeIndex(hours) + HOUR / 2
    sp = pvlib.solarposition.get_solarposition(mid, lat, lon)
    return dc_from_sun(sp["apparent_zenith"].to_numpy(), sp["azimuth"].to_numpy(),
                       pvlib.irradiance.get_extra_radiation(mid).to_numpy(), ghi, dni, dhi, temp_air, wind, p)


def dc_from_sun(zenith, azimuth, dni_extra, ghi, dni, dhi, temp_air, wind, p: dict) -> np.ndarray:
    """`dc_per_kw` from a solar position already computed, so many geometries can share it (`local`)."""
    poa = pvlib.irradiance.get_total_irradiance(
        p["tilt_deg"], p["azimuth_deg"], zenith, azimuth,
        np.asarray(dni, float), np.asarray(ghi, float), np.asarray(dhi, float),
        dni_extra=dni_extra, model=p["transposition"], albedo=p["albedo"],
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
    ratio; national = monitored capacity x capacity-weighted mean over provinces.

    Inputs come from `features.issuance_inputs` with the same `live` flag as the feature rows, so the weather
    snapshot, capacity and weights are the ones the features were built from. A row whose run, snapshot retrieval
    or capacity differs raises: it was built under other availability."""

    name = "physics"

    def __init__(self, inputs, p: dict | None = None, live: bool = False):
        self.inputs, self.p, self.live = inputs, p or CONFIG["physics"]["belgium"], live

    def fit(self, train) -> None:
        pass

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        exp = self.inputs.exp
        parts = []
        for (target_date, kind), g in rows.groupby(["target_date", "kind"], sort=False):
            ctx = F.issuance_inputs(exp, self.inputs, target_date, kind, self.live)
            first = g.iloc[0]
            same = (_same(ctx.run, first["weather_run_utc"]) and _same(ctx.retrieved_at, first["weather_retrieved_at"])
                    and _same(ctx.capacity, first["capacity"]))
            if not same:
                raise ValueError(f"{target_date} {kind}: feature row built from other snapshots than live={self.live}")
            if ctx.weather is None or not ctx.capacity_complete:
                continue
            hours = pd.DatetimeIndex(g["target_hour_utc"])
            w = hour_weather(ctx.weather, hours).reset_index()
            w["weight"] = w["point"].map(ctx.weights)
            w["national"] = ctx.capacity
            w["row"] = w["hour_utc"].map(pd.Series(g.index, index=hours))
            parts.append(w)
        out = pd.Series(np.nan, index=rows.index)
        if not parts:
            return out.to_numpy()
        w = pd.concat(parts, ignore_index=True)
        w["dc"] = np.nan
        for point, g in w.groupby("point"):
            lat, lon = exp.points[point]
            w.loc[g.index, "dc"] = dc_per_kw(g["hour_utc"], lat, lon, g["shortwave_radiation"],
                                             g["direct_normal_irradiance"], g["diffuse_radiation"],
                                             g["temperature_2m"], g["wind_speed_10m"], self.p)
        w["part"] = w["dc"] * self.p["performance_ratio"] * w["weight"] * w["national"]
        out.loc[:] = w.groupby("row")["part"].sum(min_count=len(exp.points)).reindex(rows.index).to_numpy()
        return out.to_numpy()


def _same(a, b) -> bool:
    if a is None or pd.isna(a):
        return b is None or pd.isna(b)
    if b is None or pd.isna(b):
        return False
    if isinstance(a, float):
        return bool(np.isclose(a, float(b)))
    return pd.Timestamp(a) == pd.Timestamp(b)


def candidates(exp_name: str, inputs) -> list:
    return [Persistence(), BelgiumPhysics(inputs) if exp_name == "belgium" else PvdaqPhysics()]
