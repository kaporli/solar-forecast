# Legacy student project (2024-05-31)

The original Solar coursework, kept as it was. Nothing in `src/` imports it.

Snapshot: commit `a253d18`, tag `legacy-2024-05-31`.

| File | What it is |
| --- | --- |
| `Solar_Elias.ipynb` | notebook: data preparation, model comparison, grid search, saving the model |
| `Solar_Script_Elias.py` | script that loads `model.joblib` and predicts from `datasets/forecast(in).csv` |
| `sun.csv` | sunrise, noon and sunset table for 2023 to 2024, replaced by computed solar geometry in `src/` |
| `datasets/` | the two sample weather forecasts the script reads |

## Model artifact

`model.joblib` (85,260,663 bytes, SHA-256 `6518f2b712c30af63389feb3ea045767a9e600e79e47734dac4c259e68df825f`)
was removed from the working tree. History is not rewritten; retrieve it with:

```
git show legacy-2024-05-31:model.joblib > legacy/model.joblib
shasum -a 256 legacy/model.joblib
```

It is a `GridSearchCV` best estimator: `StandardScaler` and `RandomForestRegressor(random_state=42)`,
`n_estimators=300`, `max_depth=15`.

## Recorded results

Mean absolute error as printed by the notebook, on a random train/test split of the original rooftop export
(the export itself is lost):

| Model | MAE |
| --- | --- |
| Linear regression | 0.2083 |
| Random forest | 0.0663 |
| Decision tree | 0.0813 |
| Support vector regression | 0.0917 |
| Random forest after grid search | 0.0657 |

A random split mixes hours of the same days into training and test, so these numbers are not comparable with
the chronological evaluation in `docs/forecast-protocol.md`.
