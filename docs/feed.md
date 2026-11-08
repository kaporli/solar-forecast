# Public feed (#6)

`data/feed/belgium.json`, written by `solar-data feed belgium` (`feed.py`) and after every `issue` and `score` run,
so at 11:01, 11:45, 18:01 and 18:45 Europe/Brussels. It is replaced atomically: written and flushed to a temporary
file, renamed over the old one, then the directory is flushed. A reader sees the old file or the new one, never a
part. A failed issuance does not remove the last valid forecast: the feed shows it as `stale`.

Sample feeds for the portfolio, one per state: `fixtures/feed/belgium-{fresh,fallback,stale,unavailable}.json`,
made by `tools/feed_fixtures.py` from the worker's issued records of 2026-11-08 and 2026-11-09. The forecast values
are real; in all but `fresh` the state of the 18h issuance is constructed (a relabelled weather run, a failure
record in place of the 2026-11-09 18h record).

## Contract, version 1

| Field | Meaning |
| --- | --- |
| `schema`, `version` | `kaporli.solar-forecast.feed`, `1`. A change that removes or redefines a field raises the version. |
| `generated_at` | When the file was written (UTC, ISO 8601). |
| `experiment` | `belgium`. |
| `target` | What is forecast: `label` ("measured and upscaled Belgium Total (Elia)"), `area`, `unit` (MWh per hour), `interval` (hourly, [start, end)), `timezone` of local hours. |
| `latest` | The kind (`11h` or `18h`) of the most recently issued forecast shown, or null. An interface may default to it, labelled as such; the 18h forecast never replaces the 11h one. |
| `issuances.11h`, `issuances.18h` | One entry per kind, below. |
| `attribution` | Sources and licences to display. |

Per kind:

| Field | Meaning |
| --- | --- |
| `state` | `fresh`, `fallback`, `stale` or `unavailable` (below). |
| `reason` | Null when fresh or fallback; otherwise `failed`, `late` or `missing` for the latest due issuance. |
| `expected_issuance_utc`, `deadline_utc` | The latest issuance due at `generated_at` and its publication deadline (30 minutes later). |
| `valid_until` | When this entry stops being current: the next deadline for fresh and fallback, the end of the shown forecast's target day for stale, null for unavailable. A reader past it should treat the entry as stale even if the file was not regenerated (for example when the worker is down). |
| `forecast` | Null when unavailable; otherwise the forecast shown, below. |

Forecast:

| Field | Meaning |
| --- | --- |
| `target_date` | Local target day (Europe/Brussels). 23, 24 or 25 hours. |
| `issuance_utc`, `published_at`, `computed_at` | Issuance time (11:00 or 18:00 local on the day before), when the record was published, when it was computed. `published_at` is null if the publication record was not written. |
| `weather` | `run_utc` (the ECMWF IFS HRES run used), `availability` (`recorded`: the prescribed run; `fallback`: an earlier run), `retrieved_at`. |
| `actuals_cutoff_utc` | Latest Elia quarter-hour the issuance could use. |
| `capacity_mw` | Elia's monitored capacity for Belgium at issuance. |
| `model` | `candidate`, `model_sha256`, `feature_version`. |
| `code_commit` | Commit of this repository that issued it. |
| `hours[]` | `start_utc`, `end_utc`, `local_hour`, `lead_hours` (from issuance to the hour's start), `forecast_mwh`, `persistence_mwh`, `physics_mwh`. A null value is missing, never zero. |

## States

Per kind, at `generated_at`:

- **fresh:** the latest due issuance was published with its prescribed weather run.
- **fallback:** the same, with an earlier weather run because the prescribed one was not available. There is no
  fallback model; the model is always the packaged one.
- **stale:** the latest due issuance left a failure record (`failed`, `late`), or its deadline has passed without
  a record (`missing`). The previous forecast is shown while its target day lasts.
- **unavailable:** no forecast whose target day has not ended.

Between an issuance time and its deadline, while no attempt has failed, the previous forecast keeps its state: the
new one is not due yet.

Not in the feed: input snapshot names, file paths, hosts, attempt ids, errors and tracebacks (these stay in the
records on the worker), and scores (`data/scores/belgium/summary.json`).
