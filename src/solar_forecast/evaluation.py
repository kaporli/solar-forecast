"""Chronological evaluation for #4 and #5: frozen folds, one backtest for every candidate, metrics and paired
block bootstrap. The protocol is config/evaluation.json; the final test period is locked (see `backtest`)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import features as F
from . import sources
from .weather import solar

CONFIG = json.loads((sources.ROOT / "config/evaluation.json").read_text())
HINDCAST_END = pd.Timestamp("2024-11-12", tz="UTC")  # runs initialised earlier are 49r1 hindcasts


@dataclass(frozen=True)
class Fold:
    name: str
    start: dt.date
    end: dt.date
    test: bool = False


def _d(s) -> dt.date:
    return pd.Timestamp(s).date()


def folds(exp_name: str, cfg: dict = CONFIG, include_test: bool = False) -> list[Fold]:
    e = cfg["experiments"][exp_name]
    out = [Fold(f["name"], _d(f["start"]), _d(f["end"])) for f in e["validation_folds"]]
    if include_test:
        out.append(Fold("test", _d(e["test"]["start"]), _d(e["test"]["end"]), test=True))
    check_folds(out, cfg)
    return out


def check_folds(fs: list[Fold], cfg: dict = CONFIG) -> None:
    """Folds are ordered, disjoint, start after the first trainable day, and the test period comes last."""
    lag = cfg["training"]["label_lag_days"]
    first = _d(cfg["training"]["first_target"])
    for a, b in zip(fs, fs[1:]):
        if not (a.start <= a.end < b.start):
            raise ValueError(f"folds {a.name} and {b.name} overlap or are out of order")
    for f in fs:
        if f.start <= HINDCAST_END.date():  # target D uses a run of D-1: D = 2024-11-12 would use a hindcast
            raise ValueError(f"fold {f.name} starts on hindcast runs")
        if f.start - dt.timedelta(days=lag) < first:
            raise ValueError(f"fold {f.name} has no training days")
    if any(f.test for f in fs[:-1]):
        raise ValueError("the test period must be the last fold")


def train_end(fold: Fold, cfg: dict = CONFIG) -> dt.date:
    """Last target day whose labels are complete before the fold's first issuance (D-1 at 11:00 or 09:00): D-2."""
    return fold.start - dt.timedelta(days=cfg["training"]["label_lag_days"])


def daylight(hours: pd.DatetimeIndex, exp: F.Experiment) -> np.ndarray:
    lat = float(np.mean([p[0] for p in exp.points]))
    lon = float(np.mean([p[1] for p in exp.points]))
    return ~solar(pd.DatetimeIndex(hours), [lat], [lon])["night"].to_numpy()


def scored_examples(exp: F.Experiment, feats: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """Every feature row with its target (`actual`), `scored`, `daylight` and `complete_day`."""
    ex = F.examples(feats, targets[["hour_utc", "local_date", "local_hour", exp.target_col, "eligible",
                                    "day_complete"] + [c for c in ("elia_da11_mwh", "elia_da18_mwh") if c in targets]])
    ex = ex.rename(columns={exp.target_col: "actual"})
    ex["eligible"] = ex["eligible"].fillna(False).astype(bool)
    ex["day_complete"] = ex["day_complete"].fillna(False).astype(bool)
    ex["scored"] = ex["eligible"] & ex["weather_complete"] & ex["capacity_complete"] & ex["persistence_lf"].notna()
    hours = pd.DatetimeIndex(ex["target_hour_utc"].unique())
    ex["daylight"] = ex["target_hour_utc"].map(pd.Series(daylight(hours, exp), index=hours))
    day = ex.groupby(["kind", "target_date"])["scored"].transform("all") & ex["day_complete"]
    ex["complete_day"] = day
    return ex


def training_rows(ex: pd.DataFrame, fold: Fold, cfg: dict = CONFIG) -> pd.DataFrame:
    """Examples a candidate may fit on for this fold: labels known before its first issuance, target eligible,
    inputs complete. Validation folds before this one are included (expanding window)."""
    keep = (ex["target_date"] <= train_end(fold, cfg)) & ex["eligible"] & ex["weather_complete"] & ex["capacity_complete"]
    return ex[keep]


def backtest(candidate, ex: pd.DataFrame, fs: list[Fold], cfg: dict = CONFIG, unlock_test: bool = False) -> pd.DataFrame:
    """Fit once per fold on `training_rows`, predict every row of the fold from feature columns only, clip to
    [0, capacity x 1 h]. Raises on the test fold unless `unlock_test` (reserved for the locked candidate in #5)."""
    out = []
    for fold in fs:
        if fold.test and not unlock_test:
            raise PermissionError("final test period is locked until the deployment candidate is selected (#5)")
        train = training_rows(ex, fold, cfg)
        if len(train) and train["target_date"].max() > train_end(fold, cfg):
            raise AssertionError("training labels after the fold's first issuance")
        candidate.fit(train)
        rows = ex[(ex["target_date"] >= fold.start) & (ex["target_date"] <= fold.end)]
        pred = np.asarray(candidate.predict(rows[list(F.CONTRACT)]), float)
        pred = np.clip(pred, 0, rows["capacity"].to_numpy(float))
        out.append(rows.assign(fold=fold.name, candidate=candidate.name, prediction=pred))
    return pd.concat(out, ignore_index=True)


def metrics(r: pd.DataFrame, pred: str = "prediction") -> dict:
    """Scored rows only. Hourly metrics over all and daylight hours; daily totals over complete days."""
    r = r[r["scored"] & r[pred].notna()]
    e = r[pred] - r["actual"]
    d = r["daylight"].to_numpy(bool)
    days = r[r["complete_day"]].groupby(["kind", "target_date"])[[pred, "actual"]].sum()
    de = days[pred] - days["actual"]
    nan = float("nan")
    return {
        "hours": int(len(r)), "daylight_hours": int(d.sum()), "complete_days": int(len(days)),
        "mae_daylight": float(e[d].abs().mean()) if d.any() else nan,
        "mae_all": float(e.abs().mean()) if len(e) else nan,
        "rmse_daylight": float(np.sqrt((e[d] ** 2).mean())) if d.any() else nan,
        "rmse_all": float(np.sqrt((e ** 2).mean())) if len(e) else nan,
        "bias_daylight": float(e[d].mean()) if d.any() else nan,
        "daily_mae": float(de.abs().mean()) if len(de) else nan,
        "daily_bias": float(de.mean()) if len(de) else nan,
    }


def block_bootstrap(a: pd.DataFrame, b: pd.DataFrame, cfg: dict = CONFIG) -> dict:
    """Paired moving-block bootstrap over target days of daylight MAE(a) - MAE(b) on hours scored in both.
    `a` and `b`: backtest rows of two candidates. Circular blocks of consecutive days in calendar order."""
    rs = cfg["resampling"]
    key = ["kind", "target_hour_utc"]
    m = a[a["scored"] & a["daylight"]].merge(b[key + ["prediction"]], on=key, suffixes=("_a", "_b")).dropna(
        subset=["prediction_a", "prediction_b"])
    m["ea"] = (m["prediction_a"] - m["actual"]).abs()
    m["eb"] = (m["prediction_b"] - m["actual"]).abs()
    per = m.groupby("target_date").agg(ea=("ea", "sum"), eb=("eb", "sum"), n=("ea", "size")).sort_index()
    ea, eb, n = per["ea"].to_numpy(), per["eb"].to_numpy(), per["n"].to_numpy()
    k, block = len(per), rs["block_days"]
    rng = np.random.default_rng(rs["seed"])
    stats = np.empty(rs["resamples"])
    for i in range(rs["resamples"]):
        starts = rng.integers(0, k, size=-(-k // block))
        idx = ((starts[:, None] + np.arange(block)) % k).ravel()[:k]
        stats[i] = (ea[idx].sum() - eb[idx].sum()) / n[idx].sum()
    alpha = (1 - rs["interval"]) / 2
    daily = (ea - eb) / n
    acf = [float(pd.Series(daily).autocorr(lag)) for lag in range(1, block + 1)] if k > block + 1 else []
    return {"days": int(k), "hours": int(n.sum()), "difference": float((ea.sum() - eb.sum()) / n.sum()),
            "low": float(np.quantile(stats, alpha)), "high": float(np.quantile(stats, 1 - alpha)),
            "daily_difference_autocorrelation": acf}


def split_manifest(ex: pd.DataFrame, fs: list[Fold], cfg: dict = CONFIG) -> list[dict]:
    out = []
    for f in fs:
        r = ex[(ex["target_date"] >= f.start) & (ex["target_date"] <= f.end)]
        s = r[r["scored"]].sort_values(["kind", "target_hour_utc"])
        keys = "\n".join(f"{k} {t.isoformat()}" for k, t in zip(s["kind"], s["target_hour_utc"]))
        out.append({"fold": f.name, "start": str(f.start), "end": str(f.end), "train_last_target": str(train_end(f, cfg)),
                    "rows": int(len(r)), "scored_hours": int(len(s)),
                    "rows_on_hindcast_runs": int((r["weather_run_utc"] < HINDCAST_END).sum()),
                    "scored_keys_sha256": hashlib.sha256(keys.encode()).hexdigest()})
    return out


def season(d: dt.date) -> str:
    return {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
            6: "JJA", 7: "JJA", 8: "JJA"}.get(d.month, "SON")


def sky(r: pd.DataFrame) -> pd.Series:
    """Per row: the target day's daylight mean forecast cloud cover class (forecast information, not actuals)."""
    cc = r[r["daylight"]].groupby(["kind", "target_date"])["cloud_cover"].mean()
    cls = pd.cut(cc, [-0.1, 30, 70, 100.1], labels=["clear", "mixed", "overcast"]).astype("string")
    return pd.Series([cls.get((k, d)) for k, d in zip(r["kind"], r["target_date"])], index=r.index, dtype="string")
