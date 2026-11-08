# Data build: targets and features

Decisions for #3, applied by `uv run solar-data build <experiment>`. Column definitions are in
`docs/feature-contract.md`; coverage is in `docs/quality/`.

## Inputs

- Every retrieval is an append-only snapshot: a sidecar in `data/raw/snapshots/<source>/` (request,
  SHA-256 of the response bytes, `request_started_at`, `retrieved_at`). Since 2026-11-05 the body is stored once
  per distinct content, gzip-compressed and named by that SHA-256, in `data/raw/objects/`; identical responses
  share one object and keep one sidecar each. Earlier snapshots keep their plain payload next to the sidecar and
  are never rewritten. The build reads only snapshots and verifies each checksum on the original bytes.
- Elia collection (storage decision in #6): routine captures fetch the last 14 days of ods209 (plus the forecast
  days published ahead) and the ods210 window, at 10:30 and 17:30 (inputs before the 11:00 and 18:00 issuances)
  and 11:30 and 18:30 Europe/Brussels (Elia's new day-ahead forecasts). `solar-data snapshot --reconcile` fetches
  every calendar month of ods209 since 2026-10, once a month, to find later revisions (each request starts a day
  early, so it holds every Brussels day of its month whole); a revision is available
  from the retrieval that found it, never earlier. Requests are bounded by UTC dates and ordered by time and
  region, so unchanged content gives identical bytes.
- Elia observations are read through a SQLite index beside the archive (`data/raw/index.sqlite`,
  `solar_forecast/index.py`), filled from the snapshots on first use and rebuildable by deleting it. It stores each
  distinct quarter-hour version once and which retrieval delivered it, so builds no longer parse every snapshot
  and an unchanged observation keeps its provenance. Resolution is the archive rule below, field by field, over
  exactly the snapshots a build is given (a pinned build: its manifest's), restricted to retrievals up to the
  as-of time and optionally to recent quarter-hours. Rebuilding `belgium-v1` through it gives the same content
  hashes.
- The dataset manifest `data/datasets/<experiment>-v1.json` lists every snapshot used with the SHA-256 of its
  payload and of its sidecar. The sidecar decides availability (`retrieved_at`) and identity (run, dataset), so
  `--inputs` rebuilds from exactly those and refuses a missing file or a changed payload or sidecar.
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
- Neither source is a cumulative meter (both give interval-average power), so cumulative differencing,
  first-reading handling and counter-reset tests are not applicable (agreed in review of #9). Gap, duplicate,
  unit and DST handling apply.
- Curtailment and clipping are not detectable from these sources and are not excluded: they are part of what
  was produced. PVDAQ's effective AC limit stays unknown (`config/installation.json`).
- PVDAQ fall-back labels (01:00 to 01:45 on 2024-11-03 and 2025-11-02) occur once and could belong to either
  hour, so they are dropped. That leaves 07:00, 08:00 and 09:00 UTC missing on those days.

## Features (v1)

- One function, `features_for_issuance`, produces the rows for one issuance. Batch training and historical
  replay call it for every issuance; inference calls it once with `live=True`, which drops every snapshot
  retrieved after the issuance and falls back to the latest earlier run (`weather_availability = fallback`)
  if the rule's run was not retrieved in time. Tests check that both paths give identical rows.
- Weather is weighted by monitored capacity per province known at the cutoff. National capacity and all 11
  province capacities must be known and positive; otherwise `capacity_complete` is false and weather and solar
  geometry are null, never re-weighted over the remaining provinces. Per-province features are not in v1; #5 may
  add them if validation supports it.
- Solar geometry is computed (pvlib) at the capacity-weighted centroid, replacing the legacy sunrise table.
- Persistence is a feature as well as the #4 baseline, built from eligible hours that ended before the cutoff.
- Models predict load factor (target / capacity) and are scored in MWh or kWh, because Belgian monitored
  capacity grew 35 % over the window. A step in capacity between issuance and target is not anticipated;
  that is what was known at issuance.
- Rows are row-local: nothing is computed across issuances, so the build fits nothing. Learned preprocessing
  (scalers, imputers) goes inside the model pipeline and is fitted per fold on training rows only (#4, #5).
- Historical rows are a retrospective evaluation (`weather_availability = assumed`) under the assumptions in
  `docs/forecast-protocol.md`.
