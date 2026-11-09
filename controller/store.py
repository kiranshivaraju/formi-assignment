"""Postgres state store: runs + bookmark, raw events, scoreboard, diary.

The central guarantee (S10, S16): a chunk's raw events, its scoreboard counts, any diary rows it
implies, and the bookmark move all commit in ONE transaction. A crash at any point leaves either
all of it or none of it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).with_name("schema.sql")
MINUTES_PER_DAY = 1440
SCHEMA_VERSION = 6  # bump on any schema.sql change; an older database then fails fast


class SchemaMismatch(RuntimeError):
    pass


@dataclass
class Run:
    run_id: int
    scenario: str
    sim_url: str
    token: str
    d0: datetime
    last_chunk: int
    last_event_id: int
    sim_minute: int


def connect(dsn: str) -> psycopg.Connection:
    """Autocommit connection: every `with conn.transaction()` block is a real, durable commit.

    Without autocommit, any bare read opens an implicit transaction that never closes, so every
    later transaction() block silently becomes a savepoint inside it and nothing is committed
    until exit. (Found by profiling: ingest slowed as the uncommitted temp stage grew.)
    """
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(4242)")  # concurrent starts don't race on DDL
        # CREATE IF NOT EXISTS never alters an existing table, so refuse an older database
        # instead of failing mid-run on a missing column.
        if conn.execute("SELECT to_regclass('runs') IS NOT NULL AS has").fetchone()["has"]:
            row = None
            if conn.execute("SELECT to_regclass('schema_meta') IS NOT NULL AS has").fetchone()["has"]:
                row = conn.execute("SELECT max(version) AS v FROM schema_meta").fetchone()
            found = row["v"] if row else None
            if found != SCHEMA_VERSION:
                raise SchemaMismatch(
                    f"database schema v{found}, code expects v{SCHEMA_VERSION}. Reset the controller "
                    f"database with `docker compose down -v` (this deletes its state).")
        conn.execute(SCHEMA.read_text())
        conn.execute("INSERT INTO schema_meta (version) VALUES (%s) ON CONFLICT DO NOTHING",
                     (SCHEMA_VERSION,))
    return conn


def lock_sim(conn: psycopg.Connection, sim_url: str) -> bool:
    """Hold a session lock per simulator for the controller's lifetime: one driver per world.

    Released automatically when the process dies (its connection closes).
    """
    return conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (sim_url,)).fetchone()["ok"]


def d0_from_state(state: dict) -> datetime:
    """D0 00:00 derived from the API's own clock, never hard-coded."""
    return datetime.fromisoformat(state["ts"]) - timedelta(minutes=state["sim_minute"])


def is_new_world(run: Run, state: dict) -> bool:
    """The sim keeps its world in memory; a restarted sim is a brand-new world (S22).

    If its clock or chunk count is behind what we already ingested, it cannot be the world we
    were driving.
    """
    return (state["scenario"] != run.scenario
            or state["sim_minute"] < run.sim_minute
            or state["chunks"] <= run.last_chunk
            or state["last_event_id"] < run.last_event_id
            or d0_from_state(state) != run.d0)


def run_token(state: dict) -> str:
    """Part of every proposal id. Deterministic, because the sim's approver draws its decision from
    the proposal id: random ids would make every run unrepeatable. Still differs if our database is
    reset while the same world runs on (the clock and event count have moved), so ids never repeat."""
    key = f"{state['scenario']}|{state['ts']}|{state['sim_minute']}|{state['last_event_id']}"
    return hashlib.md5(key.encode()).hexdigest()[:8]


def _run_from_row(row: dict) -> Run:
    return Run(row["run_id"], row["scenario"], row["sim_url"], row["token"], row["d0"], row["last_chunk"],
               row["last_event_id"], row["sim_minute"])


def resolve_run(conn: psycopg.Connection, sim_url: str, state: dict) -> tuple[Run, bool]:
    """Resume the latest run for this simulator (URL + scenario) if it's the same world, else
    start a new one. Keying on the URL keeps two sims of the same scenario from resuming each
    other's run. Returns (run, created).
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM runs WHERE sim_url = %s AND scenario = %s ORDER BY run_id DESC LIMIT 1",
            (sim_url, state["scenario"])).fetchone()
        if row and not is_new_world(_run_from_row(row), state):
            return _run_from_row(row), False
        row = conn.execute(
            "INSERT INTO runs (scenario, sim_url, token, d0) VALUES (%s, %s, %s, %s) RETURNING *",
            (state["scenario"], sim_url, run_token(state), d0_from_state(state))).fetchone()
        return _run_from_row(row), True


# Raw events and scoreboard in one statement: the scoreboard only counts rows that were actually
# inserted, so even a duplicate event can never be counted twice.
_INGEST_SQL = """
WITH ins AS (
    INSERT INTO events (run_id, event_id, type, workflow_id, ts, body)
    SELECT %(run_id)s, (body->>'event_id')::bigint, body->>'type', body->>'workflow_id',
           (body->>'ts')::timestamptz, body
    FROM stage
    ON CONFLICT DO NOTHING
    RETURNING type, ts, body
), calls AS (
    SELECT body, floor(extract(epoch FROM ts - %(d0)s) / 86400)::int AS sim_day,
           extract(epoch FROM ts - %(d0)s) AS secs
    FROM ins WHERE type = 'call'
), seg AS (  -- E12: the same calls per customer group; runs even though nothing reads it
    INSERT INTO segment_stats AS g (run_id, workflow_id, sim_day, hour, cohort_id, feature, value,
                                    welcome_message, voice, dials, connected, engaged, hallucinations)
    SELECT %(run_id)s, c.body->>'workflow_id', c.sim_day,
           floor((c.secs - c.sim_day * 86400) / 3600)::int,
           coalesce(c.body->>'cohort_id', '?'), f.key, coalesce(f.value, '?'),  -- {"lang": null}
           coalesce(c.body->'variant'->>'welcome_message', '?'), coalesce(c.body->'variant'->>'voice', '?'),
           count(*),
           count(*) FILTER (WHERE (c.body->>'connected')::bool),
           count(*) FILTER (WHERE (c.body->>'engaged')::bool),
           count(*) FILTER (WHERE coalesce((c.body->'qa'->>'hallucination_flag')::bool, false))
    FROM calls c
    JOIN leads l ON l.run_id = %(run_id)s AND l.lead_id = c.body->>'lead_id'
    CROSS JOIN LATERAL jsonb_each_text(l.features) f
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
    ON CONFLICT (run_id, workflow_id, sim_day, hour, cohort_id, feature, value, welcome_message, voice)
    DO UPDATE SET dials = g.dials + EXCLUDED.dials,
                  connected = g.connected + EXCLUDED.connected,
                  engaged = g.engaged + EXCLUDED.engaged,
                  hallucinations = g.hallucinations + EXCLUDED.hallucinations
)
INSERT INTO call_stats AS s (run_id, workflow_id, sim_day, hour, cohort_id, welcome_message, voice,
                             caller_id, prompt_version, dials, connected, engaged, hallucinations)
SELECT %(run_id)s, body->>'workflow_id', sim_day,
       floor((secs - sim_day * 86400) / 3600)::int,
       coalesce(body->>'cohort_id', '?'),  -- '?' rather than one odd event blocking ingest
       coalesce(body->'variant'->>'welcome_message', '?'), coalesce(body->'variant'->>'voice', '?'),
       coalesce(body->'variant'->>'caller_id', '?'), coalesce(body->'variant'->>'prompt_version', '?'),
       count(*),
       count(*) FILTER (WHERE (body->>'connected')::bool),
       count(*) FILTER (WHERE (body->>'engaged')::bool),
       count(*) FILTER (WHERE coalesce((body->'qa'->>'hallucination_flag')::bool, false))
FROM calls
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
ON CONFLICT (run_id, workflow_id, sim_day, hour, cohort_id, welcome_message, voice, caller_id,
             prompt_version)
DO UPDATE SET dials = s.dials + EXCLUDED.dials,
              connected = s.connected + EXCLUDED.connected,
              engaged = s.engaged + EXCLUDED.engaged,
              hallucinations = s.hallucinations + EXCLUDED.hallucinations
"""

# C2: lead features, saved before the calls in the same chunk are counted per group (E12).
_LEADS_SQL = """
INSERT INTO leads (run_id, lead_id, workflow_id, seen_minute, features)
SELECT %(run_id)s, body->>'lead_id', body->>'workflow_id',
       floor(extract(epoch FROM (body->>'ts')::timestamptz - %(d0)s) / 60)::int,
       CASE WHEN jsonb_typeof(body->'features') = 'object' THEN body->'features' ELSE '{}'::jsonb END
FROM stage WHERE body->>'type' = 'lead' AND body->>'lead_id' IS NOT NULL
ON CONFLICT DO NOTHING
"""
LEAD_KEEP_DAYS = 14  # a lead's campaign (cadence of 3-4 attempts) is long over by then

# Every observed config change becomes a diary row (S20), keyed by event so it's written once.
_CONFIG_DIARY_SQL = """
INSERT INTO decisions (run_id, decision_key, sim_minute, workflow_id, kind, action, reason, inputs)
SELECT %(run_id)s, 'config_changed:' || (body->>'event_id'),
       floor(extract(epoch FROM (body->>'ts')::timestamptz - %(d0)s) / 60)::int,
       body->>'workflow_id', 'config_observed', 'recorded',
       format('config v%%s applied by %%s (dims: %%s)',  -- NULL-safe, unlike ||; %% escapes psycopg
              coalesce(body->>'version', '?'), coalesce(body->>'actor', '?'),
              coalesce((SELECT string_agg(d, ', ') FROM jsonb_array_elements_text(
                  CASE WHEN jsonb_typeof(body->'changed_dims') = 'array' THEN body->'changed_dims'
                       ELSE '[]'::jsonb END) d), '')),
       body
FROM stage WHERE body->>'type' = 'config_changed'
ON CONFLICT (run_id, decision_key) DO NOTHING
"""


# The approver's decision lands on our tracker and in the diary in the same transaction as the
# event itself (G14). Only a pending request can be decided.
_DECISION_SQL = """
WITH d AS (
    SELECT body->>'approval_id' AS approval_id, body->>'status' AS status, body->'reasons' AS reasons,
           floor(extract(epoch FROM (body->>'ts')::timestamptz - %(d0)s) / 60)::int AS minute,
           body->>'workflow_id' AS workflow_id, body->>'event_id' AS event_id
    FROM stage WHERE body->>'type' = 'approval_decided'
), upd AS (
    UPDATE proposals p SET status = d.status, decided_minute = d.minute, reasons = d.reasons
    FROM d WHERE p.run_id = %(run_id)s AND p.approval_id = d.approval_id AND p.status = 'pending'
    RETURNING p.proposal_id, p.approval_id
)
INSERT INTO decisions (run_id, decision_key, sim_minute, workflow_id, kind, action, reason, inputs)
SELECT %(run_id)s, 'approval_decided:' || d.approval_id, d.minute, d.workflow_id, 'approval_decided',
       d.status,
       format('client approver %%s request %%s (%%s)%%s', d.status, coalesce(p.proposal_id, '?'),
              d.approval_id,
              CASE WHEN jsonb_array_length(coalesce(d.reasons, '[]')) > 0
                   THEN ': ' || (SELECT string_agg(r, '; ') FROM jsonb_array_elements_text(d.reasons) r)
                   ELSE '' END),
       jsonb_build_object('approval_id', d.approval_id, 'proposal_id', p.proposal_id,
                          'status', d.status, 'reasons', d.reasons, 'event_id', d.event_id)
FROM d LEFT JOIN proposals p ON p.run_id = %(run_id)s AND p.approval_id = d.approval_id
ON CONFLICT (run_id, decision_key) DO NOTHING
"""

# A supervisor edit puts a hold on each setting it changed (G20, G21).
_HOLD_SQL = """
INSERT INTO holds (run_id, workflow_id, dim, since_minute)
SELECT %(run_id)s, body->>'workflow_id', dim,
       floor(extract(epoch FROM (body->>'ts')::timestamptz - %(d0)s) / 60)::int
FROM stage, jsonb_array_elements_text(
    CASE WHEN jsonb_typeof(body->'changed_dims') = 'array' THEN body->'changed_dims' ELSE '[]' END) dim
WHERE body->>'type' = 'config_changed' AND body->>'actor' = 'supervisor'
ON CONFLICT DO NOTHING
"""


def ingest_chunk(conn: psycopg.Connection, run: Run, chunk: dict, lines: Iterable[str]) -> int:
    """Save one chunk atomically. Returns the number of events in the chunk.

    Skips chunks at or below the bookmark, so calling it again for the same chunk is a no-op.
    """
    if chunk["chunk"] <= run.last_chunk:
        return 0
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS stage (body jsonb) ON COMMIT DELETE ROWS")
        n = 0
        with conn.cursor().copy("COPY stage (body) FROM STDIN") as cp:
            for line in lines:
                cp.write_row((line,))
                n += 1
        if n != chunk["events"]:
            raise RuntimeError(f"chunk {chunk['chunk']}: expected {chunk['events']} events, got {n}")
        params = {"run_id": run.run_id, "d0": run.d0}
        conn.execute(_LEADS_SQL, params)
        conn.execute(_INGEST_SQL, params)
        conn.execute(_CONFIG_DIARY_SQL, params)
        conn.execute(_DECISION_SQL, params)
        conn.execute(_HOLD_SQL, params)
        conn.execute(
            "UPDATE runs SET last_chunk = %s, last_event_id = %s, sim_minute = GREATEST(sim_minute, %s) "
            "WHERE run_id = %s",
            (chunk["chunk"], chunk["last_event_id"], chunk["until_minute"], run.run_id))
    run.last_chunk = chunk["chunk"]
    run.last_event_id = chunk["last_event_id"]
    run.sim_minute = max(run.sim_minute, chunk["until_minute"])
    return n


def sim_label(minute: int) -> str:
    """'D3 14:00', the same label the diary's sim_ts column shows."""
    return f"D{minute // MINUTES_PER_DAY} {minute % MINUTES_PER_DAY // 60:02d}:{minute % 60:02d}"


def sim_minute_of(run: Run, ts: str) -> int:
    """Minutes since D0 for an API timestamp."""
    return int((datetime.fromisoformat(ts) - run.d0).total_seconds() // 60)


def mark_covered(conn: psycopg.Connection, run: Run, sim_minute: int) -> None:
    """Record that every event up to sim_minute is ingested, even if no chunk arrived (quiet night).

    Only valid after ingesting all chunks listed AFTER reading the sim state at sim_minute: the sim
    flushes a chunk at the end of every advance, so nothing older can still be outstanding.
    """
    if sim_minute <= run.sim_minute:
        return
    with conn.transaction():
        conn.execute("UPDATE runs SET sim_minute = %s WHERE run_id = %s", (sim_minute, run.run_id))
    run.sim_minute = sim_minute


def record_decision(conn: psycopg.Connection, run: Run, *, key: str, sim_minute: int,
                    workflow_id: str | None, kind: str, action: str, reason: str, inputs: dict) -> bool:
    """Append a diary row. Returns False if this decision_key was already recorded."""
    with conn.transaction():
        cur = conn.execute(
            """INSERT INTO decisions (run_id, decision_key, sim_minute, workflow_id, kind, action,
                                      reason, inputs)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, decision_key) DO NOTHING""",
            (run.run_id, key, sim_minute, workflow_id, kind, action, reason, json.dumps(inputs)))
        return cur.rowcount == 1


def day_totals(conn: psycopg.Connection, run: Run, sim_day: int) -> dict[str, dict]:
    """Per-workflow totals for one sim day, read from the scoreboard."""
    rows = conn.execute(
        """SELECT workflow_id, sum(dials)::int AS dials, sum(connected)::int AS connected,
                  sum(engaged)::int AS engaged, sum(hallucinations)::int AS hallucinations
           FROM call_stats WHERE run_id = %s AND sim_day = %s GROUP BY workflow_id""",
        (run.run_id, sim_day)).fetchall()
    return {r["workflow_id"]: r for r in rows}


_COUNT_COLS = {"welcome_message", "voice", "caller_id", "prompt_version"}


def hourly_counts(conn: psycopg.Connection, run: Run, *, since: int, until: int | None = None,
                  workflow_id: str | None = None, exclude_workflow: str | None = None,
                  by: str | None = None, hours: list[int] | None = None) -> list[dict]:
    """Scoreboard rows summed per hour bucket (and per option of `by`, e.g. 'caller_id'):
    {start, option, dials, connected, engaged, hallucinations}. `start` is the bucket's sim minute; buckets with
    since <= start < until count. `hours` keeps only those hours of the day."""
    if by is not None and by not in _COUNT_COLS:
        raise ValueError(f"cannot count by {by}")
    opt = by or "'all'"
    return conn.execute(
        f"""SELECT sim_day * {MINUTES_PER_DAY} + hour * 60 AS start, {opt} AS option,
                   sum(dials)::int AS dials, sum(connected)::int AS connected,
                   sum(engaged)::int AS engaged, sum(hallucinations)::int AS hallucinations
            FROM call_stats
            WHERE run_id = %(r)s AND sim_day * {MINUTES_PER_DAY} + hour * 60 >= %(since)s
              AND (%(until)s::int IS NULL OR sim_day * {MINUTES_PER_DAY} + hour * 60 < %(until)s)
              AND (%(wid)s::text IS NULL OR workflow_id = %(wid)s)
              AND (%(ex)s::text IS NULL OR workflow_id <> %(ex)s)
              AND (%(hours)s::int[] IS NULL OR hour = ANY(%(hours)s))
            GROUP BY 1, 2""",
        {"r": run.run_id, "since": since, "until": until, "wid": workflow_id, "ex": exclude_workflow,
         "hours": hours}).fetchall()


def leads_called(conn: psycopg.Connection, run: Run, workflow_id: str, since: int, *,
                 caller_id: str | None = None) -> int:
    """Distinct leads dialled since a sim minute (optionally on one caller ID), from raw events."""
    return conn.execute(
        """SELECT count(DISTINCT body->>'lead_id') AS n FROM events
           WHERE run_id = %s AND workflow_id = %s AND type = 'call' AND ts >= %s
             AND (%s::text IS NULL OR body->'variant'->>'caller_id' = %s)""",
        (run.run_id, workflow_id, run.d0 + timedelta(minutes=since), caller_id, caller_id)).fetchone()["n"]


def callers_used(conn: psycopg.Connection, run: Run, workflow_id: str) -> set[str]:
    """Caller IDs with any dial in our records for a workflow (history included): not spares (L6)."""
    rows = conn.execute("SELECT DISTINCT caller_id FROM call_stats WHERE run_id=%s AND workflow_id=%s AND dials > 0",
                        (run.run_id, workflow_id)).fetchall()
    return {r["caller_id"] for r in rows}


def get_state(conn: psycopg.Connection, run: Run, key: str) -> dict:
    row = conn.execute("SELECT body FROM feature_state WHERE run_id=%s AND key=%s", (run.run_id, key)).fetchone()
    return row["body"] if row else {}


def put_state(conn: psycopg.Connection, run: Run, key: str, body: dict, minute: int) -> None:
    """Call inside the transaction that writes the diary line explaining the change."""
    conn.execute("""INSERT INTO feature_state (run_id, key, body, updated_minute) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (run_id, key) DO UPDATE SET body=EXCLUDED.body, updated_minute=EXCLUDED.updated_minute""",
                 (run.run_id, key, json.dumps(body), minute))


def last_supervisor_edit(conn: psycopg.Connection, run: Run, workflow_id: str, dim: str) -> int | None:
    """Sim minute of the latest supervisor edit to a setting (from holds, released or not)."""
    return conn.execute("SELECT max(since_minute) AS m FROM holds WHERE run_id=%s AND workflow_id=%s AND dim=%s",
                        (run.run_id, workflow_id, dim)).fetchone()["m"]


def last_config_change(conn: psycopg.Connection, run: Run, workflow_id: str, dim: str) -> int | None:
    """Sim minute of the latest observed config change to a setting (any actor), from raw events."""
    row = conn.execute("""SELECT max(ts) AS ts FROM events WHERE run_id=%s AND workflow_id=%s AND type='config_changed'
                          AND jsonb_typeof(body->'changed_dims') = 'array' AND body->'changed_dims' ? %s""",
                       (run.run_id, workflow_id, dim)).fetchone()
    return int((row["ts"] - run.d0).total_seconds() // 60) if row["ts"] else None


def sum_rows(rows: list[dict], since: int = -10**9, until: int = 10**9, options: set | None = None) -> dict:
    """Totals of hourly_counts rows with since <= start < until (and option in `options`)."""
    t = {"dials": 0, "connected": 0, "engaged": 0, "hallucinations": 0}
    for r in rows:
        if since <= r["start"] < until and (options is None or r["option"] in options):
            for k in t:
                t[k] += r[k]
    return t


def strata_counts(conn: psycopg.Connection, run: Run, workflow_id: str, dim: str, *, since: int,
                  until: int | None = None, segments: bool = False) -> list[dict]:
    """E4: counts per stratum (day, hour, cohort) and option of `dim` (welcome_message or voice), for
    comparisons made inside each stratum. With segments=True, also per customer group
    (feature, value), from segment_stats (E12)."""
    if dim not in ("welcome_message", "voice"):
        raise ValueError(f"cannot split strata by {dim}")
    table, extra = ("segment_stats", ", feature, value") if segments else ("call_stats", "")
    return conn.execute(
        f"""SELECT sim_day, hour, cohort_id{extra}, {dim} AS option,
                   sum(dials)::int AS dials, sum(connected)::int AS connected,
                   sum(engaged)::int AS engaged, sum(hallucinations)::int AS hallucinations
            FROM {table}
            WHERE run_id = %(r)s AND workflow_id = %(wid)s
              AND sim_day >= floor(%(since)s / {MINUTES_PER_DAY}.0)  -- lets the primary key narrow the scan
              AND sim_day * {MINUTES_PER_DAY} + hour * 60 >= %(since)s
              AND (%(until)s::int IS NULL OR sim_day * {MINUTES_PER_DAY} + hour * 60 < %(until)s)
            GROUP BY sim_day, hour, cohort_id{extra}, {dim}""",
        {"r": run.run_id, "wid": workflow_id, "since": since, "until": until}).fetchall()


PRUNED_TYPES = ("call", "lead")  # the bulk; small event types (config, approvals, campaigns) are kept


def prune_raw_events(conn: psycopg.Connection, run: Run, before_minute: int, now: int) -> int:
    """D6: delete raw call/lead events older than a sim minute, with a diary line in the same
    transaction. The scoreboard already holds their counts. Returns rows deleted."""
    with conn.transaction():
        n = conn.execute("DELETE FROM events WHERE run_id=%s AND type = ANY(%s) AND ts < %s",
                         (run.run_id, list(PRUNED_TYPES), run.d0 + timedelta(minutes=before_minute))).rowcount
        old_leads = conn.execute("DELETE FROM leads WHERE run_id=%s AND seen_minute < %s",
                                 (run.run_id, now - LEAD_KEEP_DAYS * MINUTES_PER_DAY)).rowcount
        if n or old_leads:
            record_decision(conn, run, key=f"prune:{before_minute}", sim_minute=now, workflow_id=None,
                            kind="storage", action="pruned",
                            reason=f"deleted {n} raw call/lead events before {sim_label(before_minute)} and "
                                   f"{old_leads} lead feature rows older than {LEAD_KEEP_DAYS} days; their counts "
                                   f"stay in the scoreboard",
                            inputs={"deleted": n, "lead_rows": old_leads, "before_minute": before_minute})
    return n
