# Data build: targets and features

Decisions for #3, applied by `uv run solar-data build <experiment>`. Column definitions are in
`docs/feature-contract.md`; coverage is in `docs/quality/`.

## Inputs

- Every retrieval is an append-only snapshot in `data/raw/snapshots/<source>/` with a sidecar (request,
  SHA-256, `request_started_at`, `retrieved_at`). The build reads only snapshots and verifies each checksum.
- The dataset manifest `data/datasets/<experiment>-v1.json` lists every snapshot used. `--inputs` rebuilds from
  exactly those and refuses a missing or changed file.
- Elia: ods032 (Belgium and the 11 provinces from 2024-03-01), ods209 and ods210 (grid connection Total). When
  snapshots overlap, the archive datasets take precedence over ods210 and newer retrievals over older ones,
  column by column, so an Elia forecast that ods209 lacks can still come from an ods210 snapshot.
- Weather: one snapshot per ECMWF IFS run named by an issuance rule, trimmed to 49 hours (Belgium) or
  57 hours (PVDAQ), all 11 province points in one request.

## Targets

- Belgium: `measured_upscaled_mwh`, the mean MW of the four quarter-hours of the hour. PVDAQ:
  `energy_kwh`, the sum of kW x 0.25 h over the four rows ending in the hour.
- An hour is `observed` only with four valid quarter-hours; otherwise its value is NaN, never zero.
- `exclusion` records why valid-looking data is not used: `negative`, `above_capacity` (Belgium, above monitored
  capacity), `above_dc_capacity` (PVDAQ, above 892.8 kW), `outage_day` (PVDAQ: zero energy over at least six
  observed daylight hours; the values are kept). `eligible` = observed and no exclusion; primary metrics use it.
- `night_zero_estimate` is 0.0 for an unobserved hour with the sun's upper limb below the horizon at both ends
  of the hour at every point. It is a separate column and never counts as observed.
- `day_complete`: every hour of the local day observed. Daily metrics use only such days.
- Neither source is a cumulative meter, so there is no differencing, first-reading or reset handling.
- Curtailment and clipping are not detectable from these sources and are not excluded: they are part of what
  was produced. PVDAQ's effective AC limit stays unknown (`config/installation.json`).
- PVDAQ fall-back labels (01:00 to 01:45 on 2024-11-03 and 2025-11-02) occur once and could belong to either
  hour, so they are dropped. That leaves 07:00, 08:00 and 09:00 UTC missing on those days.

## Features (v1)

- One function, `features_for_issuance`, produces the rows for one issuance. Batch training and historical
  replay call it for every issuance; inference calls it once with `live=True`, which drops every snapshot
  retrieved after the issuance and falls back to the latest earlier run (`weather_availability = fallback`)
  if the rule's run was not retrieved in time. Tests check that both paths give identical rows.
- Weather is weighted by monitored capacity per province known at the cutoff. Per-province features are not in
  v1; #5 may add them if validation supports it.
- Solar geometry is computed (pvlib) at the capacity-weighted centroid, replacing the legacy sunrise table.
- Persistence is a feature as well as the #4 baseline, built from eligible hours that ended before the cutoff.
- Models predict load factor (target / capacity) and are scored in MWh or kWh, because Belgian monitored
  capacity grew 35 % over the window. A step in capacity between issuance and target is not anticipated;
  that is what was known at issuance.
- Rows are row-local: nothing is computed across issuances, so the build fits nothing. Learned preprocessing
  (scalers, imputers) goes inside the model pipeline and is fitted per fold on training rows only (#4, #5).
- Historical rows are a retrospective evaluation (`weather_availability = assumed`) under the assumptions in
  `docs/forecast-protocol.md`.
