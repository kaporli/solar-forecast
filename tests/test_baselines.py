import numpy as np
import pandas as pd
import pytest

from solar_forecast import baselines as B
from solar_forecast import features as F
from solar_forecast import sources

P = B.CONFIG["physics"]["pvdaq"]
SITE = sources.config("installation")


def _dc(hours, ghi, dni, dhi, temp, wind=1.0):
    n = len(hours)
    return B.dc_per_kw(hours, SITE["latitude"], SITE["longitude"], [ghi] * n, [dni] * n, [dhi] * n, [temp] * n,
                       [wind] * n, P)


def test_fully_dark_hours_give_zero():
    night = pd.date_range("2025-06-01 07:00", periods=3, freq="h", tz="UTC")  # 00:00 to 03:00 local
    assert (_dc(night, 0, 0, 0, 15) == 0).all()
    rows = pd.DataFrame({"target_hour_utc": night, "shortwave_radiation": 0.0, "direct_normal_irradiance": 0.0,
                         "diffuse_radiation": 0.0, "temperature_2m": 15.0, "wind_speed_10m": 1.0})
    assert (B.PvdaqPhysics().predict(rows) == 0).all()


def test_temperature_response_lowers_output():
    noon = pd.DatetimeIndex([pd.Timestamp("2025-06-01 20:00Z")])
    cool, hot = _dc(noon, 900, 800, 100, 10)[0], _dc(noon, 900, 800, 100, 40)[0]
    assert hot < cool
    assert (cool - hot) / cool == pytest.approx(0.004 * 30, rel=0.15)  # gamma x temperature difference


def test_clipping_at_the_ac_limit_and_energy_units():
    ac = B.installation_ac_kw(np.array([0.0, 400.0, 2000.0, np.nan]), P)
    assert ac[0] == 0 and np.isnan(ac[3]) and ac[2] == P["ac_limit_kw"]
    after_losses = 400.0 * (1 - 14.075660688264469 / 100)
    assert 0.9 * after_losses < ac[1] < after_losses  # inverter losses only
    noon = pd.DatetimeIndex([pd.Timestamp("2025-06-01 20:00Z")])
    rows = pd.DataFrame({"target_hour_utc": noon, "shortwave_radiation": 900.0, "direct_normal_irradiance": 800.0,
                         "diffuse_radiation": 100.0, "temperature_2m": 20.0, "wind_speed_10m": 1.0})
    kwh = B.PvdaqPhysics().predict(rows)[0]  # one hour at constant mean power: kWh equals kW
    assert kwh == pytest.approx(B.installation_ac_kw(P["dc_kw"] * _dc(noon, 900, 800, 100, 20), P)[0])
    assert 0 < kwh <= P["ac_limit_kw"]


def test_persistence_scales_load_factor_by_capacity():
    rows = pd.DataFrame({"persistence_lf": [0.5, np.nan], "capacity": [8000.0, 8000.0]})
    p = B.Persistence().predict(rows)
    assert p[0] == 4000.0 and np.isnan(p[1])


def test_belgium_physics_uses_the_feature_rows_inputs(store):
    from conftest import elia_csv
    from test_features import TARGET, base, inputs

    base(store)
    i = inputs(store.path)
    rows = F.features_for_issuance(F.belgium(), i, TARGET, "11h", live=True)
    before = B.BelgiumPhysics(i, live=True).predict(rows)
    night = rows["cos_zenith"].to_numpy() == 0
    assert np.isfinite(before).all() and (before[~night] > 0).any() and (before <= rows["capacity"].to_numpy()).all()
    # a capacity revision retrieved after the issuance: live features and live physics must not change
    store("elia", "ods032_belgium", "csv", elia_csv("2024-03-05", "2024-03-17 09:30", capacity=2000.0),
          "2024-03-17 12:00Z", dataset="ods032")
    j = inputs(store.path)
    live = F.features_for_issuance(F.belgium(), j, TARGET, "11h", live=True)
    pd.testing.assert_frame_equal(live, rows)
    np.testing.assert_allclose(B.BelgiumPhysics(j, live=True).predict(live), before)
    with pytest.raises(ValueError, match="other snapshots"):  # live rows, retrospective inputs
        B.BelgiumPhysics(j, live=False).predict(live)
    replay = F.features_for_issuance(F.belgium(), j, TARGET, "11h")
    np.testing.assert_allclose(B.BelgiumPhysics(j).predict(replay), 2 * before)  # replay sees the revision
