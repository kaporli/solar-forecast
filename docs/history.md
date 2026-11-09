# Public issuance history (#7)

`data/feed/belgium-history.json`, written by `solar-data feed belgium` (`history.py`) together with the feed, so
after every `issue` and `score` run, and replaced atomically the same way. It lists every issuance made so far with
its forecast and, once scored, the actual production and Elia's day-ahead forecast for each hour. The feed
([feed.md](feed.md)) says which forecast to show now; the history is for looking back.

Sample: `fixtures/history/belgium.json`, the history built from the worker's records and scores of 2026-11-09
morning (five issuances, three with provisional scores). All values are real.

## Contract, version 1

| Field | Meaning |
| --- | --- |
| `schema`, `version` | `kaporli.solar-forecast.history`, `1`. A change that removes or redefines a field raises the version. |
| `generated_at`, `experiment`, `target`, `attribution` | As in the feed. |
| `issuances[]` | One entry per issuance, oldest `issuance_utc` first, below. |

Per issuance:

| Field | Meaning |
| --- | --- |
| `kind`, `target_date` | `11h` or `18h`; local target day. |
| `status` | `issued`; or `failed` or `late` when the issuance left only failure records (`late` wins). An issuance with no record at all is not listed. |
| `issuance_utc` | The issuance time (for a failed or late one, the time it was due). |
| `published_at`, `weather`, `capacity_mw`, `model`, `code_commit` | As in the feed's forecast (`weather` without `retrieved_at`). Null when not issued. |
| `score` | Null until the issuance is scored; then its latest score version: `status` (`provisional`, `final` or `revised`, [evaluation-protocol.md](evaluation-protocol.md)), `version`, `scored_at`, `coverage` (`hours`, `observed`: hours with all four quarter-hours, `scored`, `day_complete`) and `metrics` per predictor (`forecast`, `persistence`, `physics`, `elia`), as in the score file. |
| `hours[]` | Empty when not issued. Otherwise `start_utc`, `end_utc`, `local_hour`, `daylight`, `forecast_mwh`, `persistence_mwh`, `physics_mwh` (from the record), `elia_mwh` (Elia's day-ahead forecast of the same issuance time), `actual_mwh` (measured and upscaled) and `scored` (from the score version; null and false before scoring). |

A null value is missing, never zero: an unobserved hour has `actual_mwh` null. Metrics on a provisional score can
change in a later version; only `final` and `revised` are settled. Not in the history: attempt ids, record and
snapshot names, retrieval checksums, paths, hosts and errors (these stay on the worker).
