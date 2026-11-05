# Deployment (#6)

## Runtime

The packaged model (`artifacts/belgium/`, `solar_forecast.artifact`) runs as it is, in Python with the locked
dependencies (`uv sync --locked --no-dev`), on an always-on homelab worker separate from the web host. Nothing is
exported or reimplemented, so parity rests on the same code and library versions; `artifact.load` refuses other
library versions or changed prediction code.

## Measurements on the worker

`uv run python tools/measure_artifact.py`, three runs on 2026-11-05. Worker: one core of an Intel Core i7-10700T,
1 GB RAM, Debian 13, Python 3.13.5. The artifact was built on Python 3.13.7 (arm64), with the same library versions.

| Measure | Worker (3 runs) | Protocol limit |
| --- | --- | --- |
| Interpreter and imports, fresh process | 0.53 to 0.57 s | |
| Artifact load | 0.41 to 1.13 s (first run cold) | |
| Prediction, one issuance (25 hours, median of 50) | 7.1 to 7.4 ms | 5 s per issuance |
| Peak memory of the process | 186 to 193 MB | 512 MB |
| Artifact size | 1.40 MB | 50 MB |
| Largest difference from the fixtures' predictions | 0 MWh | |

Feature construction and input retrieval are not included; they are measured with the daily issuance. The
physics baseline is not part of the selected model.
