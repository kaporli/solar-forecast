"""Deployment and rollback check (#6, docs/deployment.md "Deploying and rolling back"):

    .venv/bin/python tools/deploy_check.py [--target YYYY-MM-DD --kind 11h|18h]

Run in the checkout that is about to serve, after `uv sync --locked --no-dev`. Writes nothing outside a temporary
directory except new rows in the derived Elia index. Checks, and exits non-zero if one fails:

1. the packaged model loads with these library versions and reproduces its fixtures exactly;
2. this checkout reissues an issuance already published by the worker (by default the latest one) from the
   snapshots retrieved by its issuance time, and gets the same features and forecast as the record;
3. scoring and the feed run on the live records.

Prints the timing and peak memory of each step as JSON."""
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from solar_forecast import artifact as A
from solar_forecast import issue, score, sources

try:
    from solar_forecast import feed
except ImportError:  # a rollback target older than the feed (#18)
    feed = None


def peak_mb() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10))


args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
records = sorted((issue.OUT / "belgium").glob("*_??h.json"), key=lambda p: json.loads(p.read_text())["issuance_utc"])
if "--target" in args:
    live = issue.record_path(issue.OUT, "belgium", args["--target"], args["--kind"])
else:
    live = next(p for p in reversed(records) if json.loads(p.read_text())["status"] == "issued")
rec = json.loads(live.read_text())
out, failed = {"code_commit": issue._git("rev-parse", "HEAD"), "reissued": live.name}, []

t = time.perf_counter()
out["parity_max_abs_mwh"] = A.parity(A.directory("belgium"))
out["artifact_s"] = round(time.perf_counter() - t, 2)
if out["parity_max_abs_mwh"] != 0:
    failed.append("artifact parity")

with tempfile.TemporaryDirectory() as tmp:
    t = time.perf_counter()
    now = pd.Timestamp(rec["issuance_utc"]) + pd.Timedelta(minutes=1)  # as if at the issuance: same inputs
    again = json.loads(issue.issue("belgium", rec["kind"], rec["target_date"], now, sources.STORE, Path(tmp)).read_text())
    out["reissue_s"] = round(time.perf_counter() - t, 2)
    out["features_match"] = again["features_sha256"] == rec["features_sha256"]
    out["forecast_match"] = [h["forecast_mwh"] for h in again["hours"]] == [h["forecast_mwh"] for h in rec["hours"]]
    out["model_match"] = again["model"]["model_sha256"] == rec["model"]["model_sha256"]
    for k in ("features_match", "forecast_match", "model_match"):
        if not out[k]:
            failed.append(k)

    t = time.perf_counter()
    score.score("belgium", out=Path(tmp) / "scores")
    out["score_s"] = round(time.perf_counter() - t, 2)
    if feed:
        t = time.perf_counter()
        f = feed.build("belgium")
        out["feed_s"] = round(time.perf_counter() - t, 2)
        out["feed_states"] = {k: e["state"] for k, e in f["issuances"].items()}

out["peak_rss_mb"] = peak_mb()
out["failed"] = failed
print(json.dumps(out, indent=1))
sys.exit(1 if failed else 0)
