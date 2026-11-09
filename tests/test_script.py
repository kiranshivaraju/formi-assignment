"""Script health (C8, H1-H10) on a scripted scoreboard, with a fake sim for the paid calls.

Numbers mirror scenario 02: Kestrel ran P1 at about 1.5% made-up claims; a supervisor switched to P2
at D3 11:30, which made things up on about 8% of answered calls.
"""
import json

import pytest

from controller import settings, store
from controller.features import Context, script
from tests.test_controller_store import STATE, TEST_DSN, URL, test_db  # noqa: F401

D = 1440
KS = {"governance": {"min_validation_pass_rate": 0.97, "auto_apply": {"enabled": True, "allowed_dims": ["voice"]}},
      "options": {"prompt_version": ["P1", "P2", "P3", "P4", "P5"]}}


class FakeSim:
    """Answers the two paid endpoints like the sim does, and counts what we spent."""

    def __init__(self, ideas=("P2", "P5", "P4"), pass_rates=None, refuse=()):
        self.ideas, self.calls, self.refuse = list(ideas), [], set(refuse)
        self.pass_rates = pass_rates or {"P1": 0.99, "P3": 0.988, "P4": 0.975, "P5": 0.997, "P2": 0.94}
        self.credits = 200.0

    def send(self, method, path, body, retry=True):
        assert retry is False, "paid calls must never be retried automatically"
        self.calls.append((path, body))
        if body.get("prompt_version") in self.refuse or path in self.refuse:
            return 402, {"error": "insufficient_credits"}
        if path == "/llm/suggest-fixes":
            self.credits -= 3
            return 200, {"suggestions": [{"prompt_version": p, "summary": f"idea {p}"} for p in self.ideas],
                         "credits_remaining": self.credits}
        if path == "/validation/runs":
            self.credits -= -(-body["n"] // 100)
            return 200, {"validation_run_id": f"val_{len(self.calls)}", "prompt_version": body["prompt_version"],
                         "n": body["n"], "pass_rate": self.pass_rates[body["prompt_version"]],
                         "credits_remaining": self.credits}
        raise AssertionError(path)


@pytest.fixture
def db(request):
    with store.connect(TEST_DSN) as conn:
        run, _ = store.resolve_run(conn, URL, {**STATE, "scenario": request.node.name})
        yield conn, run


def put(conn, run, day, hour, pv, connected, halluc, engaged=None, wid="ks"):
    engaged = round(connected * 0.5) if engaged is None else engaged
    conn.execute("INSERT INTO call_stats VALUES (%s,%s,%s,%s,'default','W1','V1','D1',%s,%s,%s,%s,%s) "
                 "ON CONFLICT (run_id, workflow_id, sim_day, hour, cohort_id, welcome_message, voice, caller_id, "
                 "prompt_version) DO UPDATE SET dials=EXCLUDED.dials, connected=EXCLUDED.connected, "
                 "engaged=EXCLUDED.engaged, hallucinations=EXCLUDED.hallucinations",
                 (run.run_id, wid, day, hour, pv, connected * 4, connected, engaged, halluc))


def switch(conn, run, minute, eid=1, wid="ks"):
    ts = run.d0.timestamp() + minute * 60
    body = {"type": "config_changed", "workflow_id": wid, "changed_dims": ["prompt_version"], "actor": "supervisor",
            "event_id": eid}
    conn.execute("INSERT INTO events VALUES (%s,%s,'config_changed',%s,to_timestamp(%s),%s)",
                 (run.run_id, eid, wid, ts, json.dumps(body)))


def history(conn, run, days=range(-14, 3), pv="P1"):
    for day in days:
        for h in range(9, 21):
            put(conn, run, day, h, pv, 100, 1 if h % 2 else 2)   # about 1.5% made up


def ctx(conn, run, now, pv, sim, credits=200.0):
    c = Context(conn, run, sim=sim, gov=None, settings=settings.load(), workflows={"ks": KS}, now=now,
                sim_state={"credits_remaining": credits})
    c._configs = {"ks": {"version": 3, "config": {"prompt_version": pv}}}
    return c


def diary(conn, run):
    return [(r["action"], r["reason"]) for r in conn.execute(
        "SELECT action, reason FROM decisions WHERE run_id=%s ORDER BY decision_id", (run.run_id,)).fetchall()]


def test_bad_script_goes_back_to_proven_one_and_ai_ideas_are_only_reported(db):
    conn, run = db
    history(conn, run)
    put(conn, run, 3, 9, "P1", 90, 1)
    put(conn, run, 3, 10, "P1", 90, 2)
    switch(conn, run, 3 * D + 11 * 60 + 30)
    for h, (n, x) in zip((11, 12, 13), ((50, 5), (90, 7), (95, 8))):
        put(conn, run, 3, h, "P2", n, x)
    sim = FakeSim()
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", sim))
    assert ch.urgent and ch.patch == {"prompt_version": "P1"} and ch.attempts == ["script P1"]
    assert ch.summary.startswith("Switch script back from P2 to proven P1")
    assert ch.evidence["validation_pass_rate"] == 0.99 and ch.evidence["validation_run_id"]
    # Kiran's rule: AI asked once, first 2 NEW ideas tested (P2 is the bad one, so P5 and P4)
    paths = [(p, b.get("prompt_version")) for p, b in sim.calls]
    assert paths == [("/validation/runs", "P1"), ("/llm/suggest-fixes", None),
                     ("/validation/runs", "P5"), ("/validation/runs", "P4")]
    assert ch.evidence["credits_spent"] == 3 + 3 + 3 + 3
    assert "Other options checked" in ch.rationale and "P5 99.7% clean" in ch.rationale
    actions = [a for a, _ in diary(conn, run)]
    assert actions[0] == "incident" and actions.count("test_drive") == 3 and "ai_ideas" in actions
    # next step: nothing paid again, same change
    sim.calls.clear()
    (again,) = script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", sim))
    assert sim.calls == [] and again.patch == ch.patch


def test_how_many_ai_ideas_to_test_is_a_knob(db, tmp_path):
    conn, run = db
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    f = tmp_path / "s.yaml"
    f.write_text(settings.DEFAULT_PATH.read_text().replace("ai_ideas_to_test: 2", "ai_ideas_to_test: 1"))
    c = ctx(conn, run, 3 * D + 14 * 60, "P2", FakeSim())
    c.settings = settings.load(f)
    script.check(c)
    assert [b.get("prompt_version") for p, b in c.sim.calls if p == "/validation/runs"] == ["P1", "P5"]


def test_normal_wobble_is_not_an_incident(db):
    conn, run = db
    history(conn, run, days=range(-14, 4))
    put(conn, run, 3, 21 - 1, "P1", 100, 3)   # a 3% hour: within normal wobble
    assert script.check(ctx(conn, run, 3 * D + 21 * 60, "P1", FakeSim())) == []
    assert diary(conn, run) == []


def test_no_proven_script_uses_the_cleanest_ai_idea(db):
    """H5: a long-standing script drifting, nothing else ever ran: the AI's best tested idea is the fix."""
    conn, run = db
    history(conn, run, days=range(-14, 3))
    for h in range(9, 21):
        put(conn, run, 3, h, "P1", 100, 9)                 # P1 itself makes things up on 9% all day
    sim = FakeSim(ideas=("P3", "P5", "P4"))
    (ch,) = script.check(ctx(conn, run, 3 * D + 21 * 60, "P1", sim))
    assert ch.patch == {"prompt_version": "P5"}           # cleanest of the two tested (P3, P5)
    assert "engagement unknown" in ch.summary
    script.check(ctx(conn, run, 3 * D + 21 * 60 + 15, "P1", sim))
    tested = [b["prompt_version"] for p, b in sim.calls if p == "/validation/runs"]
    assert tested == ["P3", "P5"]                          # Kiran's 2, even after P5 became the fix


def test_a_bad_three_hours_is_not_a_drift(db):
    """Found on scenario 01: 13 made-up in 257 calls over 3 h (normal predicts ~4) raised an alarm.
    Luck, when you look every 15 minutes for days. A drift is judged on a whole day."""
    conn, run = db
    history(conn, run, days=range(-14, 3))
    for h in range(9, 21):
        put(conn, run, 2, h, "P1", 85, 4 if h in (13, 14, 15) else 1)
    assert script.check(ctx(conn, run, 2 * D + 16 * 60, "P1", FakeSim())) == []
    assert script.check(ctx(conn, run, 2 * D + 21 * 60, "P1", FakeSim())) == []


def test_verify_says_when_the_new_script_is_still_worse_than_normal(db):
    conn, run = db
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", FakeSim()))
    switch(conn, run, 3 * D + 15 * 60, eid=2)
    script.check(ctx(conn, run, 3 * D + 15 * 60, "P1", FakeSim()))
    for h in range(16, 22):
        put(conn, run, 3, h, "P1", 100, 4)                 # 4%: better than 8.9%, worse than 1.5%
    script.check(ctx(conn, run, 3 * D + 22 * 60, "P1", FakeSim()))
    action, reason = diary(conn, run)[-1]
    assert action == "not_back_to_normal" and "above normal" in reason


def test_credit_budget_and_reserve(db):
    """H7: the fix's own test drive may use the reserve; optional AI ideas may not."""
    conn, run = db
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    store.put_state(conn, run, "script_health:credits_start", {"credits": 200}, 0)
    sim = FakeSim()
    sim.credits = 41
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", sim, credits=41))  # reserve is 40
    assert [p for p, _ in sim.calls] == ["/validation/runs"]                       # only the essential one
    assert ch.patch == {"prompt_version": "P1"}
    assert any(a == "ai_skipped" and "reserve" in r for a, r in diary(conn, run))


def test_fixed_then_verified_then_closed(db):
    conn, run = db
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", FakeSim()))
    switch(conn, run, 3 * D + 15 * 60, eid=2)               # our change applied at 15:00
    assert script.check(ctx(conn, run, 3 * D + 15 * 60, "P1", FakeSim())) == []
    for h in (16, 17):
        put(conn, run, 3, h, "P1", 100, 2)
    script.check(ctx(conn, run, 3 * D + 18 * 60, "P1", FakeSim()))
    assert diary(conn, run)[-1][0] == "fixed"                 # 200 calls: too few to judge yet
    assert script.check(ctx(conn, run, 3 * D + 19 * 60, "P1", FakeSim())) == []
    for h in (18, 19, 20):
        put(conn, run, 3, h, "P1", 100, 2)
    script.check(ctx(conn, run, 3 * D + 21 * 60, "P1", FakeSim()))
    action, reason = diary(conn, run)[-1]
    assert action == "verified" and reason.startswith("after switching to P1: made-up claims 2.0% on 500")
    assert store.get_state(conn, run, "script_health:ks")["incident"] is None
    assert list(store.get_state(conn, run, "script_health:ks")["bad_scripts"]) == ["P2"]
    # P1 is now compared with its own earlier use, never with P2 (once flagged, never a reference): no alarm
    assert script.check(ctx(conn, run, 3 * D + 21 * 60 + 15, "P1", FakeSim())) == []


def test_someone_else_fixing_it_closes_our_incident(db):
    conn, run = db
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", FakeSim()))
    assert script.check(ctx(conn, run, 3 * D + 15 * 60, "P3", FakeSim())) == []   # supervisor chose P3
    assert diary(conn, run)[-1][0] == "forget"



def incident_at_14(conn, run, sim, **kw):
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    return script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", sim, **kw))


def test_proven_script_needs_only_the_approvers_bar(db):
    """Review HIGH 1: 98.3% on 300 is above the 97% bar but under our margin. Real calls already
    proved P1, so it is still the fix (before: skipped, and an AI idea proposed instead)."""
    conn, run = db
    (ch,) = incident_at_14(conn, run, FakeSim(pass_rates={"P1": 0.983, "P5": 0.997, "P4": 0.975}))
    assert ch.patch == {"prompt_version": "P1"}


def test_fix_replaced_before_verify_is_not_blamed_and_new_script_is_judged(db):
    """Review HIGH 2: our fix P1 goes live, a supervisor switches back to P2 an hour later."""
    conn, run = db
    incident_at_14(conn, run, FakeSim())
    switch(conn, run, 3 * D + 15 * 60, eid=2)
    script.check(ctx(conn, run, 3 * D + 15 * 60, "P1", FakeSim()))
    switch(conn, run, 3 * D + 16 * 60, eid=3)
    for h in (16, 17):
        put(conn, run, 3, h, "P2", 90, 8)
    (ch,) = script.check(ctx(conn, run, 3 * D + 18 * 60, "P2", FakeSim()))
    actions = [a for a, _ in diary(conn, run)]
    assert "superseded" in actions and actions.count("incident") == 2
    assert not any(a in ("verified", "not_improved") for a in actions)
    assert ch.patch == {"prompt_version": "P1"}


def test_turned_down_fix_moves_to_the_next_option(db):
    """Review MEDIUM: the approver said no to P1; don't wait a day while a passing option exists."""
    conn, run = db
    sim = FakeSim()
    incident_at_14(conn, run, sim)

    class Gov:
        def attempt_blocked(self, intent, attempt, now):
            return "cooling_down" if attempt == "script P1" else None

    c = ctx(conn, run, 3 * D + 15 * 60, "P2", sim)
    c.gov = Gov()
    (ch,) = script.check(c)
    assert ch.patch == {"prompt_version": "P5"} and "AI idea" in ch.summary
    assert any(a == "next_option" for a, _ in diary(conn, run))


def test_refused_paid_call_is_not_charged_and_retried(db):
    conn, run = db
    sim = FakeSim(refuse={"P1"})
    assert incident_at_14(conn, run, sim) == []        # the proven fix must be retried, not an AI idea used
    inc = store.get_state(conn, run, "script_health:ks")["incident"]
    assert inc["errors"]["P1"] == 1 and "P1" not in inc["skipped"] and inc["target"] is None
    assert inc["spent"] == 3 + 3 + 3                    # AI + 2 ideas; the refused test drive cost nothing
    sim.refuse.clear()
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", sim))
    assert ch.patch == {"prompt_version": "P1"}


def test_lost_paid_answer_stays_counted(db):
    """Review MEDIUM: an error after the sim charged us must not let us pay again unnoticed."""
    conn, run = db

    class Lost(FakeSim):
        def send(self, method, path, body, retry=True):
            raise RuntimeError("answer lost after the sim charged")

    assert incident_at_14(conn, run, Lost()) == []     # the error is caught per workflow
    inc = store.get_state(conn, run, "script_health:ks")["incident"]
    assert inc["spent"] == 3 and inc["pending"] == {"P1": 3}
    script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", FakeSim()))
    inc = store.get_state(conn, run, "script_health:ks")["incident"]
    assert any(a == "paid_lost" for a, _ in diary(conn, run))
    assert inc["spent"] == 3 + 12 and inc["pending"] == {}


def test_no_ai_question_when_no_ideas_would_be_tested(db, tmp_path):
    conn, run = db
    f = tmp_path / "s.yaml"
    f.write_text(settings.DEFAULT_PATH.read_text().replace("ai_ideas_to_test: 2", "ai_ideas_to_test: 0"))
    sim = FakeSim()
    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    c = ctx(conn, run, 3 * D + 14 * 60, "P2", sim)
    c.settings = settings.load(f)
    script.check(c)
    assert [p for p, _ in sim.calls] == ["/validation/runs"]



def test_refused_twice_proven_moves_to_the_next_proven(db):
    """Review round 2: P1 refused twice must not block P3, also proven on real calls."""
    conn, run = db
    history(conn, run)
    history(conn, run, days=range(-14, -7), pv="P3")
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    sim = FakeSim(refuse={"P1"})
    script.check(ctx(conn, run, 3 * D + 14 * 60, "P2", sim))
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", sim))
    assert ch.patch == {"prompt_version": "P3"} and "proven" in ch.summary


def test_waiting_proven_fix_does_not_block_a_passed_idea_when_retry_is_impossible(db):
    """Review round 2: P1 refused once, then no credits to retry: use the AI idea that already passed."""
    conn, run = db
    sim = FakeSim(refuse={"P1"})
    assert incident_at_14(conn, run, sim) == []
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", sim, credits=2))
    assert ch.patch == {"prompt_version": "P5"}


def test_turned_down_candidate_is_not_paid_for(db):
    conn, run = db

    class Gov:
        def attempt_blocked(self, intent, attempt, now):
            return "given_up" if attempt == "script P1" else None

    history(conn, run)
    switch(conn, run, 3 * D + 11 * 60)
    for h in (11, 12, 13):
        put(conn, run, 3, h, "P2", 90, 8)
    sim = FakeSim()
    c = ctx(conn, run, 3 * D + 14 * 60, "P2", sim)
    c.gov = Gov()
    script.check(c)
    assert ("/validation/runs", "P1") not in [(p, b.get("prompt_version")) for p, b in sim.calls]



def test_ai_question_skipped_for_the_reserve_is_asked_once_it_is_needed(db):
    """Review round 3: P1 refused while the wallet sits on the reserve, so the optional AI question is
    skipped; P1 is then refused again. Now the AI is the only way to a fix and must be asked."""
    conn, run = db
    store.put_state(conn, run, "script_health:credits_start", {"credits": 200}, 0)
    sim = FakeSim(refuse={"P1"})
    sim.credits = 41
    assert incident_at_14(conn, run, sim, credits=41) == []
    assert not any(p == "/llm/suggest-fixes" for p, _ in sim.calls)
    (ch,) = script.check(ctx(conn, run, 3 * D + 14 * 60 + 15, "P2", sim, credits=41))
    assert any(p == "/llm/suggest-fixes" for p, _ in sim.calls)
    assert ch.patch == {"prompt_version": "P5"}
    assert any(a == "candidate_skipped" for a, _ in diary(conn, run))
