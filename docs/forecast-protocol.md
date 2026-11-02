# Forecast protocol and data contract

Frozen 2026-11-02, revised 2026-11-02 after review (observed coverage, persistence fallback, snapshots, DST
ambiguity, evaluation classification). Evidence: `docs/data-feasibility.md`, `data/manifest.json`. Changing anything here requires a
new dated version of this file before any model is selected.

Two experiments share the weather source and the evaluation discipline but are reported separately:

- **A. Belgian aggregate (primary, live):** Elia Belgium Total, "measured and upscaled".
- **B. Installation experiment:** PVDAQ system 2107. Its results do not validate A.

## A. Belgian aggregate

### Task

| Item | Value |
| --- | --- |
| Target | Belgian PV production, Elia region `Belgium`, grid connection `Total`, "measured and upscaled" |
| Unit | MWh per hour = mean MW of the four quarter-hours in the hour |
| Hour key | UTC start `s` of `[s, s + 1h)`; a quarter-hour belongs to the hour containing its start |
| Target day | local calendar day D in Europe/Brussels: 24 hours, or 23 / 25 on DST days, scored as they are |
| Primary issuance | 11:00 Europe/Brussels on D-1, matched to Elia's `dayahead11hforecast` |
| Secondary issuance | 18:00 Europe/Brussels on D-1, matched to Elia's `dayaheadforecast`, from target day 2024-08-16 |
| Lead time | 22 to 47 hours after the 00 UTC run of D-1 |

An hour is observed only if all four quarter-hours have a `measured` value; otherwise it is missing, never zero.
Coverage is reported from the data, observed and imputed apart:

- Observed: the measured target. Primary metrics use observed hours only.
- Imputed: an optional `night_zero_estimate` for a missing hour with the sun below the horizon throughout. It is
  stored in a separate column, never counted as observed coverage and never in primary metrics.

At freezing (`check_sources.py belgium`), 2024-03-15 to 2026-11-01: 962 calendar days, **746 with every hour
observed**, 22,872 of 23,088 hours observed. The 216 incomplete days are 2024-03-15 to 2024-10-15 (the
23:00 local hour lacks its last quarter-hour every day) and 2026-10-26 (the 00:00 local hour, the migration gap).
Days are not dropped for one missing hour; metrics are computed over observed hours, and daily metrics only
over days with every hour observed.

### Sources and joins

- ods032 for timestamps up to 2026-10-25 22:45 UTC (`region='Belgium'`), ods209 from 2026-10-26 00:00 UTC
  (`region='Belgium' and gridconnectiontype='Total'`, `measured_upscaled` parsed with a decimal comma).
- 2026-10-25 23:00 to 23:45 UTC is in neither dataset: the hour is missing (night).
- Live: ods210 with the same filter. Each retrieval is stored with its retrieval time; nothing is overwritten.

### Inputs available at issuance

| Input | Rule |
| --- | --- |
| Weather, 11:00 issuance | 00 UTC run of D-1 |
| Weather, 18:00 issuance | 06 UTC run of D-1 (archive from August 2024) |
| Elia actuals | quarter-hours that end at least 30 minutes before issuance |
| Monitored capacity | the value on the latest actual available at issuance, Belgium Total and per province |
| Elia forecasts | never used as model inputs; only as the benchmark |

Availability margins: Open-Meteo published the 2026-11-02 00 UTC run at 07:41 UTC. The 11:00 issuance is
09:00 UTC in summer and 10:00 UTC in winter, a margin of 1.3 to 2.3 hours. Historical delays are not published;
the backtest assumes the run was available. Live, if the run is missing at issuance, the forecast uses the
latest available run and records which one; such issuances are flagged and reported separately.

### Weather aggregation

- 11 points, one per province (`config/belgium.json`), fetched in one Single Runs request.
- Each variable for hour `[s, s + 1h)`: radiation at label `s + 1h` (mean of the preceding hour); temperature,
  wind and cloud cover as the mean of labels `s` and `s + 1h`.
- National features are the capacity-weighted mean over provinces, with weights from the monitored capacity
  known at issuance. Per-province features may also be used; #3 decides.
- One point per province is a simplification; #3 may add points if validation supports it.

### Benchmarks

- Persistence: for each local clock hour of D, the load factor (MW / monitored capacity) of the same clock hour
  on D-2, scaled to the capacity known at issuance. If that hour is not observed on D-2, the same hour on the
  latest earlier day where it is observed, at most D-8; otherwise persistence is missing for that hour and the
  hour is excluded from every model's metrics. The source day of each hour is recorded. DST: the 25-hour day
  uses the repeated clock hour's value for both occurrences; the 23-hour day skips the missing hour.
- Aggregate physics baseline: per-province POA irradiance for an assumed fleet geometry, times monitored
  capacity and a fixed performance ratio. The geometry and ratio are assumptions documented in #4, not fitted
  on test data.
- Elia `dayahead11hforecast` (11:00) and `dayaheadforecast` (18:00), averaged to hours like the actuals.
  Historical values are taken from ods032 and ods209 as published there; whether they equal what Elia published
  at the time is unverified. Days without an Elia forecast (2026-10-28 to 2026-11-01 for the 11:00 forecast)
  are excluded from the Elia comparison only, and the count is reported.
- Snapshots: `check_sources.py snapshot` saves the ods209 and ods210 responses under `data/raw/elia/` with a
  microsecond UTC timestamp and a random suffix in the filename, created exclusively so an existing snapshot
  is never replaced, and a sidecar holding the request, SHA-256, `request_started_at` and `retrieved_at`
  (response fully received). Availability at an issuance cutoff is judged by `retrieved_at`. It is **not scheduled yet**: scheduled collection is part of #6. Until it runs at least daily
  after 11:00 and 18:00, Elia forecasts that ods209 does not keep are lost, and the 2026-10-28 to 2026-11-01
  gap cannot be recovered from snapshots.
- The result is reported as measured; there is no target of beating Elia.

### Evaluation window

| Item | Value |
| --- | --- |
| First target day | 2024-03-15 (00 UTC run of 2024-03-14, the first archived run) |
| Last target day at freezing | 2026-11-01; the window grows daily |
| Days at freezing | 962 calendar days, 746 with every hour observed (see Task) |
| Runs before 2024-11-12 | 49r1 hindcasts, not forecasts available at the time; the final test period starts after this date |
| Known breaks | Elia publication switch 2026-10-26; monitored capacity updated in steps |

### What the results are

Results on archived data are a **retrospective evaluation under stated availability assumptions**, not an
operational backtest:

1. The weather run named by the issuance rule was available at issuance (only one publication time observed).
2. Open-Meteo's archive holds what ECMWF published. Runs before 2024-11-12 are hindcasts and fail this by
   definition; for later runs it is unverified. Starting the final test after 2024-11-12 removes the known
   hindcasts but does not prove availability.
3. Elia actuals, capacity and archived forecasts are as known at issuance (revisions unverified).

Every reported historical result names these assumptions. Operational claims are made only for issuances
recorded live by #6, with their inputs and retrieval times stored.

Elia history from 2020-09-30 may be used for analysis that needs no forecast weather, never for the operational
backtest. Folds, test period and selection criteria are set in #4 and #5.

## B. Installation experiment: PVDAQ 2107

### Task

| Item | Value |
| --- | --- |
| Issuance | 09:00 America/Los_Angeles on local day D |
| Target | hourly AC energy, kWh, from the revenue meter, every hour of local day D+1 |
| Hour key | UTC start `s` of `[s, s + 1h)`; local day grouping in America/Los_Angeles (whole-hour offsets) |
| Lead time | 31 to 56 hours after the 00 UTC run of D |
| Production at issuance | complete local day D-1 (replay assumption; PVDAQ itself publishes yearly) |
| Window | target days 2024-03-15 to 2025-12-31: 657 calendar days, 654 with every hour observed; final test after 2024-11-12 |

### Target construction

1. Drop exact duplicate rows; duplicates with different values are an error.
2. Localize `measured_on` to America/Los_Angeles (interval-end labels), convert to UTC. Spring-forward labels
   do not exist. On fall-back days the files hold the repeated 01:00 to 01:45 labels once, so which occurrence
   they belong to is unknown: these rows are ambiguous and are dropped (`ambiguous="NaT"`), not assigned.
3. Hour energy = sum of `kW x 0.25 h` over the four rows whose UTC end labels are `s + 15 min` to `s + 60 min`;
   fewer than four rows makes the hour missing. Missing hours stay missing in the measured target; an optional
   `night_zero_estimate` follows the same rule as in A (separate, excluded from coverage and primary metrics).
   Incomplete days at freezing: 2024-11-03 and 2025-11-02 (ambiguous hours) and 2025-12-31 (the 2025 file
   ends at 23:45, so the last hour is missing).
4. Clipping, outage days (2024-10-18, 2024-11-06) and other quality rules are decided in #3 without forecast scores.

### Weather

00 UTC run of D at the site; issuance at 16:00 or 17:00 UTC leaves 8.3 to 9.3 hours after the observed
availability time. Same variable alignment as A. Geometry: tilt 25 deg, azimuth 180 (compass, pvlib);
Open-Meteo's tilted-irradiance parameters use 0 = south, so pass `azimuth - 180`. Solar position at `s + 30 min`.

## Both experiments

- Not allowed as inputs: reanalysis, observed weather, the Historical Forecast API, runs initialised after the
  rule above, and actuals newer than the stated cutoff.
- Hourly refreshed 48-hour forecast: not evaluable historically (ECMWF runs every 6 hours; rapid-update models
  archived only from 2026-04-02). A live 48-hour product would be shown separately and never replace the fixed
  issuance used for scoring.
