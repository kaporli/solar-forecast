# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas>=2.2"]
# ///
"""Retrieve and inspect the selected sources.

    uv run tools/check_sources.py                 # Belgium (Elia + weather) and the PVDAQ experiment
    uv run tools/check_sources.py belgium         # only Belgium
    uv run tools/check_sources.py pvdaq --fixtures
    uv run tools/check_sources.py snapshot        # only save ods209 and ods210 snapshots

Downloads land in data/raw/ (ignored). Elia responses are saved as timestamped
snapshots with their request and SHA-256 (data/raw/elia/), never overwritten.
Every assertion is a finding recorded in docs/data-feasibility.md; a failing
assertion means the upstream data changed. Weather checks are sampled, not a
continuous coverage proof.
"""
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "data/manifest.json").read_text())
BELGIUM = json.loads((ROOT / "config/belgium.json").read_text())
RAW = ROOT / "data/raw"
ELIA = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets"
FREEZE_LAST_DAY = "2026-11-01"


def get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def elia(dataset: str, where: str) -> pd.DataFrame:
    """Export a dataset, save the raw bytes as a new snapshot with its request and checksum, parse."""
    q = urllib.parse.urlencode({"where": where, "order_by": "datetime", "delimiter": ","})
    url = f"{ELIA}/{dataset}/exports/csv?{q}"
    retrieved = datetime.now(timezone.utc)
    body = get(url)
    out = RAW / "elia"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{dataset}_{retrieved:%Y%m%dT%H%M%SZ}"
    (out / f"{stem}.csv").write_bytes(body)
    (out / f"{stem}.json").write_text(json.dumps({
        "dataset": dataset, "where": where, "url": url, "retrieved_at": retrieved.isoformat(),
        "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}, indent=1))
    df = pd.read_csv(io.BytesIO(body), encoding="utf-8-sig")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    if "measured_upscaled" in df:  # ods209 publishes it as text with a decimal comma
        df["measured"] = pd.to_numeric(df["measured_upscaled"].astype(str).str.replace(",", "."), errors="coerce")
    return df.set_index("datetime").sort_index()


def single_run(run: str, params: dict) -> dict | list:
    q = urllib.parse.urlencode({**params, "run": run}, safe=",")
    return json.loads(get(f"{params['endpoint']}?{q}" if "endpoint" in params else
                          f"https://single-runs-api.open-meteo.com/v1/forecast?{q}"))


def day_hours(day: pd.Timestamp, tz: str) -> pd.DatetimeIndex:
    """UTC starts of every hour of local day `day` (23, 24 or 25)."""
    start = pd.Timestamp(day.date()).tz_localize(tz)
    end = (pd.Timestamp(day.date()) + pd.Timedelta(days=1)).tz_localize(tz)
    return pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq="h", inclusive="left")


def check_run(loc: dict, hourly: str, day: pd.Timestamp, tz: str, run: str) -> None:
    """The run has every requested variable, non-null, at exactly the labels covering local day `day`."""
    h = pd.DataFrame(loc["hourly"])
    h["time"] = pd.to_datetime(h["time"], utc=True)
    assert h["time"].is_unique, (run, "duplicate labels")
    h = h.set_index("time")
    variables = hourly.split(",")
    assert set(variables) <= set(h.columns), (run, set(variables) - set(h.columns))
    labels = day_hours(day, tz) + pd.Timedelta(hours=1)  # radiation describes the hour ending at the label
    missing = labels.difference(h.index)
    assert missing.empty, (run, loc.get("latitude"), list(missing))
    assert h.loc[labels, variables].notna().all().all(), (run, loc.get("latitude"))


# Belgium: Elia actuals and benchmark forecasts

LIVE_FILTER = "region='Belgium' and gridconnectiontype='Total'"


def snapshot() -> tuple[pd.DataFrame, pd.DataFrame]:
    return elia("ods209", LIVE_FILTER), elia("ods210", LIVE_FILTER)


def observed_coverage(old: pd.DataFrame, new: pd.DataFrame) -> None:
    """Hours and local days whose four quarter-hours all have an observed `measured` value."""
    q = pd.concat([old["measured"], new["measured"]]).dropna()
    days = pd.date_range("2024-03-15", FREEZE_LAST_DAY, freq="D")
    hours = day_hours(days[0], "Europe/Brussels")[:0].append([day_hours(d, "Europe/Brussels") for d in days])
    n = q.groupby(q.index.floor("h")).size().reindex(hours, fill_value=0)
    complete = n == 4
    local_day = hours.tz_convert("Europe/Brussels").date
    by_day = complete.groupby(local_day).all()
    bad = sorted(str(d) for d in by_day[~by_day].index)
    print(f"Belgian target {days[0].date()} .. {FREEZE_LAST_DAY}: {len(days)} calendar days, "
          f"{int(by_day.sum())} with every hour observed; {int(complete.sum())} of {len(hours)} hours observed")
    print(f"  incomplete days {len(bad)}: {bad[0]} .. {bad[-2]} and {bad[-1]}")
    missing_hours = pd.Series(hours[~complete.values]).dt.tz_convert("Europe/Brussels").dt.hour.value_counts().to_dict()
    print(f"  incomplete hours by local hour {missing_hours}")
    assert (len(days), int(by_day.sum()), len(bad)) == (962, 746, 216), (len(days), int(by_day.sum()), len(bad))


def check_elia() -> None:
    old = elia("ods032", "region='Belgium'")
    new, live = snapshot()

    full = pd.date_range(old.index.min(), old.index.max(), freq="15min")
    assert str(old.index.min()) == "2020-09-30 22:00:00+00:00" and old.index.is_unique
    assert full.difference(old.index).empty, "ods032 has missing 15-minute slots"
    print(f"ods032 Belgium {old.index.min()} .. {old.index.max()}, {len(old)} rows, no missing slots")
    for c in ("measured", "dayahead11hforecast", "dayaheadforecast"):
        share = old[c].isna().groupby(old.index.year).mean().round(4).to_dict()
        print(f"  {c} null share by year {share}")

    gap = pd.date_range(old.index.max(), new.index.min(), freq="15min")[1:-1]
    print(f"seam: ods032 ends {old.index.max()}, ods209 starts {new.index.min()}, {len(gap)} slots between (night)")
    cap_old, cap_new = old["monitoredcapacity"].iloc[-1], new["monitoredcapacity"].dropna().iloc[0]
    assert abs(cap_old - cap_new) / cap_old < 0.01, (cap_old, cap_new)
    print(f"  monitored capacity {cap_old:.1f} MW (ods032 Belgium) vs {cap_new:.1f} MW (ods209 Belgium Total)")

    observed_coverage(old, new)

    by_day = new[["measured", "dayahead11hforecast", "dayaheadforecast"]].notna()
    by_day = by_day.groupby(new.index.tz_convert("Europe/Brussels").date).sum()
    print("ods209 non-null quarter-hours per local day:\n" + by_day.to_string())

    both = pd.concat([live["measured"].rename("live"), new["measured"].rename("hist")], axis=1, sort=True).dropna()
    print(f"ods210 vs ods209 measured: {len(both)} shared quarter-hours, max diff {(both['live'] - both['hist']).abs().max():.3f} MW")
    last = live["measured"].dropna().index.max()
    print(f"ods210 last measured interval start {last}, retrieved {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC")
    print(f"ods210 day-ahead 11h forecast reaches {live['dayahead11hforecast'].dropna().index.max()}")


def check_belgium_weather(step_days: int = 21) -> None:
    """Sampled every step_days: the 00 UTC run of D-1 (and 06 UTC from August 2024) covers local day D at all points."""
    params = MANIFEST["belgium"]["weather"]["params"]
    points = len(BELGIUM["weather_points"]["provinces"])
    days = pd.date_range("2024-03-15", pd.Timestamp.now().normalize() - pd.Timedelta(days=1), freq=f"{step_days}D")
    for day in days:
        runs = ["00:00"] + (["06:00"] if day >= pd.Timestamp("2024-08-16") else [])
        for hh in runs:
            run = (day - pd.Timedelta(days=1)).strftime("%Y-%m-%dT") + hh
            j = single_run(run, params)
            assert isinstance(j, list) and len(j) == points, (run, j)
            for loc in j:
                check_run(loc, params["hourly"], day, "Europe/Brussels", run)
            time.sleep(0.3)
    print(f"Belgian weather (sampled): {len(days)} target days every {step_days} days, {points} points each, "
          "every hour of local day D present and non-null")


# Installation experiment: PVDAQ 2107

def fetch_pvdaq() -> dict[str, Path]:
    prod = MANIFEST["installation_experiment"]["production"]
    RAW.mkdir(parents=True, exist_ok=True)
    paths = {}
    for f in prod["files"]:
        p = RAW / Path(f["path"]).name
        if not p.exists():
            p.write_bytes(get(f"{prod['base_url']}/{f['path']}"))
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest == f["sha256"], f"{p.name}: checksum changed upstream ({digest})"
        paths[p.name] = p
    return paths


def audit_pvdaq(paths: dict[str, Path]) -> pd.DataFrame:
    meta = json.loads(paths["2107_system_metadata.json"].read_text())
    col = MANIFEST["installation_experiment"]["production"]["column"]
    metric = meta["Metrics"][col]
    assert (metric["units"], metric["aggregation_type"]) == ("kw", "avg")
    assert sum(m["quantity"] for m in meta["Modules"].values()) == 2880

    raw = pd.concat(pd.read_csv(paths[n]) for n in ("2107_meter_15m_data_2024.csv", "2107_meter_15m_data_2025.csv"))
    raw["t"] = pd.to_datetime(raw["measured_on"])
    dups = raw[raw["t"].duplicated(keep=False)]
    assert dups.groupby("t")[col].nunique().max() == 1, "duplicate timestamps with different values"
    d = raw.drop_duplicates("t").set_index("t")[col]
    print(f"PVDAQ meter rows {len(raw)}, duplicate rows dropped {len(raw) - len(d)} "
          f"({dups['t'].min()} .. {dups['t'].max()}), range {d.index.min()} .. {d.index.max()}")
    assert d.isna().sum() == 0 and d.min() >= 0

    missing = pd.date_range(d.index.min(), d.index.max(), freq="15min").difference(d.index)
    assert {str(t.date()) for t in missing} == {"2024-03-10", "2025-03-09"}  # spring-forward gaps only
    per_day = d.groupby(d.index.date).size()
    for day, n in [("2024-11-03", 96), ("2025-11-02", 96), ("2025-03-09", 92)]:
        assert per_day[pd.Timestamp(day).date()] == n, day
    daily = (d * 0.25).resample("D").sum()
    print("  peak kW", d.max(), "at", d.idxmax(), "| all-zero days", [str(x.date()) for x in daily[daily == 0].index])
    print("  energy MWh: 2024 %.1f, 2025 %.1f" % (daily["2024"].sum() / 1e3, daily["2025"].sum() / 1e3))

    # Target rule: ambiguous fall-back labels are dropped, not assigned; an hour needs four end labels.
    utc = d.tz_localize("America/Los_Angeles", ambiguous="NaT", nonexistent="NaT")
    ambiguous = utc.index.isna()
    utc = utc[~ambiguous]
    utc.index = utc.index.tz_convert("UTC")
    n = utc.groupby((utc.index - pd.Timedelta(minutes=15)).floor("h")).size()
    days = pd.date_range("2024-03-15", "2025-12-31", freq="D")
    hours = day_hours(days[0], "America/Los_Angeles")[:0].append([day_hours(x, "America/Los_Angeles") for x in days])
    complete = n.reindex(hours, fill_value=0) == 4
    by_day = complete.groupby(hours.tz_convert("America/Los_Angeles").date).all()
    bad = sorted(str(x) for x in by_day[~by_day].index)
    print(f"  ambiguous rows dropped {int(ambiguous.sum())}; target {len(days)} calendar days, "
          f"{int(by_day.sum())} with every hour observed; incomplete {bad}")
    assert bad == ["2024-11-03", "2025-11-02", "2025-12-31"], bad  # fall-back hours; file ends 23:45
    return raw


def check_pvdaq_weather(step_days: int = 14) -> None:
    params = MANIFEST["installation_experiment"]["weather"]["params"]
    issues = pd.date_range("2024-03-14", "2025-12-30", freq=f"{step_days}D")
    for day in issues:
        j = single_run(day.strftime("%Y-%m-%dT00:00"), params)
        assert not j.get("error"), (day, j.get("reason"))
        assert len(j["hourly"]["time"]) == 168, day
        check_run(j, params["hourly"], day + pd.Timedelta(days=1), "America/Los_Angeles", str(day.date()))
        time.sleep(0.3)
    print(f"PVDAQ weather (sampled): {len(issues)} 00 UTC runs, every hour of the next local day present and non-null")


def write_fixtures(raw: pd.DataFrame) -> None:
    out = ROOT / "fixtures"
    out.mkdir(exist_ok=True)
    keep = raw["t"].dt.strftime("%Y-%m-%d").isin(
        ["2024-11-02", "2024-11-03", "2024-11-04", "2025-03-08", "2025-03-09", "2025-03-10"])
    raw.loc[keep].drop(columns="t").to_csv(out / "pvdaq_2107_meter_dst.csv", index=False)
    params = MANIFEST["installation_experiment"]["weather"]["params"]
    (out / "ecmwf_ifs_2025-06-15T00.json").write_text(json.dumps(single_run("2025-06-15T00:00", params), indent=1))
    for p in sorted(out.iterdir()):
        print("fixture", p.name, hashlib.sha256(p.read_bytes()).hexdigest())


def self_test() -> None:
    """check_run accepts the fixture run and rejects it with one hour removed (offline)."""
    j = json.loads((ROOT / "fixtures/ecmwf_ifs_2025-06-15T00.json").read_text())
    hourly = MANIFEST["installation_experiment"]["weather"]["params"]["hourly"]
    day = pd.Timestamp("2025-06-16")
    check_run(j, hourly, day, "America/Los_Angeles", "fixture")
    i = j["hourly"]["time"].index("2025-06-16T20:00")
    cut = {**j, "hourly": {k: v[:i] + v[i + 1:] for k, v in j["hourly"].items()}}
    try:
        check_run(cut, hourly, day, "America/Los_Angeles", "fixture minus one hour")
    except AssertionError:
        return
    raise AssertionError("check_run accepted a run with a missing hour")


if __name__ == "__main__":
    self_test()
    args = sys.argv[1:]
    if args == ["snapshot"]:
        snapshot()
        print("saved to", RAW / "elia")
        sys.exit()
    if not {"belgium", "pvdaq"} & set(args):
        args += ["belgium", "pvdaq"]
    if "belgium" in args:
        check_elia()
        check_belgium_weather()
    if "pvdaq" in args:
        raw = audit_pvdaq(fetch_pvdaq())
        check_pvdaq_weather()
        if "--fixtures" in args:
            write_fixtures(raw)
