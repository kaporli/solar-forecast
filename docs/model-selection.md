# Model selection (#5)

The Belgian aggregate decides the selection and gets the final test. PVDAQ 2107 runs the same comparison as a
separate installation-level experiment; its results do not validate the Belgian model.

## Steps

1. **Compare** (`uv run solar-data compare belgium|pvdaq`). This runs the baselines, the three planned
   candidates and a small feature ablation through the frozen backtest (`docs/evaluation-protocol.md`) on the
   validation folds. It applies the frozen selection rule, measures resources and checks prediction intervals
   chronologically. Results: `docs/evaluation/<experiment>-models.md` and `data/evaluation/<experiment>-models.json`.
2. **Lock** (`uv run solar-data lock belgium`). This writes `config/lock-belgium.json` from what the comparison
   evaluated: the recipe recorded for the selected candidate, and the fingerprint of the implementation (SHA-256 of
   every package module) and dependencies (`uv.lock`, library versions). It refuses when any of these, the
   dataset or the protocol differs now, or when the comparison came from uncommitted code. The lock records:
   - the selected candidate, its inputs and settings, the target transform and output clipping;
   - the refit recipe and the final-test period;
   - the comparators reported with the test: persistence, physics, `physics_calibrated` (refitted with the same
     recipe) and Elia;
   - the interval decision;
   - the SHA-256 of the comparison results, dataset manifest, protocol and `uv.lock`.
3. **Review.** The lock is merged before any test score exists.
4. **Final test** (`uv run python -m solar_forecast.final_test belgium`, after the lock was merged). It runs once
   for the locked candidate with the refit recipe, together with persistence, physics, `physics_calibrated` and
   Elia. Before scoring it refuses if any module present at the lock's code commit, `uv.lock`, the Python or
   library versions, the dataset, the protocol or a recipe has changed, if the code is uncommitted, or if results
   already exist. A disappointing result is reported as it is; nothing is tuned, switched or rerun against the
   test. `--rehearse <dir>` runs the same code on the last validation fold, which reproduces that fold's recorded
   scores. `--recompute` regenerates the report and artifact manifest after a reporting change; it writes nothing
   unless every recorded metric and the model checksum come out identical. Results:
   `docs/evaluation/belgium-test.md` and `data/evaluation/belgium-test.json`. Slices are reported twice: the fitted
   models and baselines on every scored hour, and every model with Elia on the hours that have an Elia forecast.
5. **Artifact** (`artifacts/belgium/`, written by the same run from the same fit): `model.joblib`, `manifest.json`
   (input schema and feature version, recipe, seed, output clipping, training window, code commit, lock, dataset,
   protocol, `uv.lock` and library versions, checksums) and `fixtures.parquet` (feature rows of three test days,
   including the 25-hour day of 2026-10-25, with the predictions of the scored run). `solar_forecast.artifact`
   loads it, refusing a changed model checksum, changed prediction code (SHA-256 of `models.py` and `artifact.py`,
   which the pickled model runs) or other library versions. `predict` has no fallback: it raises on a missing,
   non-numeric or non-finite input, a capacity that is not positive, or a non-finite prediction, then clips;
   `tests/test_final_test.py` checks reload parity on the fixtures. The fixtures start from feature rows: parity
   of feature construction from live inputs belongs to #6.

## Candidates

All three predict load factor (actual / capacity known at issuance) and are fitted once per fold, on the fold's
training rows only:

- `physics_calibrated`: the physics baseline times one least-squares factor.
- `gbm`: gradient-boosted trees (scikit-learn `HistGradientBoostingRegressor`, absolute-error loss). Inputs:
  - forecast irradiance and its clear-sky index;
  - temperature, wind and cloud cover;
  - solar geometry;
  - day of year and local hour;
  - lead time;
  - persistence load factor, the only production lag. It is available at issuance by construction.
- `physics_residual_gbm`: calibrated physics plus trees on the remaining load factor. The calibration is refitted
  inside each fold, so residual targets never use a factor fitted on later data.

The tree settings are fixed in `solar_forecast/models.py` and were not tuned. Tuning on the validation folds
would be allowed, but its validation scores would then no longer be an unbiased estimate of performance; that
would need nested chronological validation. The untouched final test evaluates the locked selection. The
ablation drops one input group at a time from `gbm`. It is reported only; the selection is made among the three
planned candidates.

## Intervals

Four chronological residual-quantile methods are checked. Each fold is calibrated on residuals of earlier folds,
restricted to labels known at the fold's first issuance, the same cutoff as model fitting. These are not split
conformal intervals: they pool residuals of different fold models and use plain quantiles without a finite-sample
correction. A method is usable only if coverage stays within 5 points of nominal (80 % and 95 %) in every fold.

None qualifies, so none is frozen and the forecast is published as point values, stating that prediction
intervals have not been validated. This result rules out these methods, not conformal prediction in general.
The bootstrap intervals in the reports are a different thing: uncertainty of differences between candidates.

## Reproduce

```
uv sync --locked
uv run solar-data fetch belgium && uv run solar-data build belgium --inputs data/datasets/belgium-v1.json
uv run solar-data compare belgium
uv run solar-data compare pvdaq
```

The comparison is deterministic (seeded trees, fixed folds). Resource figures depend on the machine they were
measured on, which the report names; deployment-host measurements belong to #6.
