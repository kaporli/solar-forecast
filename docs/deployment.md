# Deployment (#6)

## Runtime

The packaged model (`artifacts/belgium/`, `solar_forecast.artifact`) runs as it is, in Python with the locked
dependencies (`uv sync --locked --no-dev`), on an always-on homelab worker separate from the web host. Nothing is
exported or reimplemented, so parity rests on the same code and library versions; `artifact.load` refuses other
library versions or changed prediction code.

## Model measurements on the worker

`uv run python tools/measure_artifact.py`, three runs on 2026-11-05; the raw outputs are in
`docs/deployment/worker-2026-11-05-run{1,2,3}.json`. Worker: one core of an Intel Core i7-10700T, 1 GB RAM,
Debian 13, Python 3.13.5. The artifact was built on Python 3.13.7 (arm64), with the same library versions.

| Measure | Worker (3 runs) | Protocol limit |
| --- | --- | --- |
| Fresh process, launched and timed from outside (interpreter startup and imports) | 0.61 to 0.64 s | |
| Imports, timed inside the measuring process | 0.52 to 0.53 s | |
| Artifact load | 0.41 to 0.44 s | |
| Prediction, one issuance (25 hours, median of 50) | 7.4 to 8.2 ms | 5 s per issuance |
| Peak memory of the process | 181 MB | 512 MB |
| Artifact size | 1.40 MB | 50 MB |
| Largest difference from the fixtures' predictions (146 rows) | 0 MWh | |

These cover the model only. Input retrieval, feature construction and the baselines are measured with the daily
pipeline, which needs its own resource measurement.

## Issuances (decided 2026-11-05 in #6)

- Both issuances run live, are archived and scored: 11:00 Europe/Brussels on D-1 with the 00 UTC run (primary
  evaluation series) and 18:00 on D-1 with the 06 UTC run (secondary), as in the protocol.
- Each issuance has its own identity, record, metrics and Elia benchmark (`dayahead11hforecast` for 11:00,
  `dayaheadforecast` for 18:00), and its own freshness and failure status.
- The public feed keeps both for each target day. An interface may default to the latest available issuance,
  labelled as such; the 18:00 forecast never replaces the 11:00 record.

## Scoring policy (decided 2026-11-05 in #6, recorded before prospective scoring)

- **Provisional:** an observed hour is scored once all four of its quarter-hours are available, and the score is
  marked provisional.
- **Finalized for scoring:** the actuals of a target day are those of the first successful ods209 retrieval made
  at least seven days after the target day ends in Europe/Brussels. Seven days is this project's operational
  policy, not evidence that Elia cannot revise later.
- **Missing hours** stay missing; coverage is reported, and daily-total metrics still require a complete day.
- **Versions:** a later correction or a newly supplied observation creates a new immutable score version that
  references the actuals used and their retrieval time. Earlier versions are kept.

## Daily issuance

- **Captures** (`solar-data snapshot`, 10:30, 11:30, 17:30 and 18:30 Europe/Brussels): Elia ods209 (last 14 days)
  and ods210, and today's 00 UTC and 06 UTC weather runs once they are at least three hours old. Live runs are
  fetched with 72 hours instead of the archive's 49, so the previous day's run still covers a target day when
  the protocol's fallback needs it; values for an hour do not depend on the length. A run counts as captured only
  with a complete response (`sources.run_complete`: every point, every requested hour, every variable, nothing
  missing after the run hour, whose radiation is always empty). An incomplete response is kept and the run is
  fetched again at the next capture.
- **Issuance** (`solar-data issue belgium --kind 11h|18h`, one minute after 11:00 and 18:00): only snapshots
  retrieved by the issuance time count. Elia: every retrieval that can hold a quarter-hour from 12 days before the
  target day (`index.reaching`: not indexed yet, or with an indexed row from then), so the window resolves as over
  the whole history and older non-null values are kept, while only those retrievals are read and verified.
  Weather: complete responses of runs from the last three days; the prescribed run, or the latest earlier run
  labelled `fallback`. The packaged model predicts; persistence and physics are recorded beside it.
- **Deadline:** an issuance must be published within 30 minutes of its time (`issue.LATE`), an operational
  allowance accepted on #16; the input cutoffs stay those of 11:00 and 18:00. It is checked at the start and again
  after the record is flushed to disk, immediately before the link that commits it. A later attempt, for example
  a persistent timer firing after downtime, is not issued; it leaves a `late` record, so it never enters the
  on-time evaluation.
- **Records:** `data/forecasts/belgium/<target>_<kind>.json`. Written to a temporary file, flushed to disk, then
  linked to its name, which fails if the name exists: a record is complete or absent, and a retry cannot replace
  it. Each holds the issuance time, deadline, attempt id, start time and computation time (`computed_at`), code
  commit, model checksums, feature version and content hash, the weather run and how it was obtained, capacity,
  the checksum of every input snapshot, and per hour the forecast, persistence and physics in MWh.
- **Before the commit**, a failed or late attempt publishes `<target>_<kind>.failed-<attempt>.json` the same way,
  with status `failed` or `late`, the error and no forecast; the attempt id (start time and a random suffix) keeps
  every attempt. There is no fallback model.
- **After the commit** the forecast stands. `<target>_<kind>.published.json` records the attempt, the publication
  time (taken immediately before the link), whether the directory sync succeeded (`durable`, `sync_error`) and any
  error removing the temporary file (`cleanup_error`). Either error exits non-zero and never writes a failure
  record. If the process stops between the link and this
  file, the forecast exists without it: its publication time is then unknown, but no later than the deadline.

## Scoring

`solar-data score belgium` (`score.py`) applies the scoring policy above. An issued record whose target day has
started is scored when it has no version yet, or when a retrieval not processed by an earlier run holds rows in
its target day. `retrievals.json` lists the processed retrievals by sidecar, not by retrieval time, so a snapshot
that appears later with an earlier retrieval time (an import, an overlapping collector) is still picked up and the
result equals a rebuild; without the file every issuance is rescored once, which writes nothing where nothing
changed.

- **Actuals:** Elia "measured and upscaled" Belgium Total, MWh per hour from the four quarter-hours
  (`targets.belgium_target`, the same exclusions as the backtest), resolved field by field over every retrieval
  stored so far (ods209 before ods210, newest non-null).
- **Final:** once, the measured values and capacity of the first ods209 retrieval made at least seven days after
  the target day ends in Europe/Brussels whose requested interval holds the whole local day: its UTC dates
  [start, end) must contain the day's UTC interval, so a November partition does not finalize 1 November (which
  starts on 31 October at 23:00 UTC); the monthly reconciliation requests start a day early, so each holds every
  local day of its month and can finalize a day the routine captures missed. The Elia benchmark is not taken from that retrieval: it stays resolved over
  all retrievals, so day-ahead forecasts that ods209 no longer carries keep their captured values.
- **Revised:** after the final version, the final retrieval and the retrievals made after it are resolved the same
  way (ods209 before ods210, newest non-null), so a later correction or a newly supplied observation appends a
  `revised` version, while older retrievals never fill what the final retrieval left out. The final version is
  kept, and later versions name it (`final_retrieval`).
- **Versions:** `data/scores/belgium/<target>_<kind>.v<N>.json`, published exclusively like the forecast records.
  A new version is written on finalization and otherwise only when the per-hour actuals or benchmark change. Each
  references the forecast record (file, checksum, attempt, model), the previous version's content hash, and every
  retrieval a used field came from (fields resolve independently, so actuals and benchmark can come from different
  retrievals), with its checksums and the fields it supplied.
- **Coverage:** observed and scored hours, the unscored hours, missing predictions and whether the day is
  complete. An hour is scored when all four quarter-hours are present, it has no exclusion and the forecast,
  persistence and physics are all finite, so the three share one sample; an hour missing one of them is listed in
  `missing_predictions` by predictor. Missing hours stay missing. Daily totals need a complete day.
- **Metrics:** the hourly metrics of the backtest (`evaluation.metrics`) for the forecast, persistence and physics
  on the scored hours. The Elia benchmark of the kind (`dayahead11hforecast` for 11:00, `dayaheadforecast` for
  18:00) is scored as published, on the scored hours where Elia has a value.
- **Summary:** `data/scores/belgium/summary.json`, replaced atomically on each run. Pools are split by kind and by
  weather availability (`recorded` for the prescribed run, `fallback`), so fallback issuances are reported apart:
  `<kind>_<availability>_all` over the latest version of each issuance and `<kind>_<availability>_final` over its
  final version.

## Public feed

`data/feed/belgium.json`, regenerated after every issuance and scoring run and replaced atomically. Contract,
states and fixtures: [feed.md](feed.md).
