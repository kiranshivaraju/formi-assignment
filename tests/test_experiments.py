"""Greeting/voice experiments (C6) and promotion, fading and rollback (C7), on a scripted scoreboard.

Like the caller-ID tests, these write scoreboard rows directly and hand the feature a config, so each
rule (E1-E14 in notes.md) is checked exactly. End to end it runs in scenarios 01 and 02.
"""
import json

import pytest

from controller import settings, stats, store
from controller.features import Context, experiments
from tests.test_controller_store import STATE, TEST_DSN, URL, chunk_meta, test_db  # noqa: F401

D = 1440
WF = {"objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
      "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1"]},
      "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["welcome_message"], "max_shift": 0.2}}}
KYC = {**WF, "objective": {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}}}
THIRDS = {"W1": 0.333333, "W2": 0.333333, "W3": 0.333333}
HOURS = range(9, 21)


@pytest.fixture
def db(request):
    with store.connect(TEST_DSN) as conn:
        run, _ = store.resolve_run(conn, URL, {**STATE, "scenario": request.node.name})
        yield conn, run


def cfg(welcome, cohorts=None, extra=None):
    c = {"allocations": {"default": {"welcome_message": welcome, "voice": {"V1": 1.0}}, **(extra or {})}}
    if cohorts is not None:
        c["cohorts"] = cohorts
    return {"version": 1, "config": c}


class FakeGov:
    """The gate's answers that the feature reads: rejection records per (intent, attempt), holds."""
    def __init__(self, blocked=None):
        self.blocked = blocked or {}

    def attempt_blocked(self, intent, attempt, now):
        return self.blocked.get((intent, attempt))

    def held_dims(self, wid):
        return {}


def ctx(conn, run, now, config, wf=WF, overrides=None, gov=None):
    s = settings.load()
    if overrides:
        s.raw["experiments"]["workflows"] = {"wf": overrides}
    c = Context(conn, run, sim=None, gov=gov, settings=s, workflows={"wf": wf}, now=now)
    c._configs = {"wf": config}
    return c


def put(conn, run, day, hour, welcome, connected, engaged, dials=None, table="call_stats", seg=None):
    dials = dials if dials is not None else connected * 2
    if table == "call_stats":
        conn.execute("INSERT INTO call_stats VALUES (%s,'wf',%s,%s,'default',%s,'V1','D1','P1',%s,%s,%s,0)",
                     (run.run_id, day, hour, welcome, dials, connected, engaged))
    else:
        conn.execute("INSERT INTO segment_stats VALUES (%s,'wf',%s,%s,'default',%s,%s,%s,'V1',%s,%s,%s,0)",
                     (run.run_id, day, hour, seg[0], seg[1], welcome, dials, connected, engaged))


def fill(conn, run, day, rates, per_hour=50, hours=HOURS, **kw):
    for h in hours:
        for w, r in rates.items():
            put(conn, run, day, h, w, per_hour, round(per_hour * r), **kw)


def diary(conn, run):
    return [r["reason"] for r in conn.execute("SELECT reason FROM decisions WHERE run_id=%s ORDER BY decision_id",
                                               (run.run_id,)).fetchall()]


def state(conn, run):
    return store.get_state(conn, run, "experiments:wf")["dims"]["welcome_message"]


def started(conn, run, now=60, **kw):
    """Run the first check, which starts a test of W1/W2/W3 (W1 is today's)."""
    (ch,) = experiments.check(ctx(conn, run, now, cfg({"W1": 1.0}), **kw))
    return ch


# ------------------------------------------------------------------ pure helpers

def test_stratified_lift_removes_the_good_hours_illusion():
    """E4 (Simpson): B ran mostly in the evening, when everyone engages more. Pooled, B looks 12
    points better; hour by hour, B and A are the same."""
    morning, evening = (400, 160, 100, 40), (100, 70, 400, 280)   # (nA, hitsA, nB, hitsB): 40% / 70% each
    pooled = stats.z_score(160 + 70, 500, 40 + 280, 500)
    assert pooled < -5                                              # pooled: B "clearly better"
    x = stats.stratified_lift([morning, evening])
    assert x.diff == pytest.approx(0.0) and abs(x.z) < 0.01
    one = stats.stratified_lift([(390, 195, 390, 254)])
    assert one.z == pytest.approx(-stats.z_score(195, 390, 254, 390))


def test_split_and_sample_size():
    assert experiments.test_split(["W1", "W2", "W3"], "W1", "equal") == THIRDS
    assert experiments.test_split(["W1", "W2", "W3"], "W1", 0.5) == {"W1": 0.5, "W2": 0.25, "W3": 0.25}
    assert experiments.test_split(["W1"], "W1", "equal") == {"W1": 1.0}
    assert stats.calls_needed(0.5, 0.08, 3.0) == 1152 and stats.calls_needed(0.5, 0.05, 3.0) == 2950


def test_control_share_knob_is_validated(tmp_path):
    raw = settings.DEFAULT_PATH.read_text()
    for bad in ("control_share: 0.95", "control_share: half"):
        f = tmp_path / "s.yaml"
        f.write_text(raw.replace("control_share: equal", bad))
        with pytest.raises(settings.SettingsError, match="control_share"):
            settings.load(f)


# ------------------------------------------------------------------ the feature

def test_first_step_starts_a_test(db):
    conn, run = db
    ch = started(conn, run)
    assert ch.intent_key == "experiments:wf" and not ch.urgent
    assert ch.patch == {"allocations": {"default": {"welcome_message": THIRDS}}}  # voice has one option: skipped
    assert ch.attempts == ["test greeting W1/W2/W3"]
    assert ch.summary.startswith("Test greetings W1, W2, W3 side by side") and ch.impact_score == 0.0
    # nothing changes while the config already carries the test
    assert experiments.check(ctx(conn, run, 75, cfg(THIRDS))) == []


def test_control_share_from_settings(db):
    conn, run = db
    ch = started(conn, run, overrides={"control_share": 0.5})
    assert ch.patch["allocations"]["default"]["welcome_message"] == {"W1": 0.5, "W2": 0.25, "W3": 0.25}


def test_pickup_primary_workflow_never_experiments(db):
    conn, run = db
    assert experiments.check(ctx(conn, run, 60, cfg({"W1": 1.0}), wf=KYC)) == []
    assert "people decide to pick up before they hear the greeting" in diary(conn, run)[0]


def test_clearly_worse_option_is_stopped_at_once(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.52, "W3": 0.30}, hours=range(9, 14))   # 250 answered each
    (ch,) = experiments.check(ctx(conn, run, 14 * 60, cfg(THIRDS)))
    assert ch.urgent and ch.attempts == ["stop greeting W3"] and ch.intent_key == "experiments:wf:urgent"
    assert ch.patch["allocations"]["default"]["welcome_message"] == {"W1": 0.5, "W2": 0.5}
    assert "Stop greeting W3" in ch.summary and "luck units worse" in ch.rationale


def test_winner_promoted_only_at_the_daily_check(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})          # 600 answered each
    assert experiments.check(ctx(conn, run, D + 8 * 60, cfg(THIRDS))) == []   # E2: not before 09:00
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W2": 0.433333, "W1": 0.566667}}}}
    assert ch.attempts == ["promote greeting W2"] and not ch.urgent
    assert "Give greeting W2 43% of calls (step 1 of 2)" in ch.summary and "goes to 80% tomorrow" in ch.summary
    assert 0 < ch.impact_score <= 1 and ch.dry_run["greeting"]["winner"]["n_b"] == 600


def test_small_lead_is_not_a_winner_yet(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.56, "W3": 0.50})          # +6 points on 600: ~2 luck units
    assert experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS))) == []
    assert "no winner yet" in diary(conn, run)[-1]


def test_guardrail_blocks_promotion(db):
    conn, run = db
    started(conn, run)
    for h in HOURS:
        put(conn, run, 0, h, "W1", 50, 25, dials=100)
        put(conn, run, 0, h, "W2", 50, 31, dials=200)                 # pickup 25% vs 50%
        put(conn, run, 0, h, "W3", 50, 25, dials=100)
    assert experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS))) == []
    assert any("fails a guardrail: pickup rate" in r for r in diary(conn, run))


def test_group_where_winner_does_worse_keeps_todays_option(db):
    """E12: W2 wins overall (Hindi loves it) but Tamil speakers clearly do worse: they keep W1."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.70, "W3": 0.51}, per_hour=40, table="seg", seg=("lang", "hi"))
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.30, "W3": 0.51}, per_hour=10, table="seg", seg=("lang", "ta"))
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert ch.patch["cohorts"] == [{"id": "exp_lang_ta", "where": {"lang": ["ta"]}}]
    assert ch.patch["allocations"]["exp_lang_ta"] == {"welcome_message": {"W1": 1.0}}
    assert ch.patch["allocations"]["default"]["welcome_message"] == {"W2": 0.433333, "W1": 0.566667}
    assert "lang=ta keep W1" in ch.summary


def test_no_winner_after_enough_calls_ends_the_test_for_good(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.51, "W3": 0.50}, per_hour=100)   # 1,200 each >= 1,152 needed
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 1.0}}}}
    assert ch.attempts == ["end greeting test of W1/W2/W3"] and not ch.urgent
    assert state(conn, run)["phase"] == "done"
    # E9 (Kiran): no automatic re-test, however long we wait
    assert experiments.check(ctx(conn, run, 9 * D + 9 * 60 + 15, cfg({"W1": 1.0}))) == []


def test_option_still_ahead_keeps_running_against_todays(db):
    """E9 (Kiran): W3 is +5 points (about 2.5 luck units) when the test would end: drop W2, run W3 50/50."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.49, "W3": 0.55}, per_hour=100)
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 0.5, "W3": 0.5}}}}
    assert ch.attempts == ["narrow greeting test to W1/W3"]
    # a day at 50/50 later W3 proves itself and is promoted
    fill(conn, run, 1, {"W1": 0.50, "W3": 0.56}, per_hour=150)
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 9 * 60 + 15, cfg({"W1": 0.5, "W3": 0.5})))
    assert ch.attempts == ["promote greeting W3"]
    assert ch.patch["allocations"]["default"]["welcome_message"] == {"W3": 0.8, "W1": 0.2}   # R4-M4: no step 1


def test_leader_that_falls_back_ends_the_test(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.49, "W3": 0.55}, per_hour=100)
    experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    fill(conn, run, 1, {"W1": 0.55, "W3": 0.48}, per_hour=150)
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 9 * 60 + 15, cfg({"W1": 0.5, "W3": 0.5})))
    assert ch.attempts == ["end greeting test of W1/W3"] and "fell back" in ch.rationale


def promoted_state(conn, run, live=2 * D, **extra):
    s = {"phase": "promoted", "control": "W1", "champion": "W2", "arms": ["W1", "W2", "W3"], "stopped": {},
         "start": 60, "live": live, "target": {"W2": 0.8, "W1": 0.2}, "carve": {}, "urgent_off": [],
         "promo": {"diff": 0.12, "se": 0.029}, "attempt": "promote greeting W2", "summary": "", "rationale": "",
         "dry_run": {}, "evidence": {}, "impact": 0.1, **extra}
    with conn.transaction():
        store.put_state(conn, run, "experiments:wf", {"dims": {"welcome_message": s}}, live)


def test_winner_that_fades_is_rolled_back_and_rests(db):
    """E13: W2 led by 12 points; now it is level with the 20% left on W1. Roll back, drop W2, test W3."""
    conn, run = db
    promoted_state(conn, run)
    for h in HOURS:
        put(conn, run, 2, h, "W2", 160, 80)
        put(conn, run, 2, h, "W1", 40, 20)
    now = 2 * D + 21 * 60
    (ch,) = experiments.check(ctx(conn, run, now, cfg({"W2": 0.8, "W1": 0.2})))
    assert ch.urgent and ch.attempts == ["move off greeting W2"]
    assert ch.patch["allocations"]["default"]["welcome_message"] == {"W1": 0.5, "W3": 0.5}
    s = state(conn, run)
    assert s["phase"] == "testing" and s["faded"] == ["W2"]
    assert any("stopped working" in r for r in diary(conn, run))


def test_winner_still_ahead_is_kept(db):
    conn, run = db
    promoted_state(conn, run)
    for h in HOURS:
        put(conn, run, 2, h, "W2", 160, 99)
        put(conn, run, 2, h, "W1", 40, 20)
    assert experiments.check(ctx(conn, run, 2 * D + 21 * 60, cfg({"W2": 0.8, "W1": 0.2}))) == []
    assert state(conn, run)["phase"] == "promoted"


def test_supervisor_edit_stops_the_test_for_good(db):
    """E14 (Kiran): their choice stands; we never propose a test on that setting again."""
    conn, run = db
    started(conn, run)
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) "
                 "VALUES (%s,'wf','welcome_message',600,true)", (run.run_id,))
    assert experiments.check(ctx(conn, run, 615, cfg({"W2": 1.0}))) == []      # theirs: no change from us
    assert any("their choice stands for good" in r for r in diary(conn, run))
    for later in (2 * D, 5 * D + 9 * 60 + 15):
        assert experiments.check(ctx(conn, run, later, cfg({"W2": 1.0}))) == []
    assert state(conn, run)["phase"] == "supervisor_owned"


def test_ingest_counts_calls_per_customer_group(db):
    conn, run = db
    lead = json.dumps({"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf", "lead_id": "wf:l1",
                       "features": {"lang": "ta", "age_band": "60+"}, "event_id": 1})
    call = json.dumps({"type": "call", "ts": "2027-01-04T10:05:00+05:30", "workflow_id": "wf", "lead_id": "wf:l1",
                       "cohort_id": "default", "connected": True, "engaged": True, "event_id": 2,
                       "variant": {"welcome_message": "W2", "voice": "V3", "caller_id": "D1", "prompt_version": "P1"},
                       "qa": {"hallucination_flag": False}})
    store.ingest_chunk(conn, run, chunk_meta(0, [lead, call]), [lead, call])
    rows = store.strata_counts(conn, run, "wf", "voice", since=0, segments=True)
    assert sorted((r["feature"], r["value"], r["option"], r["engaged"]) for r in rows) == \
        [("age_band", "60+", "V3", 1), ("lang", "ta", "V3", 1)]


# ------------------------------------------------------------------ review round 1 (R-*)

def faded_w2(conn, run):
    for h in HOURS:
        put(conn, run, 2, h, "W2", 160, 80)
        put(conn, run, 2, h, "W1", 40, 20)


def test_retest_after_a_narrowed_win_fades_is_not_ended_at_once(db):
    """R-H1: the 'narrowed' flag of the old test must not end the new test on its first daily check."""
    conn, run = db
    promoted_state(conn, run, narrowed=True, judged_day=2)
    faded_w2(conn, run)
    experiments.check(ctx(conn, run, 2 * D + 21 * 60, cfg({"W2": 0.8, "W1": 0.2})))
    assert experiments.check(ctx(conn, run, 3 * D + 9 * 60 + 15, cfg({"W1": 0.5, "W3": 0.5}))) == []
    assert state(conn, run)["phase"] == "testing"


def test_harmful_option_is_not_brought_back_by_a_rollback(db):
    """R-M2: W3 was stopped as harmful; when W2 fades, only W1 is left: back to W1, no new test."""
    conn, run = db
    promoted_state(conn, run, harmed=["W3"])
    faded_w2(conn, run)
    chs = experiments.check(ctx(conn, run, 2 * D + 21 * 60, cfg({"W2": 0.8, "W1": 0.2})))
    assert [c.patch["allocations"]["default"]["welcome_message"] for c in chs] == [{"W1": 1.0}]
    assert state(conn, run)["phase"] == "done"


def test_rollback_keeps_carve_outs_and_goes_alone_as_urgent(db):
    """R-M4 + R-H2: the urgent move-off touches only the default split (auto-appliable); the
    group carve-out pinned to W1 stays, so no cohort change needs the approver."""
    conn, run = db
    promoted_state(conn, run, carve={"lang=ta": {"feature": "lang", "value": "ta"}})
    faded_w2(conn, run)
    config = cfg({"W2": 0.8, "W1": 0.2}, cohorts=[{"id": "exp_lang_ta", "where": {"lang": ["ta"]}}],
                 extra={"exp_lang_ta": {"welcome_message": {"W1": 1.0}}})
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 21 * 60, config))
    assert ch.intent_key == "experiments:wf:urgent" and ch.urgent
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 0.5, "W3": 0.5}}}}


def test_supervisor_edit_elsewhere_ends_our_leftover_split_once(db):
    """R-M5: the supervisor changed one cohort's greeting; our 3-way split in default would run on
    unwatched, so it goes back to W1, once, and then we never touch the greeting again."""
    conn, run = db
    started(conn, run)
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) "
                 "VALUES (%s,'wf','welcome_message',600,true)", (run.run_id,))
    (ch,) = experiments.check(ctx(conn, run, 615, cfg(THIRDS)))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 1.0}}}}
    # R2-H1: after one auto step of 0.2 the split no longer looks like ours; the clean-up still finishes
    (ch,) = experiments.check(ctx(conn, run, 630, cfg({"W1": 0.533333, "W2": 0.233333, "W3": 0.233333})))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 1.0}}}}
    assert experiments.check(ctx(conn, run, 2 * D, cfg({"W1": 1.0}))) == []
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) "
                 "VALUES (%s,'wf','welcome_message',%s,true)", (run.run_id, 3 * D))
    assert experiments.check(ctx(conn, run, 3 * D + 15, cfg({"W2": 1.0}))) == []   # theirs later: left alone


def test_ingest_per_group_is_idempotent_and_survives_null_features(db):
    """Rule 4 + R-L3: a re-delivered call never counts twice; a null feature value doesn't block ingest."""
    conn, run = db
    lead = json.dumps({"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf", "lead_id": "wf:l9",
                       "features": {"lang": None}, "event_id": 11})
    call = json.dumps({"type": "call", "ts": "2027-01-04T10:05:00+05:30", "workflow_id": "wf", "lead_id": "wf:l9",
                       "cohort_id": "default", "connected": True, "engaged": False, "event_id": 12,
                       "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"}})
    store.ingest_chunk(conn, run, chunk_meta(0, [lead, call]), [lead, call])
    store.ingest_chunk(conn, run, chunk_meta(1, [call]), [call])                  # re-delivered
    rows = store.strata_counts(conn, run, "wf", "voice", since=0, segments=True)
    assert [(r["value"], r["connected"]) for r in rows] == [("?", 1)]


# ------------------------------------------------------------------ review round 2 (R2-*), through a fake gate

def test_supervisor_cohort_edit_never_touches_carve_outs(db):
    """R2-H2: the supervisor pinned Tamil speakers to W3 in our carve-out cohort; our leftover clean-up
    changes only the default split and leaves their cohort exactly as they set it."""
    conn, run = db
    promoted_state(conn, run, carve={"lang=ta": {"feature": "lang", "value": "ta"}})
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) "
                 "VALUES (%s,'wf','welcome_message',%s,true)", (run.run_id, 2 * D + 600))
    config = cfg({"W2": 0.8, "W1": 0.2}, cohorts=[{"id": "exp_lang_ta", "where": {"lang": ["ta"]}}],
                 extra={"exp_lang_ta": {"welcome_message": {"W3": 1.0}}})
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 615, config))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 1.0}}}}


def test_declined_test_end_keeps_harm_checks_running(db):
    """R2-M1: the approver refused to end the test twice; the test goes on and a harmful option is
    still stopped."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.51, "W3": 0.50}, per_hour=100)
    experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))           # wants to end the test
    gov = FakeGov({("experiments:wf", "end greeting test of W1/W2/W3"): "given_up"})
    assert experiments.check(ctx(conn, run, D + 9 * 60 + 30, cfg(THIRDS), gov=gov)) == []
    assert state(conn, run)["phase"] == "testing"
    fill(conn, run, 1, {"W1": 0.55, "W3": 0.20}, per_hour=100, hours=range(10, 14))
    (ch,) = experiments.check(ctx(conn, run, D + 14 * 60, cfg(THIRDS), gov=gov))
    assert ch.urgent and ch.attempts == ["stop greeting W3"]


def test_safety_move_turned_down_twice_is_asked_again_a_day_later(db):
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.52, "W3": 0.30}, hours=range(9, 14))
    gov = FakeGov({("experiments:wf:urgent", "stop greeting W3"): "given_up"})
    assert experiments.check(ctx(conn, run, 14 * 60, cfg(THIRDS), gov=gov)) == []
    assert experiments.check(ctx(conn, run, 14 * 60 + D - 15, cfg(THIRDS), gov=gov)) == []
    (ch,) = experiments.check(ctx(conn, run, 14 * 60 + D, cfg(THIRDS), gov=gov))
    assert ch.urgent and ch.attempts == ["stop greeting W3 (asked again D1 14:00)"]


def test_winner_that_failed_a_guardrail_is_not_narrowed_onto(db):
    """R2-M2: W2 wins on engagement but fails the pickup guardrail; with enough calls the test ends
    instead of running W2 at 50%."""
    conn, run = db
    started(conn, run)
    for h in HOURS:
        put(conn, run, 0, h, "W1", 100, 50, dials=200)
        put(conn, run, 0, h, "W2", 100, 62, dials=400)                # pickup 25% vs 50%
        put(conn, run, 0, h, "W3", 100, 50, dials=200)
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert ch.attempts == ["end greeting test of W1/W2/W3"]


# ------------------------------------------------------------------ review round 3 (R3-*)

def test_declined_promotion_never_applies_its_carve_outs_later(db):
    """R3-M1: a promotion with a Tamil carve-out is turned down twice; the test ends without ever
    creating the carve-out cohort."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.70, "W3": 0.51}, per_hour=40, table="seg", seg=("lang", "hi"))
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.30, "W3": 0.51}, per_hour=10, table="seg", seg=("lang", "ta"))
    experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))                 # promote + carve-out
    gov = FakeGov({("experiments:wf", "promote greeting W2"): "given_up"})
    experiments.check(ctx(conn, run, D + 9 * 60 + 30, cfg(THIRDS), gov=gov))
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 45, cfg(THIRDS), gov=gov))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W1": 1.0}}}}   # no cohorts


def test_narrowed_leader_that_fails_a_guardrail_ends_the_test(db):
    conn, run = db
    promoted = {"phase": "testing", "control": "W1", "arms": ["W1", "W2"], "stopped": {}, "start": 60, "live": 60,
                "narrowed": True, "target": {"W1": 0.5, "W2": 0.5}, "urgent_off": [], "attempt": "narrow"}
    with conn.transaction():
        store.put_state(conn, run, "experiments:wf", {"dims": {"welcome_message": promoted}}, 60)
    for h in HOURS:
        put(conn, run, 0, h, "W1", 100, 50, dials=200)
        put(conn, run, 0, h, "W2", 100, 62, dials=400)                # wins, but pickup 25% vs 50%
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg({"W1": 0.5, "W2": 0.5})))
    assert ch.attempts == ["end greeting test of W1/W2"] and "failed a guardrail" in ch.rationale


def test_winner_already_fading_is_not_promoted(db):
    """E15 (public_02 W3): W2 won big on day 0 but is behind over the last 24 h, so it isn't promoted."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.70, "W3": 0.50}, per_hour=100)
    fill(conn, run, 1, {"W1": 0.50, "W2": 0.45, "W3": 0.50}, per_hour=50)
    assert experiments.check(ctx(conn, run, 2 * D + 9 * 60 + 15, cfg(THIRDS))) == []   # a day's wait
    assert any("under half of that" in r for r in diary(conn, run))
    assert state(conn, run)["phase"] == "testing"



def test_pending_promotion_is_withdrawn_if_the_winner_stops_leading(db):
    """E15: W2 won on D1 but the promotion is still waiting (turned down once); by D3's morning W2 is
    behind over the last 24 h, so the promotion is withdrawn and the test goes on."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))       # promote W2
    assert ch.attempts == ["promote greeting W2"]
    fill(conn, run, 2, {"W1": 0.55, "W2": 0.45, "W3": 0.51})                        # W2 fading, still not live
    assert experiments.check(ctx(conn, run, 3 * D + 9 * 60 + 15, cfg(THIRDS))) == []  # nothing to change
    st = state(conn, run)
    assert st["phase"] == "testing" and st["champion"] is None
    assert any("the promotion is withdrawn before it goes live" in r for r in diary(conn, run))



def test_winner_whose_recent_lead_is_under_half_is_not_promoted(db):
    """E15 (public_02 run 2): still ahead in the last day, but by under half its claimed lead."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.66, "W3": 0.50}, per_hour=100)
    fill(conn, run, 1, {"W1": 0.50, "W2": 0.53, "W3": 0.50}, per_hour=50)
    assert experiments.check(ctx(conn, run, 2 * D + 9 * 60 + 15, cfg(THIRDS))) == []
    assert any("under half of that" in r for r in diary(conn, run))


def test_fading_leader_is_not_given_half_the_calls(db):
    """E15 (public_02 run 3): no proven winner, the leader W3 is fading: no narrowing to 50/50 today."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.50, "W3": 0.58}, per_hour=100)
    fill(conn, run, 1, {"W1": 0.50, "W2": 0.50, "W3": 0.51}, per_hour=50)
    assert experiments.check(ctx(conn, run, 2 * D + 9 * 60 + 15, cfg(THIRDS))) == []
    assert any("gets no more calls today" in r for r in diary(conn, run))



def test_quiet_day_is_not_read_as_fading(db):
    """R2-M2: no calls in the last 24 h for both sides: no fading verdict, the winner is promoted."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 12 * 60, cfg(THIRDS)))   # D1 had no calls at all
    assert ch.attempts == ["promote greeting W2"]


def test_narrowed_test_with_a_fading_leader_ends_once_it_had_its_calls(db):
    """R3-M: a narrowed leader that may be fading is not kept at 50% once the test had all its calls."""
    conn, run = db
    narrowed = {"phase": "testing", "control": "W1", "arms": ["W1", "W3"], "stopped": {}, "start": 60, "live": 60,
                "narrowed": True, "target": {"W1": 0.5, "W3": 0.5}, "urgent_off": [], "attempt": "narrow"}
    with conn.transaction():
        store.put_state(conn, run, "experiments:wf", {"dims": {"welcome_message": narrowed}}, 60)
    for d in range(0, 4):
        fill(conn, run, d, {"W1": 0.50, "W3": 0.55 if d < 3 else 0.51}, per_hour=150)
    (ch,) = experiments.check(ctx(conn, run, 4 * D + 9 * 60 + 15, cfg({"W1": 0.5, "W3": 0.5})))
    assert ch.attempts == ["end greeting test of W1/W3"] and "may be fading" in ch.rationale



def _step1_state(conn, run, live):
    st = {"phase": "promoted", "control": "W1", "champion": "W2", "arms": ["W1", "W2", "W3"], "stopped": {},
          "start": 60, "live": live, "target": {"W2": 0.5, "W1": 0.5}, "carve": {}, "urgent_off": [],
          "promo": {"diff": 0.12, "se": 0.029}, "promo_step": 1, "promoted": live - 120,
          "attempt": "promote greeting W2", "summary": "", "rationale": "", "dry_run": {}, "evidence": {},
          "impact": 0.1, "test_split": THIRDS}
    with conn.transaction():
        store.put_state(conn, run, "experiments:wf", {"dims": {"welcome_message": st}}, live)


def test_second_step_goes_ahead_when_the_winner_still_leads(db):
    """E16: a day on half the calls, still well ahead: winner to 80%, W1 keeps 20%."""
    conn, run = db
    _step1_state(conn, run, D + 11 * 60)
    fill(conn, run, 1, {"W1": 0.50, "W2": 0.61}, hours=range(12, 21), per_hour=60)
    fill(conn, run, 2, {"W1": 0.50, "W2": 0.61}, hours=range(9, 10), per_hour=60)
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 10 * 60, cfg({"W2": 0.5, "W1": 0.5})))
    assert ch.patch == {"allocations": {"default": {"welcome_message": {"W2": 0.8, "W1": 0.2}}}}
    assert ch.attempts == ["promote greeting W2 step 2"] and "step 2 of 2" in ch.summary


def test_second_step_rolls_back_a_winner_that_fades_on_half_the_calls(db):
    """E16: on half the calls its lead shrank under half of what it was promoted on: rolled back at 50%."""
    conn, run = db
    _step1_state(conn, run, D + 11 * 60)
    fill(conn, run, 1, {"W1": 0.50, "W2": 0.52}, hours=range(12, 21), per_hour=60)
    (ch,) = experiments.check(ctx(conn, run, 2 * D + 10 * 60, cfg({"W2": 0.5, "W1": 0.5})))
    assert ch.urgent and ch.attempts == ["move off greeting W2"]
    assert state(conn, run)["faded"] == ["W2"]
    assert any("before it got the main share" in r for r in diary(conn, run))



class _Gov:
    def __init__(self, blocked):
        self.blocked = blocked

    def attempt_blocked(self, intent, attempt, now):
        return self.blocked.get(attempt)

    def held_dims(self, wid):
        return {}


def test_turned_down_promotion_needs_to_win_again_on_new_calls(db):
    """E17: W2's promotion was turned down; the test goes on, and the old evidence isn't re-sent. Two days
    later W2 leads on the new calls only by a little, so it is not proposed again."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))          # promote W2, step 1
    assert ch.attempts == ["promote greeting W2"]
    gov = _Gov({"promote greeting W2": "cooling_down"})
    assert experiments.check(ctx(conn, run, D + 12 * 60, cfg(THIRDS), gov=gov)) == []   # back to testing
    st = state(conn, run)
    assert st["phase"] == "testing" and st["evidence_since"] == D + 9 * 60 + 15
    fill(conn, run, 2, {"W1": 0.50, "W2": 0.54, "W3": 0.51})                          # small lead on new calls
    assert experiments.check(ctx(conn, run, 3 * D + 9 * 60 + 15, cfg(THIRDS), gov=_Gov({}))) == []
    assert any("since the approver turned its promotion down" in r for r in diary(conn, run))
    assert any("waits for" in r and "new calls per option" in r for r in diary(conn, run))   # R4-M3: no early end
    assert state(conn, run)["phase"] == "testing"



def test_turned_down_promotion_drops_its_carve_outs(db):
    """R4-H1: a promotion with a Tamil carve-out is turned down: back to testing without the carve-out,
    so nothing of the declined promotion goes out under another name."""
    conn, run = db
    started(conn, run)
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.62, "W3": 0.51})
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.70, "W3": 0.51}, per_hour=40, table="seg", seg=("lang", "hi"))
    fill(conn, run, 0, {"W1": 0.50, "W2": 0.30, "W3": 0.51}, per_hour=10, table="seg", seg=("lang", "ta"))
    (ch,) = experiments.check(ctx(conn, run, D + 9 * 60 + 15, cfg(THIRDS)))
    assert "cohorts" in ch.patch
    assert experiments.check(ctx(conn, run, D + 12 * 60, cfg(THIRDS),
                                 gov=_Gov({"promote greeting W2": "cooling_down"}))) == []
    assert state(conn, run)["carve"] == {}


def test_a_new_test_forgets_the_old_fresh_evidence_window(db):
    """R4-L: a rollback starts a new test; the earlier 'turned down' window no longer applies."""
    conn, run = db
    promoted_state(conn, run, evidence_since=100)
    faded_w2(conn, run)
    experiments.check(ctx(conn, run, 2 * D + 21 * 60, cfg({"W2": 0.8, "W1": 0.2})))
    assert "evidence_since" not in state(conn, run)
