"""Sample feeds for the portfolio (#7), one per state, from issued records:

    uv run python tools/feed_fixtures.py <forecasts dir> [fixtures/feed]

`<forecasts dir>` holds <experiment>/<target>_<kind>.json records, for example a copy of the worker's
data/forecasts. Uses the records of 2026-11-08 and 2026-11-09. `fresh` is the records as issued; the others change a
copy: `fallback` relabels the 18h weather run, `stale` replaces the 2026-11-09 18h record by a failure record,
`unavailable` also moves past the end of 2026-11-08. The forecast values are real; the states other than fresh are
constructed."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from solar_forecast import feed

src = Path(sys.argv[1])
out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("fixtures/feed")
out.mkdir(parents=True, exist_ok=True)
NOW = "2026-11-08T17:45:00+00:00"  # 18:45 in Brussels: both 2026-11-09 issuances are out


def case(name, now=NOW, change=lambda d: None):
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "belgium"
        shutil.copytree(src / "belgium", d)
        change(d)
        f = feed.build("belgium", now, Path(t))
    (out / f"belgium-{name}.json").write_text(json.dumps(f, indent=1) + "\n")
    print(name, {k: v["state"] for k, v in f["issuances"].items()})


def fallback(d):
    p = d / "2026-11-09_18h.json"
    r = json.loads(p.read_text())
    r["inputs"] |= {"weather_run_utc": "2026-11-08T00:00:00+00:00", "weather_availability": "fallback"}
    p.write_text(json.dumps(r))


def failed(d):
    r = json.loads((d / "2026-11-09_18h.json").read_text())
    for p in d.glob("2026-11-09_18h*.json"):
        p.unlink()
    keep = ("experiment", "kind", "target_date", "issuance_utc", "deadline_utc", "attempt", "started_at")
    (d / f"2026-11-09_18h.failed-{r['attempt']}.json").write_text(json.dumps(
        {**{k: r[k] for k in keep}, "status": "failed", "error": "ValueError: non-finite inputs"}))


case("fresh")
case("fallback", change=fallback)
case("stale", change=failed)
case("unavailable", "2026-11-08T23:30:00+00:00", failed)
