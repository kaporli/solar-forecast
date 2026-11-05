import json

import pandas as pd
import pytest

from solar_forecast import index as I
from solar_forecast import sources

T0 = "2026-11-05T10:00:00Z"


def csv(measured="50.0", da11="40.0", start=T0) -> bytes:
    """One quarter-hour of Belgium in the ods032/ods210 column layout."""
    return (f"datetime,resolutioncode,region,measured,monitoredcapacity,dayahead11hforecast,dayaheadforecast\n"
            f"{start},PT15M,Belgium,{measured},1000.0,{da11},45.0\n").encode()


@pytest.fixture
def put(tmp_path):
    store = tmp_path / "snapshots"

    def put(body, retrieved, dataset="ods210"):
        sources.save("elia", f"{dataset}_total", "csv", body, {"retrieved_at": retrieved, "dataset": dataset}, store)
        return [m for m in sources.snapshots("elia", store) if m["retrieved_at"] == retrieved][0]
    put.store = store
    return put


def resolve(metas, as_of=None):
    con = I.connect(I.path_for(metas))
    return I.resolved(con, I.ingest(con, metas), as_of).set_index("region").loc["Belgium"], con


def test_newer_retrieval_without_a_forecast_keeps_the_older_value(put):
    a = put(csv(da11="40.0"), "2026-11-05T09:00:00+00:00")
    b = put(csv(da11="", measured="55.0"), "2026-11-05T12:00:00+00:00")
    r, _ = resolve([a, b])
    assert (r["measured_mw"], r["elia_da11_mw"]) == (55.0, 40.0)  # per field, newest non-null


def test_unchanged_observation_is_stored_once_and_keeps_every_retrieval(put):
    a = put(csv(), "2026-11-05T09:00:00+00:00")
    b = put(csv(), "2026-11-05T12:00:00+00:00")
    r, con = resolve([a, b])
    assert con.execute("SELECT COUNT(*) FROM elia_rows").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 2
    only_b, _ = resolve([b])  # a pinned build of the later retrieval alone still sees the value
    assert only_b["measured_mw"] == 50.0 and only_b["retrieved_at"] == pd.Timestamp(b["retrieved_at"])


def test_revision_is_available_from_its_retrieval_only(put):
    a = put(csv(measured="50.0"), "2026-11-05T09:00:00+00:00")
    b = put(csv(measured="52.0"), "2026-11-06T09:00:00+00:00")
    before, con = resolve([a, b], pd.Timestamp("2026-11-05T23:00Z"))
    after, _ = resolve([a, b])
    assert (before["measured_mw"], after["measured_mw"]) == (50.0, 52.0)
    assert I.revisions(con, I.ingest(con, [a, b])) == 1


def test_archive_dataset_wins_over_a_newer_live_value(put):
    archive = put(csv(measured="50.0"), "2026-11-05T09:00:00+00:00", dataset="ods032")
    live = put(csv(measured="60.0"), "2026-11-05T12:00:00+00:00", dataset="ods210")
    r, _ = resolve([archive, live])
    assert r["measured_mw"] == 50.0


def test_index_is_rebuilt_from_the_archive_alone(put):
    metas = [put(csv(measured="50.0"), "2026-11-05T09:00:00+00:00"), put(csv(measured="52.0", da11=""), "2026-11-05T12:00:00+00:00")]
    first, con = resolve(metas)
    con.close()
    I.path_for(metas).unlink()
    again, _ = resolve(metas)
    pd.testing.assert_series_equal(first, again)


def test_a_sidecar_changed_after_indexing_is_refused(put):
    m = put(csv(), "2026-11-05T09:00:00+00:00")
    resolve([m])
    meta = json.loads(m["sidecar"].read_text())
    meta["retrieved_at"] = "2026-11-05T08:00:00+00:00"
    m["sidecar"].write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="sidecar differs"):
        resolve(sources.snapshots("elia", m["sidecar"].parents[1]))


def test_since_limits_the_quarter_hours_queried(put):
    m = put(csv(start=T0) + csv(start="2026-11-04T10:00:00Z").split(b"\n", 1)[1], "2026-11-05T12:00:00+00:00")
    con = I.connect(I.path_for([m]))
    ids = I.ingest(con, [m])
    assert len(I.resolved(con, ids)) == 2 and len(I.resolved(con, ids, since=pd.Timestamp("2026-11-05", tz="UTC"))) == 1
