# Data feasibility

Checked 2026-11-02. Reproduce with `uv run tools/check_sources.py` (`belgium` or `pvdaq` to run one part,
`--fixtures` to rewrite `fixtures/`). The protocols that follow from this are in `docs/forecast-protocol.md`.

## Scope (decided 2026-11-02)

Zero budget is a hard constraint. The live model forecasts **Belgian aggregate solar production** as published
by Elia, labelled "measured and upscaled", with Open-Meteo for weather. Elia's own day-ahead forecasts are the
external benchmark. PVDAQ system 2107 stays as a separate installation-level experiment; its results do not
validate the Belgian model.

## Decision

| Question | Result |
| --- | --- |
| Belgian historical evaluation | **Go, retrospective.** Elia Belgium Total with archived ECMWF IFS runs at 11 province points, 2024-03-15 to 2026-11-01 (growing daily): 962 calendar days, 746 with every hour observed. Results hold under stated availability assumptions (`docs/forecast-protocol.md`) |
| Belgian live scoring | **Go.** Elia ods210 publishes each quarter-hour about 20 minutes after it ends; no account, CC BY 4.0 |
| Elia benchmark at matched cutoffs | **Go, with gaps.** 11:00 and 18:00 day-ahead forecasts archived since 2020; missing in the new history dataset for 2026-10-28 to 2026-11-01. A snapshot command exists; scheduling it is part of #6 |
| Installation experiment (PVDAQ 2107) | **Go.** 657 target days (654 with every hour observed), 2024-03-15 to 2025-12-31; no live scoring (yearly batches) |
| Single Belgian rooftop | **Dropped.** No free live source: the original export is lost, the PVOutput API needs a paid account, and scraping it was rejected |

## Belgian actuals and benchmark: Elia open data

Licence: CC BY 4.0 (Elia Open Data Licence). Attribution: "Source: Elia Open Data (Elia Transmission Belgium SA)".
Values are MW, the mean over each 15-minute interval; `datetime` is the interval start in UTC. Elia describes
the actuals as "measured and upscaled": an estimate for the whole fleet from monitored sites, not a meter
reading.

### Datasets and the 2026-10-26 switch

Elia replaced the publication on 2026-10-26 at 10:00:

| Dataset | Role | Range (Belgium) |
| --- | --- | --- |
| ods032 | history up to the switch | 2020-09-30 22:00 to 2026-10-25 22:45 UTC, 212,740 quarter-hours, none missing |
| ods209 | history from the switch, refreshed daily (yesterday complete) | 2026-10-26 00:00 UTC onward |
| ods210 | near real time, about 10 days: previous local day to D+8 | live |
| ods087 | old near-real-time feed | discontinued, empty |

Changes at the switch, and how they were checked:
- **Grid connection type added** (DSO, TSO, Total). ods209 Total equals DSO + TSO to within 1e-12 MW.
- **Continuity:** ods032 "Belgium" ends at 12,128.3 MW monitored capacity; ods209 "Belgium / Total" starts at
  12,107.7 MW (0.17 % lower), so the old series corresponds to Total. Daily peak load factors either side of the
  switch are in the same range (0.13 to 0.57 before, 0.17 to 0.48 after); one week is too short to rule out a
  small level shift.
- **Seam gap:** 2026-10-25 23:00 to 23:45 UTC (local 00:00 to 01:00 on 26 October) exists in neither dataset.
  Night, so zero production.
- **Regions:** Flanders and Wallonia were removed; the 11 provinces remain under new names
  (`config/belgium.json` maps them). Province capacities agree within about 2 %, except Liège (854 to 963 MW).
- **Week-ahead forecast replaced** by a D+10 forecast.
- **Format:** ods209 publishes `measured_upscaled` as text with a decimal comma ("0,000"); ods210 as a number.

### Quality in the evaluation window

- Missing `measured`: 215 quarter-hours, one per day from 2024-03-15 to 2024-10-15, always the last quarter-hour
  of the local day (21:45 or 22:45 UTC); none in 2025 or 2026. With the migration gap, 216 of 962 target days
  have a missing hour: 746 days and 22,872 of 23,088 hours are fully observed.
- Missing `dayahead11hforecast`: none from 2024; `dayaheadforecast` (18:00): none.
- ods209 since the switch: actuals complete; `dayahead11hforecast` present only for 26 and 27 October and
  `dayaheadforecast` for 26, 27 and 29 October. These days are lost unless Elia backfills them.
- Monitored capacity rose from 8,987 MW (2024-03-15) to 12,128 MW and is updated in steps, sometimes flat for
  months (January to June 2026). Its update rule is not documented.
- Peak measured value 8,559 MW (2026-04-30 11:30 UTC).

### Live behaviour

- At 11:35 UTC on 2026-11-02, ods210 held actuals up to the interval starting 11:15 UTC.
- At 12:35 local time the 11:00 day-ahead forecast for the next day was already present.
- Revisions: ods210 and ods209 agree exactly on the one shared day (96 quarter-hours). Later revisions of
  actuals or forecasts are not ruled out. `check_sources.py` saves every Elia response as a timestamped
  snapshot with its request and checksum; scheduled daily collection is part of #6 and not running yet.
- `mostrecentforecast` is overwritten as new forecasts arrive, so it is not a fixed issuance and cannot serve as
  a matched benchmark.

## Weather: ECMWF IFS 9 km single runs (both experiments)

- Open-Meteo Single Runs, `models=ecmwf_ifs`. ECMWF IFS HRES 9 km runs from 2024-03-14; other models from
  2026-04-02 only.
- **Belgium:** one request returns all 11 province points. Sampled every 21 days from 2024-03-15, the 00 UTC
  run of D-1 had 23 to 25 non-null hours for local day D at every point, and 06 UTC runs from August 2024 the
  same (row-count check, 2026-11-02).
- **PVDAQ:** 47 sampled 00 UTC runs from 2024-03-14 to 2025-12-30 passed the same row-count check.
- **Pending:** the stricter check in `check_run` (exact expected UTC labels, unique; radiation at each hour's
  end label, temperature, wind and cloud cover at both start and end) has not yet completed a sweep. The rerun
  on 2026-11-02 was interrupted by Open-Meteo errors (503 overloaded, then a 500 on the 2025-12-18 00 UTC run).
- These are sampled feasibility checks, not verified continuous coverage. Full coverage is checked when #3
  builds the dataset; a missing run makes that issuance missing.
- 06 UTC runs fail with HTTP 400 up to 2024-08-01 and are present from 2024-08-15.
- Units: W/m2, degC, m/s, %. Radiation is the mean over the preceding hour.
- Publication: on 2026-11-02 Open-Meteo reported the 00 UTC run available at 07:41:25 UTC. That is the only
  availability time observed; historical delays are an assumption.
- Open-Meteo labels the archive start "IFS Cycle 49R1 hindcasts". ECMWF made 49r1 operational on 2024-11-12, so
  earlier runs are re-runs, not what was available at the time.
- Rejected: Historical Forecast API (stitched short-range runs), Previous Runs API (fixed 24-hour lead offsets,
  not an issuance), reanalysis and observations.

## Installation experiment: PVDAQ system 2107

Arbuckle, California; 892.8 kWp DC, fixed 25 deg tilt, azimuth 180, 24 ABB TRIO-27.6 inverters
(`config/installation.json`). CC-BY-4.0, anonymous S3; files and SHA-256 in `data/manifest.json`.

- Column `meter_revenue_grade_ac_output_meter_149578`: `kw`, aggregation `avg`, source `METER`.
- 99,346 rows; 29,179 exact duplicate rows (2024 file, Feb to Nov), all with identical values.
- After de-duplication: 2024-01-01 00:15 to 2025-12-31 23:45, no NaN or negatives. Missing slots only in the
  spring-forward hours; fall-back days hold 96 rows (repeated 01:00 hour collapsed).
- Timestamps: America/Los_Angeles wall time, interval-end labels (clear-day fit: median offset 4 minutes,
  IQR 2 to 5; start labels would need 11).
- 1,373.4 MWh in 2024, 1,289.1 MWh in 2025. Peak 707.84 kW, above the inverters' 662.4 kW nominal sum: the
  effective AC limit is unknown. All-zero days: 2024-10-18, 2024-11-06.
- PVDAQ publishes this system yearly (2024 file on 2025-06-16, 2025 file on 2026-05-21): no live scoring.

## Candidates not selected

| Candidate | Reason |
| --- | --- |
| Original Belgian rooftop | export and metadata lost; would overlap the forecast archive by 37 days at most |
| PVOutput.org | API reads of other systems need paid donation mode (tested: `401 Inaccessible System ID`); free web access limited to 6 months of 5-minute data; scraping rejected |
| Open Climate Fix UK PV | not live (updated every few months, data to April 2025), gated |
| Other PVDAQ systems | 2105 static before the archive; `csv/pvdata` not updated since 2022 |
| Owner API keys (SolarEdge, Enphase, ...) | free, but needs an owner willing to share; none available |

## Unresolved

- Whether Elia revises actuals or archived forecasts after publication, and how monitored capacity is updated.
- Whether ods032 day-ahead values are exactly as published at 11:00 and 18:00 at the time.
- A possible small level shift at the 2026-10-26 switch, and the Liège capacity change.
- Historical publication delays of the ECMWF runs; Open-Meteo's archive after 2024-11-12 as published or
  reconstructed.
- Fleet orientation and tilt mix for the aggregate physics baseline.
- PVDAQ 2107: effective AC limit, cause of the two zero days.
- Fall-back hours in PVDAQ: which occurrence the single set of 01:00 to 01:45 labels belongs to.

## Attribution

- Elia Open Data (Elia Transmission Belgium SA), CC BY 4.0.
- Open-Meteo.com, CC BY 4.0; model output ECMWF IFS HRES.
- PVDAQ: NREL PV Data Acquisition public dataset, Open Energy Data Initiative, DOI 10.25984/1846021, CC BY 4.0.
