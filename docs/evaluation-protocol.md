# Evaluation protocol

Frozen 2026-11-04, before any learned-model comparison. The machine-readable version is
`config/evaluation.json`; `solar_forecast.evaluation` reads it, and every results file records its SHA-256.
Changing a value requires a new dated version of this file and a reason. Validation results:
`docs/evaluation/<experiment>-baselines.md`, produced by `uv run solar-data evaluate <experiment>`.

## Periods

| | Belgium | PVDAQ 2107 |
| --- | --- | --- |
| Training from | target day 2024-03-15 | target day 2024-03-15 |
| Validation folds | V1 2024-11-13 to 2025-01-31, V2 to 2025-04-30, V3 to 2025-07-31, V4 to 2025-10-31 | V1 2024-11-13 to 2025-01-31, V2 to 2025-04-30, V3 to 2025-06-30 |
| Final test (contiguous) | 2025-11-01 to 2026-10-31, twelve months | 2025-07-01 to 2025-12-31, six months |
| Primary kind | 11h | 09h |

- Validation starts on 2024-11-13, the first target day whose run (D-1) is initialised on or after 2024-11-12 00Z,
  when 49r1 became operational. Earlier rows may be used for training only. Results report the number of
  validation rows on hindcast runs (zero).
- Belgium: the twelve-month test covers every season once and contains the Elia switch of 2026-10-26; test
  results are reported before and from that date. Target days from 2026-11-01 are not part of the frozen
  evaluation; they belong to prospective scoring in #6.
- PVDAQ: the dataset ends 2025-12-31, so the test is half a year. Its results do not represent a full year.
- The final test is locked: `backtest` raises on it unless explicitly unlocked, which only the locked deployment
  candidate and recipe of #5 may do. No test score, for baselines either, exists before then.

## Training labels and folds

- A candidate is fitted once per fold, before the fold's first issuance, on target days up to fold start minus
  two days, the last day whose labels are complete at issuance (D-1 at 11:00 or 09:00 local), across earlier
  folds too (expanding window). It is not refitted inside a fold.
- Training rows: target eligible, `weather_complete`, `capacity_complete`. The backtest asserts that no training
  label falls after the fold's first issuance and passes candidates feature columns only.
- Every target hour of D has a fixed lead time per issuance kind, so there is no horizon-related exclusion.
  Learned preprocessing is fitted inside the candidate on the fold's training rows.

## Scoring

- Scored hour: target eligible, `weather_complete`, `capacity_complete`, persistence available. Every candidate
  is scored on the same hours.
- Daylight: the sun's upper limb above the horizon at the start or end of the hour, at the unweighted mean of
  the 11 province points (Belgium) or at the site (PVDAQ).
- Complete day: every hour of the local day scored, and the target `day_complete`.
- Every candidate's prediction is clipped to [0, capacity x 1 h]. The Elia benchmark is scored as published,
  on scored hours that have an Elia forecast, with the left-out count reported.
- Primary metric: daylight hourly MAE of the primary kind, pooled over validation folds (MWh or kWh per hour).
- Also reported: all-hours MAE, daylight and all-hours RMSE, signed bias (prediction minus actual), daily-total
  MAE and bias over complete days, daylight MAE by lead hour and by fold, and the scored hour and day counts.
- Slices, defined before any result: season (DJF, MAM, JJA, SON) and forecast sky (daylight mean forecast cloud
  cover per day: clear under 30 %, mixed 30 to 70 %, overcast over 70 %). A slice with fewer than 14 scored
  days is listed but not reported.

## Comparing candidates

- Paired moving-block bootstrap over target days of the daylight MAE difference on identical hours: circular
  blocks of 14 consecutive calendar days, 2,000 resamples, seed 0, 95 % percentile interval. A day without
  scored hours (Belgium: 2025-08-07 to 2025-08-10) keeps its place in the calendar and contributes nothing.
- Every candidate must return a finite prediction for every scored hour; a missing one fails the evaluation
  instead of dropping the hour. A comparison subset (the Elia benchmark) counts a day in daily totals only if
  every hour of that local day (23, 24 or 25) is in the subset.
- Block length: forecast errors follow weather regimes lasting several days, and the error of an uncalibrated
  model also drifts with the season. For physics minus persistence in validation (primary kind), the
  autocorrelation of the daily difference at lags 1, 3, 5, 7, 10, 14 and 21 calendar days is 0.23, 0.22, 0.18,
  0.11, 0.12, 0.07 and 0.08 for Belgium, and 0.29, 0.08, 0.08, 0.08, 0.06, -0.06 and 0.07 for PVDAQ. 14 days is
  the shortest block after which Belgium stays under 0.1. The Belgian interval depends on it: -260 to -55 MWh
  with 7-day blocks, -276 to -25 with 14, -282 to -3 with 21; PVDAQ barely moves (-38 to -14, -40 to -12,
  -41 to -11 kWh).
  Each report lists the autocorrelation at lags 1 to 14 so the choice can be rechecked for every comparison.
- Selection rule for #5: a learned candidate replaces the best baseline only if its pooled validation daylight
  MAE is at least 5 % lower, the upper bound of the 95 % interval of the difference is below zero, and it is
  better in all validation folds but at most one. Among candidates within 2 % of the best pooled daylight MAE,
  the one with fewer inputs, then lower inference cost, is chosen.
- Resource limits for the deployment candidate: inference for one issuance within 5 s and 512 MB peak memory on
  the always-on deployment host (measured in #6), model artifact at most 50 MB, training within 60 minutes on
  a development machine. The 2024 random forest artifact (85 MB) would not meet this limit.
- The Elia forecast is a benchmark, never a selection target.

## Baselines

- Persistence: load factor of the same local clock hour on D-2, else the latest earlier eligible day up to D-8,
  times the capacity known at issuance (`docs/forecast-protocol.md`).
- PVDAQ physics (installation reference): tilt 25 deg, azimuth 180 deg, DC 892.8 kW (documented); Hay-Davies
  transposition, albedo 0.2, SAPM open-rack cell temperature, -0.4 %/degC, PVWatts default DC losses (14.08 %),
  PVWatts inverter at 96 % nominal efficiency, AC limit 720 kW (24 x 30 kW maximum output of the ABB
  TRIO-27.6-TL-OUTD per its US datasheet; the meter peak of 707.84 kW is below it; the effective export limit is
  unknown). Hour energy is the mean AC power over the hour times 1 h.
- Belgian physics (aggregate reference): per province, the same chain for an assumed fleet geometry (tilt 35 deg,
  south), SAPM close-mount cell temperature, -0.4 %/degC and a fixed performance ratio of 0.80; national
  prediction = monitored capacity x capacity-weighted mean over provinces. Its inputs come from the same selection
  as the features (`features.issuance_inputs`, same `live` flag): live, a snapshot retrieved after the issuance
  is invisible to both; a row whose run, snapshot retrieval or capacity differs from that selection is refused.
- Nothing in either physics baseline is fitted. Its bias in validation (Belgium: about +500 MWh per daylight hour)
  is recorded rather than corrected; calibration is part of the learned comparison in #5.
- The original random-split MAE of 0.066 is not comparable with these scores and is not used as a reference.

## Open for review

Choices made here without precedent in the earlier issues, recorded for the reviewer: the test periods, the
5 % improvement threshold and fold rule, the resource limits, the Belgian fleet geometry and performance ratio,
and the PVDAQ AC limit.
