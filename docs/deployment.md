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
