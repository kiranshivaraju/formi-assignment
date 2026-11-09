-- Controller state. Safe to run on every start (S4).
-- Every row is tagged with run_id so several simulator worlds can share one database (S23).

-- Version of this file; checked by store.connect before anything else (bump SCHEMA_VERSION).
CREATE TABLE IF NOT EXISTS schema_meta (version int PRIMARY KEY);

-- One row per simulator world we have driven. Also holds the ingest bookmark (S10).
-- A world is identified by the simulator URL + scenario, then checked against its clock (S22).
CREATE TABLE IF NOT EXISTS runs (
    run_id       serial PRIMARY KEY,
    scenario     text        NOT NULL,
    sim_url      text        NOT NULL,
    token        text        NOT NULL,  -- from the sim state at creation: the same world gives the same
                                    -- ids (reproducible approver draws), a DB reset mid-world a new one
    d0           timestamptz NOT NULL,          -- D0 00:00 in sim time, derived from the API
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_chunk   int         NOT NULL DEFAULT -1, -- bookmark: highest chunk fully ingested
    last_event_id bigint     NOT NULL DEFAULT 0,
    sim_minute   int         NOT NULL DEFAULT 0   -- sim clock covered by ingested chunks
);

-- Every event exactly once (S9), full JSON kept for audits (S15).
CREATE TABLE IF NOT EXISTS events (
    run_id      int         NOT NULL REFERENCES runs,
    event_id    bigint      NOT NULL,
    type        text        NOT NULL,
    workflow_id text,
    ts          timestamptz NOT NULL,
    body        jsonb       NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_wf_type_ts ON events (run_id, workflow_id, type, ts);

-- The scoreboard (S16): counts per workflow x day x hour x cohort x variant.
-- sim_day is negative for history (before D0). hour is local wall-clock hour of the dial.
CREATE TABLE IF NOT EXISTS call_stats (
    run_id          int  NOT NULL REFERENCES runs,
    workflow_id     text NOT NULL,
    sim_day         int  NOT NULL,
    hour            int  NOT NULL,
    cohort_id       text NOT NULL,
    welcome_message text NOT NULL,
    voice           text NOT NULL,
    caller_id       text NOT NULL,
    prompt_version  text NOT NULL,
    dials           int  NOT NULL,
    connected       int  NOT NULL,
    engaged         int  NOT NULL,
    hallucinations  int  NOT NULL,
    PRIMARY KEY (run_id, workflow_id, sim_day, hour, cohort_id,
                 welcome_message, voice, caller_id, prompt_version)
);

-- The diary (S17, S18). decision_key makes every write idempotent across restarts.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id  bigserial PRIMARY KEY,
    run_id       int   NOT NULL REFERENCES runs,
    decision_key text  NOT NULL,
    sim_minute   int   NOT NULL,   -- minutes since D0 00:00 (negative = history)
    sim_ts       text  GENERATED ALWAYS AS (           -- human label, e.g. 'D3 14:00'
        'D' || floor(sim_minute / 1440.0)::int || ' '
        || lpad((((sim_minute % 1440) + 1440) % 1440 / 60)::text, 2, '0') || ':'
        || lpad((((sim_minute % 60) + 60) % 60)::text, 2, '0')) STORED,
    workflow_id  text,
    kind         text  NOT NULL,   -- e.g. daily_summary, config_observed
    action       text  NOT NULL,   -- e.g. no_action, recorded
    reason       text  NOT NULL,
    inputs       jsonb NOT NULL,   -- the numbers the decision was based on
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, decision_key)
);

CREATE OR REPLACE FUNCTION decisions_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decisions is append-only';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_no_update ON decisions;
CREATE TRIGGER decisions_no_update BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION decisions_append_only();

-- Changes features want (G1). One active row per intent_key: a feature that keeps detecting the
-- same thing refreshes its queued row instead of piling up duplicates.
CREATE TABLE IF NOT EXISTS changes (
    change_id      bigserial PRIMARY KEY,
    run_id         int     NOT NULL REFERENCES runs,
    intent_key     text    NOT NULL,      -- e.g. 'caller_id_health:ks_cancel:D2'
    workflow_id    text    NOT NULL,
    urgent         boolean NOT NULL,
    status         text    NOT NULL,      -- queued | in_flight | applied | dropped | rejected | expired |
                                          -- refused
    proposal_id    text,
    not_before_minute int  NOT NULL DEFAULT 0,  -- T3: next step of a stepped change waits until then
    steps_done     int     NOT NULL DEFAULT 0,  -- T3: partial steps already applied
    created_minute int     NOT NULL,
    updated_minute int     NOT NULL,
    body           jsonb   NOT NULL       -- patch, dims, summary, rationale, numbers
);
CREATE UNIQUE INDEX IF NOT EXISTS changes_one_active ON changes (run_id, intent_key)
    WHERE status IN ('queued', 'in_flight');

-- Requests we send, approval or auto-apply (G10). Status is the current state; the diary holds
-- the history.
CREATE TABLE IF NOT EXISTS proposals (
    run_id          int     NOT NULL REFERENCES runs,
    proposal_id     text    NOT NULL,
    workflow_id     text    NOT NULL,
    path            text    NOT NULL,     -- approval | auto
    urgent          boolean NOT NULL,
    status          text    NOT NULL,     -- sending | pending | approved | applying | applied |
                                          -- rejected | expired | lapsed | stale | superseded | refused |
                                          -- withdrawn
    base_version    int     NOT NULL,
    dims            text[]  NOT NULL,     -- settings this request changes
    approval_id     text,
    sent_minute     int     NOT NULL,
    decided_minute  int,
    applied_version int,
    change_ids      bigint[] NOT NULL,
    reasons         jsonb,
    request         jsonb   NOT NULL,     -- exactly what we sent
    partial         boolean NOT NULL DEFAULT false,  -- T3: one step of a bigger change
    PRIMARY KEY (run_id, proposal_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS proposals_by_approval ON proposals (run_id, approval_id);

-- Settings a supervisor just edited, left alone until judged on enough calls (G21).
CREATE TABLE IF NOT EXISTS holds (
    run_id        int  NOT NULL REFERENCES runs,
    workflow_id   text NOT NULL,
    dim           text NOT NULL,
    since_minute  int  NOT NULL,
    processed     boolean NOT NULL DEFAULT false,  -- conflicting changes already dropped
    released_minute int,
    PRIMARY KEY (run_id, workflow_id, dim, since_minute)
);

-- Small durable memory for features (e.g. which caller IDs were removed and when to retry them).
-- Written in the same transaction as the diary line that explains it.
CREATE TABLE IF NOT EXISTS feature_state (
    run_id         int   NOT NULL REFERENCES runs,
    key            text  NOT NULL,      -- e.g. 'caller_id:ks_cancel:D2'
    body           jsonb NOT NULL,
    updated_minute int   NOT NULL,
    PRIMARY KEY (run_id, key)
);

-- C2: each lead's features (language, age band, city tier, value band), so a call can be counted
-- per customer group. Kept LEAD_KEEP_DAYS after the lead arrives (its campaign is over by then).
CREATE TABLE IF NOT EXISTS leads (
    run_id       int   NOT NULL REFERENCES runs,
    lead_id      text  NOT NULL,
    workflow_id  text  NOT NULL,
    seen_minute  int   NOT NULL,
    features     jsonb NOT NULL,
    PRIMARY KEY (run_id, lead_id)
);
CREATE INDEX IF NOT EXISTS leads_by_age ON leads (run_id, seen_minute);

-- E12: the scoreboard per customer group (one feature at a time) and greeting/voice, for the harm
-- guard before a winner is promoted. One row per call per feature, written with call_stats.
CREATE TABLE IF NOT EXISTS segment_stats (
    run_id          int  NOT NULL REFERENCES runs,
    workflow_id     text NOT NULL,
    sim_day         int  NOT NULL,
    hour            int  NOT NULL,
    cohort_id       text NOT NULL,
    feature         text NOT NULL,
    value           text NOT NULL,
    welcome_message text NOT NULL,
    voice           text NOT NULL,
    dials           int  NOT NULL,
    connected       int  NOT NULL,
    engaged         int  NOT NULL,
    hallucinations  int  NOT NULL,
    PRIMARY KEY (run_id, workflow_id, sim_day, hour, cohort_id, feature, value, welcome_message, voice)
);
