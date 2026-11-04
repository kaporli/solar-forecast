import numpy as np
import pandas as pd
import pandas.testing as pdt

from solar_forecast import features as F
from solar_forecast import sources
from solar_forecast.sources import ROOT

from conftest import elia_csv, run_json

TARGET = "2024-03-18"  # 11h issuance 2024-03-17 10:00 UTC, rule run 2024-03-17 00 UTC
EXP = F.belgium()
ISSUE = F.issuance_time(EXP, TARGET, "11h")


def inputs(path):
    return F.BelgiumInputs(sources.snapshots("elia", path), sources.snapshots("weather-belgium", path), EXP)


def base(store, run_retrieved="2024-03-17 08:00Z", elia_retrieved="2024-03-17 09:45Z"):
    store("elia", "ods032_belgium", "csv", elia_csv("2024-03-05", "2024-03-17 09:30"), elia_retrieved, dataset="ods032")
    for day in ("2024-03-16", "2024-03-17"):
        run = pd.Timestamp(day, tz="UTC")
        store("weather-belgium", run.strftime("%Y%m%dT%H"), "json", run_json(run), run_retrieved, run=run.isoformat())


def test_issuance_rules():
    assert ISSUE == pd.Timestamp("2024-03-17 10:00Z")
    assert F.rule_run(EXP, ISSUE, "11h") == pd.Timestamp("2024-03-17 00:00Z")
    summer = F.issuance_time(EXP, "2024-08-20", "18h")
    assert summer == pd.Timestamp("2024-08-19 16:00Z") and F.rule_run(EXP, summer, "18h") == pd.Timestamp("2024-08-19 06:00Z")
    pv = F.pvdaq()
    i = F.issuance_time(pv, "2025-06-02", "09h")
    assert i == pd.Timestamp("2025-06-01 16:00Z") and pv.cutoff(i) == pd.Timestamp("2025-06-01 07:00Z")


def test_weather_interval_alignment(store):
    base(store)
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h")
    # target day 2024-03-18 starts 2024-03-17 23:00 UTC = label offset 23 from the run
    first = f.iloc[0]
    assert first["target_hour_utc"] == pd.Timestamp("2024-03-17 23:00Z") and first["lead_hours"] == 23
    assert first["shortwave_radiation"] == 24.0  # mean of the hour ending at label s + 1h
    assert first["temperature_2m"] == 23.5  # mean of labels s and s + 1h
    assert f["weather_complete"].all() and (f["weather_availability"] == "recorded").all()


def test_offline_and_inference_entry_points_agree(store):
    base(store)
    i = inputs(store.path)
    batch = F.build_features(EXP, i, [("2024-03-17", "11h"), (pd.Timestamp(TARGET).date(), "11h")])
    live = F.features_for_issuance(EXP, i, TARGET, "11h", live=True)
    pdt.assert_frame_equal(batch[batch["target_date"] == pd.Timestamp(TARGET).date()].reset_index(drop=True), live)
    alone = F.build_features(EXP, i, [(pd.Timestamp(TARGET).date(), "11h")])
    pdt.assert_frame_equal(alone, live)  # row-local: no dependence on other issuances


def test_snapshots_after_issuance_are_unavailable_live(store):
    base(store, run_retrieved="2024-03-17 10:30Z")  # rule run retrieved after the 10:00 issuance
    # a later Elia snapshot revises D-2 and raises capacity; live features must not see it
    store("elia", "ods032_belgium", "csv", elia_csv("2024-03-05", "2024-03-17 12:00", measured=lambda t: 80.0, capacity=2000.0),
          "2024-03-17 12:15Z", dataset="ods032")
    i = inputs(store.path)
    live = F.features_for_issuance(EXP, i, TARGET, "11h", live=True)
    replay = F.features_for_issuance(EXP, i, TARGET, "11h")
    assert (live["capacity"] == 1000.0).all() and (replay["capacity"] == 2000.0).all()
    day = live["local_hour"] == 12
    assert np.isclose(live.loc[day, "persistence_lf"], 0.05).all() and np.isclose(replay.loc[day, "persistence_lf"], 0.04).all()
    assert (live["weather_availability"] == "missing").all()  # no earlier run retrieved in time either
    assert (replay["weather_availability"] == "assumed").all()


def test_live_falls_back_to_an_earlier_run(store):
    base(store, run_retrieved="2024-03-17 10:30Z")
    run = pd.Timestamp("2024-03-16 06:00Z")
    store("weather-belgium", "20240316T06", "json", run_json(run), "2024-03-16 09:00Z", run=run.isoformat())
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h", live=True)
    assert (f["weather_availability"] == "fallback").all() and (f["weather_run_utc"] == run).all()
    assert f["lead_hours"].iloc[0] == 41 and f["weather_complete"].iloc[:2].all()
    assert not f["weather_complete"].iloc[-1]  # beyond the 49 labels of the earlier run: missing, not filled


def test_actuals_after_cutoff_not_used(store):
    # an Elia snapshot whose quarter-hours end after the cutoff must not move capacity
    gap = lambda t: 50.0
    store("elia", "ods032_belgium", "csv", elia_csv("2024-03-05", "2024-03-17 09:30", gap), "2024-03-17 09:45Z", dataset="ods032")
    late = elia_csv("2024-03-17 09:30", "2024-03-17 10:00", capacity=5000.0)
    store("elia", "ods210_total", "csv", late, "2024-03-17 09:50Z", dataset="ods210")
    run = pd.Timestamp("2024-03-17", tz="UTC")
    store("weather-belgium", "20240317T00", "json", run_json(run), "2024-03-17 08:00Z", run=run.isoformat())
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h", live=True)
    # cutoff 09:30: the 09:15 to 09:30 quarter-hour (capacity 1000) is the last one allowed
    assert (f["capacity"] == 1000.0).all() and (f["actuals_cutoff_utc"] == pd.Timestamp("2024-03-17 09:30Z")).all()


def test_persistence_falls_back_and_records_source_day(store):
    gap = lambda t: None if t.tz_convert("Europe/Brussels").date() == pd.Timestamp("2024-03-16").date() and t.hour == 11 else 50.0
    store("elia", "ods032_belgium", "csv", elia_csv("2024-03-05", "2024-03-17 09:30", gap), "2024-03-17 09:45Z", dataset="ods032")
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h")
    noon = f[f["local_hour"] == 12].iloc[0]
    other = f[f["local_hour"] == 13].iloc[0]
    assert noon["persistence_source_date"] == pd.Timestamp("2024-03-15").date()
    assert other["persistence_source_date"] == pd.Timestamp("2024-03-16").date()
    assert (f["weather_availability"] == "missing").all() and not f["weather_complete"].any()


def test_contract_types_and_generated_doc(store):
    base(store)
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h")
    assert list(f.columns) == list(F.CONTRACT)
    for c, (dtype, _, _) in F.CONTRACT.items():
        if dtype not in ("date",):
            assert str(f[c].dtype) == dtype, c
    assert (ROOT / "docs/feature-contract.md").read_text() == F.contract_markdown()


def test_missing_province_capacity_is_not_reweighted(store):
    antwerp = sources.config("belgium")["weather_points"]["provinces"][0]["ods032"]
    body = pd.read_csv(__import__("io").BytesIO(elia_csv("2024-03-05", "2024-03-17 09:30")))
    body = body[body["region"] != antwerp].to_csv(index=False).encode()
    store("elia", "ods032_belgium", "csv", body, "2024-03-17 09:45Z", dataset="ods032")
    run = pd.Timestamp("2024-03-17", tz="UTC")
    store("weather-belgium", "20240317T00", "json", run_json(run), "2024-03-17 08:00Z", run=run.isoformat())
    f = F.features_for_issuance(EXP, inputs(store.path), TARGET, "11h")
    assert not f["capacity_complete"].any() and not f["weather_complete"].any()
    assert f[["shortwave_radiation", "cos_zenith", "ghi_clearsky"]].isna().all().all()
    assert (f["weather_availability"] == "recorded").all() and (f["capacity"] == 1000.0).all()
