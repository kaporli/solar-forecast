"""Deployment and rollback check (#6, docs/deployment.md "Deploying and rolling back").

    <checkout>/.venv/bin/python deploy_check.py --data <live data dir> [--target YYYY-MM-DD --kind 11h|18h]

Runs the code of the checkout whose interpreter runs it (a staged release before activation, or the active one)
against the live data. It writes to a temporary directory only, apart from new rows in the derived Elia index.
`tools/solar-deploy` keeps this file outside the checkouts it switches, at the version of the main branch. Exits
non-zero if:

1. the packaged model does not reproduce its fixtures exactly;
2. replaying an issuance already published (by default the latest) from exactly the snapshots its record lists,
   each verified against the record's payload and sidecar checksums, gives a record whose hours (target hours,
   forecast, persistence, physics), features hash, model identity or inputs differ;
3. scoring, the feed (contract `kaporli.solar-forecast.feed` version 1) or the history
   (`kaporli.solar-forecast.history` version 1) cannot run on the live records.

A release that changes the model, features or baselines cannot pass 2 by design; it is deployed only after review.
Prints the result as JSON, with timing and peak memory."""
import argparse
import importlib
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from solar_forecast import artifact as A
from solar_forecast import issue, score, sources

FEED = ("kaporli.solar-forecast.feed", 1)
HISTORY = ("kaporli.solar-forecast.history", 1)


def peak_mb() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10))


def pinned_store(rec: dict, store: Path, tmp: Path) -> Path:
    """A store holding exactly the snapshots the record lists, each checked against the record."""
    pinned = tmp / "raw/snapshots"
    for ref in rec["inputs"]["snapshots"]:
        sidecar = store / ref["source"] / ref["sidecar"]
        if sources.sha256(sidecar.read_bytes()) != ref["sidecar_sha256"]:
            raise ValueError(f"{ref['sidecar']}: sidecar differs from the record")
        meta = sources.snapshot(sidecar, store)
        if meta["sha256"] != ref["sha256"]:
            raise ValueError(f"{ref['sidecar']}: payload checksum differs from the record")
        sources.read_verified(meta)
        dst = pinned / ref["source"] / ref["sidecar"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sidecar, dst)
        link = pinned.parent / meta["object"] if "object" in meta else dst.parent / meta["file"]
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(Path(meta["path"]).resolve())
    return pinned


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=sources.ROOT / "data")
    ap.add_argument("--target")
    ap.add_argument("--kind", choices=["11h", "18h"])
    a = ap.parse_args(argv)
    store, forecasts = a.data / "raw/snapshots", a.data / "forecasts"
    records = sorted((forecasts / "belgium").glob("*_??h.json"), key=lambda p: json.loads(p.read_text())["issuance_utc"])
    live = (issue.record_path(forecasts, "belgium", a.target, a.kind) if a.target
            else next(p for p in reversed(records) if json.loads(p.read_text())["status"] == "issued"))
    rec = json.loads(live.read_text())
    out, failed = {"code": str(Path(issue.__file__).parents[2]), "code_commit": issue._git("rev-parse", "HEAD"),
                   "replayed": live.name}, []

    def step(name, fn):
        t = time.perf_counter()
        try:
            return fn()
        except Exception as e:
            failed.append(f"{name}: {type(e).__name__}: {e}")
        finally:
            out[f"{name}_s"] = round(time.perf_counter() - t, 2)

    out["parity_max_abs_mwh"] = step("artifact", lambda: A.parity(A.directory("belgium")))
    if out["parity_max_abs_mwh"] != 0:
        failed.append("artifact parity")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def replay():
            pinned = pinned_store(rec, store, tmp)
            now = pd.Timestamp(rec["issuance_utc"]) + pd.Timedelta(minutes=1)
            return json.loads(issue.issue("belgium", rec["kind"], rec["target_date"], now, pinned, tmp / "out").read_text())

        again = step("replay", replay)
        if again:
            for k in ("hours", "features_sha256", "model", "inputs"):
                out[f"{k}_match"] = again[k] == rec[k]
                if not out[f"{k}_match"]:
                    failed.append(f"{k} differ")
        step("score", lambda: score.score("belgium", None, store, forecasts, tmp / "scores"))

        def feed():
            FD = importlib.import_module("solar_forecast.feed")  # required: the timers' commands publish it
            f = FD.build("belgium", None, forecasts)
            if (f["schema"], f["version"]) != FEED:
                raise ValueError(f"feed contract {f['schema']} {f['version']}, expected {FEED}")
            return {k: e["state"] for k, e in f["issuances"].items()}

        out["feed_states"] = step("feed", feed)

        def history():
            HI = importlib.import_module("solar_forecast.history")  # required: written with the feed
            h = HI.build("belgium", None, forecasts, tmp / "scores")
            if (h["schema"], h["version"]) != HISTORY:
                raise ValueError(f"history contract {h['schema']} {h['version']}, expected {HISTORY}")
            return len(h["issuances"])

        out["history_issuances"] = step("history", history)

    out["peak_rss_mb"] = peak_mb()
    out["failed"] = failed
    print(json.dumps(out, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
