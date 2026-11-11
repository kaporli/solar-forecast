# Local view

`data/feed/belgium-local.json`, written by `solar-data local belgium` (`local.py`), scheduled at 11:40 and 18:40
Europe/Brussels, after the captures that follow each issuance. It answers "what would a system at this place
produce on the forecast day", and says which level of data each number is:

| Level | What it is | Measured |
| --- | --- | --- |
| Lattice point | the physics reference per kWp for a tilt and orientation, on the weather forecast at that point | no |
| Province | Elia's measured and upscaled production and its day-ahead forecast, per kW of monitored capacity | yes |
| Belgium | the forecast of this repository, scored against Elia's (`feed.md`, `history.md`) | yes |

Nothing is measured below province level: no public source has production per municipality, street or roof. The
lattice values are therefore a forecast only. They are not fitted and are not scored; the national model is not
involved. Their one check against measurements is the province table (`recent`), which sets the reference
geometry's daily total beside what Elia measured per kW installed. The two differ by construction: one is a
35 degree south-facing system with a fixed performance ratio, the other the installed fleet with its mix of
orientations and losses.

## Method

- **Lattice:** 391 points 0.08 degrees of latitude by 0.125 of longitude apart (about 9 km), inside the eleven
  NUTS 2 regions of Belgium, each with its province (`config/local.json`, written by `tools/make_grid.py`). The
  spacing is close to one cell of ECMWF IFS HRES on Open-Meteo (about 8 x 11 km over Belgium), so a finer lattice
  would repeat cells. Brussels is smaller than a cell and holds one point.
- **Weather:** the run the issuance rule names for the latest due issuance (00 UTC for 11:00, 06 UTC for 18:00),
  49 hours, fetched for the lattice in two requests and stored in the snapshot archive as
  `weather-belgium-grid`. There is no fallback run: if it cannot be fetched the command fails, and the previous
  file stands until its target day ends.
- **Energy:** `baselines.dc_per_kw` (plane-of-array irradiance, SAPM cell temperature, PVWatts temperature
  response) times the performance ratio of `config/evaluation.json` (`physics.belgium`), per hour of the target
  day, for 16 geometries: flat, and 15, 35 and 50 degrees facing east, south-east, south, south-west and west.
  No shading, no inverter limit. Hours without production anywhere are left out.
- **Provinces:** Elia's quarter-hours resolved over every stored retrieval (as the issuance does), hourly as the
  target (`targets.belgium_target` with the province as region), divided by the monitored capacity of the hour.

One immutable record per issuance, `data/local/belgium/<target>_<kind>.json`, published like a forecast record
(complete or absent, never replaced) with the checksums of its weather snapshots. A rerun with the record present
only rewrites the public file.

## Contract, version 1

| Field | Meaning |
| --- | --- |
| `schema`, `version` | `kaporli.solar-forecast.local`, `1`. A change that removes or redefines a field raises the version. |
| `generated_at` | When the file was written (UTC). |
| `target_date`, `kind`, `issuance_utc` | The target day (Europe/Brussels) and the issuance (`11h` or `18h`) the file belongs to. |
| `end_utc` | End of the target day. From then a reader shows nothing as current. |
| `weather` | `model`, `run_utc`, `retrieved_at`. |
| `unit` | Wh per kWp per hour. |
| `assumptions` | `performance_ratio`, `transposition`, `albedo`, `cell_temperature`, `gamma_pdc_per_c`. |
| `hours[]` | `start_utc`, `local_hour`: the hours every series below covers, [start, start + 1h). |
| `geometries[]`, `reference` | `tilt_deg`, `azimuth_deg` (compass, 180 = south); the index of the reference geometry (35 degrees south). |
| `grid` | `lat_step`, `lon_step` of the lattice, in degrees. |
| `cells[]` | `lat`, `lon`, `province`, `wh`: one series per geometry, in the order of `geometries`. |
| `provinces[]` | Below. |
| `attribution` | Sources and licences to display. |

Per province:

| Field | Meaning |
| --- | --- |
| `id` | Elia's region name (`config/belgium.json`, `ods209`). |
| `cells` | Lattice points in it. |
| `wh` | Mean of its points' reference series. |
| `capacity_mw` | Latest monitored capacity; null without Elia data. |
| `elia_wh` | Elia's day-ahead forecast of the kind (`dayahead11hforecast` or `dayaheadforecast`) per hour of `hours`, Wh per kW of monitored capacity; null where not published. |
| `recent[]` | The seven days before today: `date`, `measured_kwh` (kWh per kW; null unless every hour of the day is observed), `elia_kwh` (the same for Elia's day-ahead forecast), `physics_kwh` (the reference total of that day's local record, of the same kind if there is one; null before the first record). |

A null is missing, never zero. Not in the file: snapshot names, checksums, paths and hosts (they stay in the record).

## Size and cost

About 240 kB in November (9 hours with production), 75 kB compressed; the hours grow with the day length. The
weather fetch is 391 locations per run, twice a day, within Open-Meteo's free limits (10,000 a day, 600 a
minute). On the development machine the run took 26 s, 2.4 s of it CPU.
