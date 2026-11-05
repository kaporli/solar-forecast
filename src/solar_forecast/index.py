"""SQLite index over the Elia snapshots (storage decision in #6), so builds and inference query observation versions
instead of parsing every snapshot. It lives next to the archive (`data/raw/index.sqlite`) and is derived from it:
deleting it and calling `ingest` again gives the same answers.

- `retrievals`: one row per snapshot sidecar (identity, retrieval time, verified checksums, parse statistics).
- `elia_rows`: each distinct quarter-hour version (dataset, region, start, values), stored once.
- `seen`: which retrieval delivered which version, so an unchanged observation keeps its provenance.

`resolved` reproduces the archive rule of `features.BelgiumInputs`: per quarter-hour and region, field by field, the
first non-null value ordered by dataset priority (archive datasets before ods210), then newest retrieval. Only the
given retrievals count, so a pinned build sees exactly its manifest's inputs and an as-of query only what was
retrieved by then.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from . import sources

FIELDS = ["measured_mw", "capacity_mw", "elia_da11_mw", "elia_da18_mw"]
PRIORITY = {"ods032": 0, "ods209": 0, "ods210": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS retrievals (
    id INTEGER PRIMARY KEY, sidecar TEXT UNIQUE NOT NULL, sidecar_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL,
    name TEXT NOT NULL, dataset TEXT NOT NULL, retrieved_ns INTEGER NOT NULL, rows INTEGER NOT NULL,
    duplicates_dropped INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS elia_rows (
    id INTEGER PRIMARY KEY, key BLOB UNIQUE NOT NULL, dataset TEXT NOT NULL, region TEXT NOT NULL,
    start_ns INTEGER NOT NULL, measured_mw REAL, capacity_mw REAL, elia_da11_mw REAL, elia_da18_mw REAL);
CREATE INDEX IF NOT EXISTS elia_rows_start ON elia_rows (start_ns);
CREATE TABLE IF NOT EXISTS seen (
    retrieval_id INTEGER NOT NULL REFERENCES retrievals, row_id INTEGER NOT NULL REFERENCES elia_rows,
    PRIMARY KEY (retrieval_id, row_id)) WITHOUT ROWID;
"""


def path_for(metas: list[dict]) -> Path:
    """The index beside the archive the snapshots come from: <raw>/index.sqlite for <raw>/snapshots/<source>/."""
    return Path(metas[0]["sidecar"]).parents[2] / "index.sqlite"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, isolation_level=None, timeout=60)  # autocommit; ingest uses explicit transactions
    con.executescript(SCHEMA)
    return con


def _ns(ts) -> np.ndarray:
    return pd.DatetimeIndex(ts).as_unit("ns").asi8


def ingest(con: sqlite3.Connection, metas: list[dict]) -> list[int]:
    """Index every snapshot not yet indexed (payload checksum verified) and return the retrieval ids of `metas`.
    Refuses a sidecar whose bytes differ from when it was indexed."""
    ids = []
    for m in metas:
        key = Path(m["sidecar"]).name
        got = con.execute("SELECT id, sidecar_sha256 FROM retrievals WHERE sidecar = ?", (key,)).fetchone()
        if got:
            if got[1] != m["sidecar_sha256"]:
                raise ValueError(f"{key}: sidecar differs from the indexed one")
            ids.append(got[0])
            continue
        f = sources.parse_elia(sources.read_verified(m), m["dataset"])
        f = f.reindex(columns=["datetime", "region"] + FIELDS)
        con.execute("BEGIN IMMEDIATE")
        try:
            rid = con.execute(
                "INSERT INTO retrievals (sidecar, sidecar_sha256, sha256, name, dataset, retrieved_ns, rows, duplicates_dropped)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, m["sidecar_sha256"], m["sha256"], m["name"], m["dataset"],
                 int(_ns([pd.Timestamp(m["retrieved_at"])])[0]), len(f), f.attrs["duplicates_dropped"])).lastrowid
            con.execute("CREATE TEMP TABLE IF NOT EXISTS batch (key BLOB, dataset TEXT, region TEXT, start_ns INTEGER,"
                        " measured_mw REAL, capacity_mw REAL, elia_da11_mw REAL, elia_da18_mw REAL)")
            con.execute("DELETE FROM batch")
            for i in range(0, len(f), 100_000):  # bounded memory for the large archive responses
                c = f.iloc[i:i + 100_000]
                rows = []
                for r, s, v in zip(c["region"].astype(str), _ns(c["datetime"]), c[FIELDS].to_numpy(float)):
                    v = [None if np.isnan(x) else float(x) for x in v]
                    k = hashlib.blake2b(repr((m["dataset"], r, int(s), v)).encode(), digest_size=16).digest()
                    rows.append((k, m["dataset"], r, int(s), *v))
                con.executemany("INSERT INTO batch VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            con.execute("INSERT OR IGNORE INTO elia_rows (key, dataset, region, start_ns, measured_mw, capacity_mw,"
                        " elia_da11_mw, elia_da18_mw) SELECT * FROM batch")
            con.execute("INSERT INTO seen SELECT ?, e.id FROM batch b JOIN elia_rows e ON e.key = b.key", (rid,))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        ids.append(rid)
    return ids


def _allowed(con: sqlite3.Connection, ids: list[int]) -> None:
    con.execute("CREATE TEMP TABLE IF NOT EXISTS allowed (id INTEGER PRIMARY KEY)")
    con.execute("DELETE FROM allowed")
    con.executemany("INSERT INTO allowed VALUES (?)", [(i,) for i in ids])


def resolved(con: sqlite3.Connection, ids: list[int], as_of=None, since=None) -> pd.DataFrame:
    """Quarter-hours as `features.BelgiumInputs.resolved`: datetime, region, the four fields and the retrieval time
    of the winning row. `as_of`: only retrievals at or before it. `since`: only quarter-hours starting from it."""
    _allowed(con, ids)
    q = ("SELECT e.dataset, e.region, e.start_ns, e.measured_mw, e.capacity_mw, e.elia_da11_mw, e.elia_da18_mw,"
         " MAX(r.retrieved_ns) AS retrieved_ns FROM seen s JOIN allowed a ON a.id = s.retrieval_id"
         " JOIN retrievals r ON r.id = s.retrieval_id JOIN elia_rows e ON e.id = s.row_id"
         " WHERE (? IS NULL OR r.retrieved_ns <= ?) AND (? IS NULL OR e.start_ns >= ?) GROUP BY e.id")
    a = None if as_of is None else int(_ns([pd.Timestamp(as_of)])[0])
    s = None if since is None else int(_ns([pd.Timestamp(since)])[0])
    cur, cols, parts = con.execute(q, (a, a, s, s)), ["dataset", "region", "start_ns", *FIELDS, "retrieved_ns"], []
    while chunk := cur.fetchmany(100_000):  # compact frames, not a million Python tuples at once
        parts.append(pd.DataFrame(chunk, columns=cols).astype({"dataset": "category", "region": "category"}))
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    # one entry per version, at its latest permitted retrieval: an older sighting of the same version comes later
    # in the order below and cannot fill a field the newer one leaves empty, so the result is unchanged
    d = d.assign(datetime=pd.to_datetime(d["start_ns"], unit="ns", utc=True).dt.as_unit("us"),  # as parse_elia and _ts
                 retrieved_at=pd.to_datetime(d["retrieved_ns"], unit="ns", utc=True).dt.as_unit("us"),
                 priority=d["dataset"].astype(str).map(PRIORITY))
    d[FIELDS] = d[FIELDS].astype(float)
    d["region"] = d["region"].astype("str")
    d = d[["datetime", "region", *FIELDS, "retrieved_at", "priority"]]
    d = d.sort_values(["priority", "retrieved_at"], ascending=[True, False])
    return d.groupby(["datetime", "region"], sort=True).first().reset_index().drop(columns="priority")


def stats(con: sqlite3.Connection, ids: list[int]) -> dict:
    _allowed(con, ids)
    rows = con.execute("SELECT r.name, COUNT(*), SUM(r.duplicates_dropped) FROM retrievals r JOIN allowed a ON a.id = r.id"
                       " GROUP BY r.name ORDER BY MIN(r.retrieved_ns)").fetchall()
    return {"snapshots": {n: c for n, c, _ in rows}, "duplicates_dropped": int(sum(d for *_, d in rows))}


def revisions(con: sqlite3.Connection, ids: list[int]) -> int:
    """Quarter-hours of region Belgium whose measured value differs between the given retrievals."""
    _allowed(con, ids)
    return con.execute(
        "SELECT COUNT(*) FROM (SELECT e.start_ns FROM seen s JOIN allowed a ON a.id = s.retrieval_id"
        " JOIN elia_rows e ON e.id = s.row_id WHERE e.region = 'Belgium' AND e.measured_mw IS NOT NULL"
        " GROUP BY e.start_ns HAVING MAX(e.measured_mw) - MIN(e.measured_mw) > 1e-6)").fetchone()[0]
