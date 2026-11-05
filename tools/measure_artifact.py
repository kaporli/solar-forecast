"""Resource and parity measurements of the packaged model on the machine this runs on (#6):

    uv run python tools/measure_artifact.py [artifacts/belgium]

Prints JSON: hardware, imports in this process, a fresh process launched and timed from outside (interpreter
startup plus imports), artifact load, prediction time for one issuance
(median of 50) and for all fixtures, peak RSS, and the largest difference from the fixtures' recorded predictions.
Feature construction is not included; it is measured with the daily issuance."""
import json
import platform
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

t0 = time.perf_counter()
import pandas as pd  # noqa: E402

from solar_forecast import artifact as A  # noqa: E402

imports_s = time.perf_counter() - t0
path = Path(sys.argv[1]) if len(sys.argv) > 1 else A.directory("belgium")

t = time.perf_counter()
model, man = A.load(path)
load_s = time.perf_counter() - t
fx = pd.read_parquet(path / "fixtures.parquet")
one = fx[(fx["kind"] == "11h") & (fx["issuance_utc"] == fx.loc[fx["kind"] == "11h", "issuance_utc"].max())]
times = []
for _ in range(50):
    t = time.perf_counter()
    A.predict(model, man, one)
    times.append(time.perf_counter() - t)
t = time.perf_counter()
A.predict(model, man, fx)
all_s = time.perf_counter() - t
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
t = time.perf_counter()  # timed outside the child, so interpreter startup is included
subprocess.run([sys.executable, "-c", "import solar_forecast.artifact"], check=True)
launch_s = time.perf_counter() - t
cpu = next((ln.split(":", 1)[1].strip() for ln in Path("/proc/cpuinfo").read_text().splitlines()
            if ln.startswith("model name")), platform.processor()) if Path("/proc/cpuinfo").exists() else platform.processor()
print(json.dumps({
    "host": {"cpu": cpu, "python": platform.python_version(), "system": f"{platform.system()} {platform.release()}"},
    "artifact": {"model_sha256": man["model_sha256"][:12], "model_mb": round(man["model_bytes"] / 2**20, 2)},
    "imports_in_this_process_s": round(imports_s, 3), "process_launch_and_imports_s": round(launch_s, 3),
    "load_s": round(load_s, 3),
    "predict_one_issuance_s": round(statistics.median(times), 4), "issuance_rows": len(one),
    "predict_all_fixtures_s": round(all_s, 4), "fixture_rows": len(fx),
    "peak_rss_mb": round(rss), "parity_max_abs_mwh": A.parity(path),
}, indent=1))
