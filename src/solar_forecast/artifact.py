"""The packaged Belgian forecaster (#5) for deployment (#6): load with checksum and dependency checks, predict from
feature-contract rows with the locked output constraint. `artifacts/<experiment>/`:

- `model.joblib`: the fitted candidate (refit recipe of the lock);
- `manifest.json`: schema, recipe, seed, postprocessing, checksums, code revision, data and split references;
- `fixtures.parquet`: reference feature rows with the predictions the packaging run produced, for parity checks.
"""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from . import features as F
from . import sources

ROOT = sources.ROOT
# Modules whose code runs at prediction: the pickled model calls `models` (GBM.predict, design), this module clips.
IMPLEMENTATION = ["models.py", "artifact.py"]


def implementation() -> dict:
    here = Path(__file__).parent
    return {m: hashlib.sha256((here / m).read_bytes()).hexdigest() for m in IMPLEMENTATION}


def directory(name: str) -> Path:
    return ROOT / "artifacts" / name


def load(path: Path):
    """The fitted model and its manifest. Refuses a model whose checksum, prediction code (`IMPLEMENTATION`) or
    library versions differ from the manifest: predictions are only claimed identical on what was recorded."""
    man = json.loads((path / "manifest.json").read_text())
    blob = (path / "model.joblib").read_bytes()
    if hashlib.sha256(blob).hexdigest() != man["model_sha256"]:
        raise ValueError("model.joblib does not match the manifest checksum")
    code = {m: h for m, h in implementation().items() if man["implementation_sha256"].get(m) != h}
    if code:
        raise ValueError(f"prediction code differs from the artifact's: {sorted(code)}")
    diff = {k: (v, version(k)) for k, v in man["versions"].items() if version(k) != v}
    if diff:
        raise ValueError(f"library versions differ from the artifact's: {diff}")
    return joblib.load(path / "model.joblib"), man


def predict(model, man: dict, rows: pd.DataFrame) -> np.ndarray:
    """Energy per hour for feature-contract rows (`features.features_for_issuance`), clipped to [0, capacity x 1 h].
    No fallback: every required input must be numeric and finite and capacity positive, otherwise it raises, and
    so does a non-finite prediction. A caller that wants a fallback (persistence, no forecast) chooses it."""
    req = man["schema"]["required"]
    missing = [c for c in req if c not in rows]
    if missing:
        raise ValueError(f"rows lack {missing}")
    if man["schema"]["feature_version"] != F.FEATURE_VERSION:
        raise ValueError("feature version differs from the artifact's")
    x = rows[req]
    wrong = [c for c in req if not pd.api.types.is_numeric_dtype(x[c]) or pd.api.types.is_bool_dtype(x[c])]
    if wrong:
        raise ValueError(f"non-numeric inputs {wrong}")
    v = x.to_numpy(float, na_value=np.nan)
    bad = sorted({req[j] for j in np.nonzero(~np.isfinite(v))[1]})
    if bad:
        raise ValueError(f"missing or non-finite inputs {bad}")
    cap = x["capacity"].to_numpy(float)
    if (cap <= 0).any():
        raise ValueError("capacity must be positive")
    pred = np.asarray(model.predict(x), float)
    if not np.isfinite(pred).all():
        raise ValueError("non-finite prediction")
    return np.clip(pred, 0, cap)


def parity(path: Path) -> float:
    """Largest absolute difference between the fixtures' recorded predictions and a fresh reload."""
    model, man = load(path)
    fx = pd.read_parquet(path / "fixtures.parquet")
    return float(np.abs(predict(model, man, fx) - fx["expected_mwh"].to_numpy(float)).max())
