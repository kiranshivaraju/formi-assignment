"""Store guarantees, against a real Postgres (S24). Needs the compose db: `docker compose up -d --wait db`.

Uses a throwaway database `formi_test`, recreated per session.
"""
import json
import os

import psycopg
import pytest

from controller import store

PORT = os.environ.get("FORMI_PG_PORT", "55432")
ADMIN_DSN = f"postgresql://formi:formi@127.0.0.1:{PORT}/formi"
TEST_DSN = f"postgresql://formi:formi@127.0.0.1:{PORT}/formi_test"

URL = "http://sim.test:8700"
STATE = {"scenario": "t", "sim_minute": 0, "ts": "2027-01-04T00:00:00+05:30", "chunks": 1,
         "last_event_id": 0}


@pytest.fixture(scope="session", autouse=True)
def test_db():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS formi_test WITH (FORCE)")
        c.execute("CREATE DATABASE formi_test")


@pytest.fixture
def conn():
    with store.connect(TEST_DSN) as c:
        yield c


@pytest.fixture
def run(conn, request):
    r, created = store.resolve_run(conn, URL, {**STATE, "scenario": request.node.name})
    assert created
    return r


def call(eid, *, connected=True, engaged=False, halluc=False, ts="2027-01-04T10:05:00+05:30"):
    ev = {"type": "call", "ts": ts, "workflow_id": "wf", "cohort_id": "default", "event_id": eid,
          "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
          "connected": connected, "engaged": engaged}
    if connected:
        ev["qa"] = {"hallucination_flag": halluc, "flow_adherence": 0.9}
    return json.dumps(ev)


def chunk_meta(n, lines, until_minute=600):
    last = json.loads(lines[-1])["event_id"]
    return {"chunk": n, "events": len(lines), "last_event_id": last, "until_minute": until_minute}


def stats(conn, run):
    return conn.execute(
        "SELECT sim_day, hour, dials, connected, engaged, hallucinations FROM call_stats WHERE run_id=%s",
        (run.run_id,)).fetchall()


def test_ingest_counts_and_scoreboard(conn, run):
    lines = [call(1), call(2, engaged=True), call(3, connected=False), call(4, halluc=True)]
    assert store.ingest_chunk(conn, run, chunk_meta(0, lines), lines) == 4
    assert stats(conn, run) == [{"sim_day": 0, "hour": 10, "dials": 4, "connected": 3,
                                 "engaged": 1, "hallucinations": 1}]
    assert run.last_chunk == 0 and run.last_event_id == 4


def test_same_chunk_twice_is_a_noop(conn, run):
    lines = [call(1), call(2)]
    store.ingest_chunk(conn, run, chunk_meta(0, lines), lines)
    assert store.ingest_chunk(conn, run, chunk_meta(0, lines), lines) == 0
    assert conn.execute("SELECT count(*) AS n FROM events WHERE run_id=%s", (run.run_id,)).fetchone()["n"] == 2
    assert stats(conn, run)[0]["dials"] == 2


def test_redelivered_event_never_double_counts(conn, run):
    """Even if a later chunk repeats an event, the scoreboard counts only newly inserted rows."""
    a, b = [call(1), call(2)], [call(2), call(3)]
    store.ingest_chunk(conn, run, chunk_meta(0, a), a)
    store.ingest_chunk(conn, run, chunk_meta(1, b), b)
    assert stats(conn, run)[0]["dials"] == 3


def test_crash_mid_chunk_leaves_nothing_behind(conn, run):
    lines = [call(1), call(2), call(3)]

    def dies_halfway():
        yield lines[0]
        yield lines[1]
        raise ConnectionError("killed mid-stream")

    with pytest.raises(ConnectionError):
        store.ingest_chunk(conn, run, chunk_meta(0, lines), dies_halfway())
    assert stats(conn, run) == []
    assert conn.execute("SELECT last_chunk FROM runs WHERE run_id=%s", (run.run_id,)).fetchone()["last_chunk"] == -1
    assert store.ingest_chunk(conn, run, chunk_meta(0, lines), lines) == 3  # retry succeeds


def test_short_chunk_is_rejected(conn, run):
    lines = [call(1), call(2)]
    with pytest.raises(RuntimeError, match="expected 3 events"):
        store.ingest_chunk(conn, run, {**chunk_meta(0, lines), "events": 3}, lines)
    assert stats(conn, run) == []


def test_history_lands_on_negative_days(conn, run):
    lines = [call(1, ts="2027-01-03T23:30:00+05:30")]
    store.ingest_chunk(conn, run, chunk_meta(0, lines, until_minute=0), lines)
    assert stats(conn, run)[0] == {"sim_day": -1, "hour": 23, "dials": 1, "connected": 1,
                                  "engaged": 0, "hallucinations": 0}


def test_config_change_goes_to_diary_once(conn, run):
    ev = json.dumps({"type": "config_changed", "ts": "2027-01-07T11:30:00+05:30", "workflow_id": "wf",
                     "version": 4, "actor": "supervisor", "changed_dims": ["prompt_version"],
                     "proposal_id": None, "approval_id": None, "event_id": 7})
    store.ingest_chunk(conn, run, chunk_meta(0, [ev]), [ev])
    rows = conn.execute("SELECT sim_ts, kind, reason FROM decisions WHERE run_id=%s", (run.run_id,)).fetchall()
    assert rows == [{"sim_ts": "D3 11:30", "kind": "config_observed",
                     "reason": "config v4 applied by supervisor (dims: prompt_version)"}]


def test_diary_is_append_only(conn, run):
    assert store.record_decision(conn, run, key="k", sim_minute=0, workflow_id="wf", kind="x",
                                 action="no_action", reason="r", inputs={})
    assert not store.record_decision(conn, run, key="k", sim_minute=0, workflow_id="wf", kind="x",
                                     action="no_action", reason="r", inputs={})
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with conn.transaction():
            conn.execute("UPDATE decisions SET reason='rewritten' WHERE run_id=%s", (run.run_id,))


def test_resume_same_world_but_detect_restarted_sim(conn, run):
    lines = [call(1), call(2)]
    store.ingest_chunk(conn, run, chunk_meta(0, lines), lines)
    same = {**STATE, "scenario": run.scenario, "sim_minute": 615, "ts": "2027-01-04T10:15:00+05:30",
            "chunks": 2, "last_event_id": 9}
    resumed, created = store.resolve_run(conn, URL, same)
    assert not created and resumed.run_id == run.run_id and resumed.last_chunk == 0
    restarted = {**STATE, "scenario": run.scenario}  # clock back at 0, nothing ingested yet
    fresh, created = store.resolve_run(conn, URL, restarted)
    assert created and fresh.run_id != run.run_id


def test_other_sim_of_same_scenario_never_resumes_our_run(conn, run):
    """Two sims of the same scenario sharing a database must not adopt each other's run."""
    lines = [call(1)]
    store.ingest_chunk(conn, run, chunk_meta(0, lines), lines)
    ahead = {**STATE, "scenario": run.scenario, "sim_minute": 615, "ts": "2027-01-04T10:15:00+05:30",
             "chunks": 5, "last_event_id": 99}
    other, created = store.resolve_run(conn, "http://other-sim:8711", ahead)
    assert created and other.run_id != run.run_id


def test_one_controller_per_sim(conn):
    assert store.lock_sim(conn, URL + "/lock")
    with store.connect(TEST_DSN) as second:
        assert not store.lock_sim(second, URL + "/lock")


def test_older_database_is_refused(conn):
    with conn.transaction():
        conn.execute("UPDATE schema_meta SET version = 1")
    try:
        with pytest.raises(store.SchemaMismatch, match="docker compose down -v"):
            store.connect(TEST_DSN)
    finally:
        with conn.transaction():
            conn.execute("UPDATE schema_meta SET version = %s", (store.SCHEMA_VERSION,))


def test_malformed_events_do_not_block_ingest(conn, run):
    cfg = json.dumps({"type": "config_changed", "ts": "2027-01-04T10:00:00+05:30", "workflow_id": "wf",
                      "version": 2, "actor": None, "changed_dims": None, "event_id": 1})
    bare = json.dumps({"type": "call", "ts": "2027-01-04T10:01:00+05:30", "workflow_id": "wf",
                       "event_id": 2, "connected": False, "engaged": False})
    store.ingest_chunk(conn, run, chunk_meta(0, [cfg, bare]), [cfg, bare])
    reason = conn.execute("SELECT reason FROM decisions WHERE run_id=%s", (run.run_id,)).fetchone()["reason"]
    assert reason == "config v2 applied by ? (dims: )"
    assert stats(conn, run)[0]["dials"] == 1


def test_bookmark_clock_never_moves_backwards(conn, run):
    store.mark_covered(conn, run, 600)
    lines = [call(1, ts="2027-01-03T20:00:00+05:30")]
    store.ingest_chunk(conn, run, chunk_meta(0, lines, until_minute=-240), lines)
    assert run.sim_minute == 600
    assert conn.execute("SELECT sim_minute FROM runs WHERE run_id=%s", (run.run_id,)).fetchone()["sim_minute"] == 600


def test_each_step_commits_durably(conn, run):
    """Regression: a bare read must not leave a transaction open that swallows later commits."""
    from controller import think
    lines = [call(1)]
    store.ingest_chunk(conn, run, chunk_meta(0, lines, until_minute=1440), lines)
    think.write_daily_summaries(conn, run, ["wf"])
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    more = [call(2, ts="2027-01-05T10:00:00+05:30")]
    store.ingest_chunk(conn, run, chunk_meta(1, more, until_minute=2000), more)
    with psycopg.connect(TEST_DSN) as other:  # a second connection only sees committed data
        n = other.execute("SELECT count(*) FROM events WHERE run_id=%s", (run.run_id,)).fetchone()[0]
        bookmark = other.execute("SELECT last_chunk FROM runs WHERE run_id=%s", (run.run_id,)).fetchone()[0]
    assert (n, bookmark) == (2, 1)


def test_stage_is_empty_after_each_chunk(conn, run):
    for i in range(3):
        lines = [call(10 * i + 1), call(10 * i + 2)]
        store.ingest_chunk(conn, run, chunk_meta(i, lines), lines)
    assert conn.execute("SELECT count(*) AS n FROM stage").fetchone()["n"] == 0


def test_prune_keeps_counts_and_small_events(conn, run):
    """D6: raw calls older than the window go; the scoreboard and config events stay."""
    lines = [call(1, ts="2027-01-04T10:05:00+05:30"), call(2, ts="2027-01-08T10:05:00+05:30"),
             json.dumps({"type": "config_changed", "ts": "2027-01-04T11:00:00+05:30", "workflow_id": "wf",
                         "version": 2, "actor": "supervisor", "changed_dims": ["voice"], "event_id": 3})]
    store.ingest_chunk(conn, run, {"chunk": 0, "events": 3, "last_event_id": 3, "until_minute": 6 * 1440}, lines)
    assert store.prune_raw_events(conn, run, 2 * 1440, 6 * 1440) == 1
    left = [r["type"] for r in conn.execute("SELECT type FROM events WHERE run_id=%s ORDER BY event_id",
                                            (run.run_id,)).fetchall()]
    assert left == ["call", "config_changed"]       # the Jan 8 call and the config event
    assert conn.execute("SELECT sum(dials) AS d FROM call_stats WHERE run_id=%s", (run.run_id,)).fetchone()["d"] == 2
    assert store.prune_raw_events(conn, run, 2 * 1440, 6 * 1440) == 0     # repeat is a no-op
