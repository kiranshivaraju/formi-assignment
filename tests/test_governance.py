"""Governance client (C9) against a real, fresh simulator per test (G22) and real Postgres.

Each test starts `python -m sim serve` on its own port in a temp run dir, so proposals here never
touch a run you care about.
"""
import itertools
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from controller import config_ops, governance, store
from controller.client import SimClient
from controller.governance import Change, Governor
from controller.main import ingest_pending
from tests.test_controller_store import TEST_DSN, test_db  # noqa: F401  (recreates formi_test)

ROOT = Path(__file__).resolve().parents[1]
D = 1440


# ------------------------------------------------------------------ pure rules

def test_urgent_timing_rule():
    """G7 with Kestrel's numbers: approver 10-19, average answer 70 min, 30 min buffer."""
    hours, avg = [10, 19], 70
    assert governance.urgent_can_send_now(16 * 60 + 30, hours, avg)          # 17:40, 80 min to spare
    assert governance.urgent_can_send_now(17 * 60 + 15, hours, avg)          # 18:25, 35 min
    assert not governance.urgent_can_send_now(17 * 60 + 30, hours, avg)      # 18:40, only 20 min
    assert not governance.urgent_can_send_now(9 * 60 + 45, hours, avg)       # approver not in yet
    assert not governance.urgent_can_send_now(21 * 60, hours, avg)           # gone home
    assert not governance.urgent_can_send_now(D + 14 * 60, [10, 18], 270)    # KYC: 18:30 > 17:30
    assert governance.urgent_can_send_now(D + 12 * 60, [10, 18], 270)        # KYC: 16:30 ok


def test_slot_reservation():
    assert governance.slots_free(4, 2, urgent=False)
    assert not governance.slots_free(4, 3, urgent=False)  # last slot kept for emergencies
    assert governance.slots_free(4, 3, urgent=True)
    assert not governance.slots_free(4, 4, urgent=True)
    assert governance.slots_free(1, 0, urgent=False)      # nothing to reserve with a limit of 1


def test_config_ops_shift_and_dims():
    base = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
            "caller_id": {"D1": 0.5, "D2": 0.5}}}, "prompt_version": "P1",
            "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    new = config_ops.apply_patch(base, {"allocations": {"default": {"caller_id": {"D1": 0.7, "D2": 0.3}}}})
    assert new["allocations"]["default"]["voice"] == {"V1": 1.0}            # untouched dims kept
    assert config_ops.changed_dims(base, new) == ["caller_id"]
    assert config_ops.max_shift(base, new) == pytest.approx(0.2)
    cohort = config_ops.apply_patch(base, {"cohorts": [{"id": "old", "where": {"age_band": ["60+"]}}],
                                           "allocations": {"old": {"voice": {"V1": 0.5, "V3": 0.5}}}})
    assert config_ops.changed_dims(base, cohort) == ["cohorts", "voice"]
    assert config_ops.max_shift(base, cohort) == pytest.approx(0.5)        # the new cohort's voice moved
    assert config_ops.patch_dims({"prompt_version": "P3", "allocations": {"x": {"voice": {}}}}) == \
        ["voice", "prompt_version"]


# ------------------------------------------------------------------ real simulator

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class World:
    """A fresh sim + controller state, advanced the same way the real loop does."""

    def __init__(self, scenario: str, tmp: Path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", scenario, "--run-dir",
                                      str(tmp / "run"), "--port", str(self.port)], cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(240):
            try:
                if httpx.get(f"{self.url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.25)
        self.sim = SimClient(self.url)
        self.conn = store.connect(TEST_DSN)
        self.run, _ = store.resolve_run(self.conn, self.url, self.sim.state())
        ingest_pending(self.sim, self.conn, self.run, ("history", "events"))
        self.gov = Governor(self.sim, self.conn, self.run, self.sim.workflows())

    @property
    def now(self) -> int:
        return self.sim.state()["sim_minute"]

    def step(self, until: int, stop=None, stop_before_tick=None) -> None:
        """Advance tick by tick to `until`, running ingest + governor like the real loop.
        `stop_before_tick` is checked after ingest, before the governor acts on what arrived."""
        while self.now < until:
            self.sim.advance_to(self.now + 15)
            ingest_pending(self.sim, self.conn, self.run, ("events",))
            store.mark_covered(self.conn, self.run, self.now)
            if stop_before_tick and stop_before_tick():
                return
            self.gov.tick(self.now)
            if stop and stop():
                return

    def proposals(self, wid=None):
        return self.conn.execute("SELECT * FROM proposals WHERE run_id=%s AND (%s::text IS NULL OR workflow_id=%s) "
                                 "ORDER BY sent_minute", (self.run.run_id, wid, wid)).fetchall()

    def diary(self, kind):
        return self.conn.execute("SELECT sim_ts, workflow_id, action, reason FROM decisions WHERE run_id=%s "
                                 "AND kind=%s ORDER BY decision_id", (self.run.run_id, kind)).fetchall()

    def close(self):
        self.conn.close()
        self.proc.terminate()
        self.proc.wait()


@pytest.fixture
def world01(tmp_path):
    w = World("scenarios/public_01_dev.yaml", tmp_path)
    yield w
    w.close()


@pytest.fixture
def world02(tmp_path):
    w = World("scenarios/public_02_incidents.yaml", tmp_path)
    yield w
    w.close()


_n = itertools.count()


def change(wid, patch, *, urgent=False, key=None):
    return Change(intent_key=key or f"test:{wid}:{next(_n)}", workflow_id=wid, urgent=urgent, patch=patch,
                  summary=f"test change on {wid}", rationale="integration test", affected_leads=100,
                  impact_score=0.3, dry_run={"note": "test"}, evidence={"n": 1})


def test_auto_apply_within_policy(world01):
    w = world01
    assert w.gov.submit(change("nw_renewal", {"allocations": {"default": {"caller_id": {"D1": 0.7, "D2": 0.3}}}}),
                        w.now) == "queued"
    w.gov.tick(w.now)
    cfg = w.sim.config("nw_renewal")
    assert (cfg["version"], cfg["actor"]) == (2, "controller_auto")
    (p,) = w.proposals("nw_renewal")
    assert (p["path"], p["status"], p["applied_version"]) == ("auto", "applied", 2)
    assert w.diary("change_applied")[0]["reason"].endswith("auto-applied within policy)")


def test_big_self_change_is_applied_in_hourly_steps(world01):
    """T3: ks_cancel allows voice auto-apply with max_shift 0.2. A 0.5 move is never refused (403) and
    never sent to the approver: it goes in steps of 0.2, 0.2, 0.1, one hour apart."""
    w = world01
    w.gov.submit(change("ks_cancel", {"allocations": {"default": {"voice": {"V1": 0.5, "V3": 0.5}}}}), w.now)
    w.gov.tick(w.now)
    w.step(4 * 60)
    ps = w.proposals("ks_cancel")
    assert [(p["path"], p["status"], p["sent_minute"], p["partial"]) for p in ps] == [
        ("auto", "applied", 0, True), ("auto", "applied", 60, True), ("auto", "applied", 120, False)]
    voices = [p["request"]["proposed_config"]["allocations"]["default"]["voice"] for p in ps]
    assert [h["proposal_id"] for h in w.sim.config_history("ks_cancel")[1:]] == [p["proposal_id"] for p in ps]
    # step 2 is 0.600001 / 0.399999: a hair under the limit, since 0.8 - 0.6 is 0.20000000000000007 in floats
    assert [v["V1"] for v in voices] == pytest.approx([0.8, 0.6, 0.5], abs=1e-5)
    reasons = [d["reason"] for d in w.diary("change_applied")]
    assert reasons[0].startswith("step 1 of 3") and "next step not before D0 01:00" in reasons[0]
    assert reasons[2].startswith("final step 3 of 3")
    assert w.conn.execute("SELECT status FROM changes WHERE run_id=%s", (w.run.run_id,)).fetchone()["status"] == "applied"


def test_change_nobody_asks_for_any_more_is_dropped(world01):
    """F3: a change whose feature stops re-submitting it is dropped after its ttl."""
    w = world01
    w.step(60)
    c = change("nw_kyc", {"allocations": {"default": {"voice": {"V2": 1.0}}}})
    c.ttl_min = 60
    w.gov.submit(c, w.now)        # 01:00, waits for the 10:00 morning note
    w.step(2 * 60)
    assert w.diary("change_dropped") == []
    w.step(2 * 60 + 15)           # 02:15: not refreshed for 75 min
    (d,) = w.diary("change_dropped")
    assert "no longer wanted" in d["reason"]
    w.step(10 * 60)
    assert w.proposals("nw_kyc") == []


def test_normal_change_waits_for_morning_then_applies(world01):
    w = world01
    w.step(60)
    w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}}), w.now)
    w.step(9 * 60 + 45)
    assert w.proposals("nw_renewal") == []  # 09:45: still waiting for the approver's day
    w.step(D, stop=lambda: any(p["status"] not in ("sending", "pending") for p in w.proposals("nw_renewal")))
    w.step(w.now + 15)                      # one more tick to apply an approval
    (p,) = w.proposals("nw_renewal")
    assert p["sent_minute"] == 600
    if p["status"] == "applied":            # the approver may also say no (base_reject_rate)
        cfg = w.sim.config("nw_renewal")
        assert cfg["actor"] == "controller_approved" and cfg["version"] == p["applied_version"]
        assert w.diary("approval_decided")[0]["action"] == "approved"
    else:
        assert p["status"] == "rejected" and w.diary("approval_decided")[0]["action"] == "rejected"


def test_urgent_goes_now_if_answer_fits_else_next_morning_tagged(world01):
    w = world01
    w.step(11 * 60)  # nw_kyc: 10-18, average 270 min -> 15:30 fits
    w.gov.submit(change("nw_kyc", {"allocations": {"default": {"voice": {"V2": 1.0}}}}, urgent=True), w.now)
    w.gov.tick(w.now)
    (p,) = w.proposals("nw_kyc")
    assert p["sent_minute"] == 11 * 60 and p["request"]["change_summary"].startswith("[URGENT]")

    w.step(14 * 60)  # ks_cancel at 14:00 fits; nw_renewal urgent at 17:00 + 135 = 19:15 does not
    w.step(17 * 60)
    w.gov.submit(change("nw_renewal", {"allocations": {"default": {"welcome_message": {"W2": 1.0}}}},
                        urgent=True), w.now)
    w.gov.tick(w.now)
    assert w.proposals("nw_renewal") == []
    w.step(D + 10 * 60)
    (p,) = w.proposals("nw_renewal")
    assert p["sent_minute"] == D + 600 and p["request"]["change_summary"].startswith("[URGENT]")


def test_never_sends_a_note_certain_to_fail(world01):
    w = world01
    w.gov.submit(change("nw_renewal", {"prompt_version": "P3"}), w.now)  # no validation evidence
    w.step(10 * 60)
    assert w.proposals("nw_renewal") == []
    assert "without a validation run" in w.diary("change_dropped")[0]["reason"]


def test_crash_before_and_after_send_never_duplicates(world01, monkeypatch):
    w = world01
    w.step(10 * 60 - 15)
    w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V3": 1.0}}}}), w.now)
    w.gov.submit(change("nw_kyc", {"allocations": {"default": {"voice": {"V2": 1.0}}}}), w.now)
    real_send = w.sim.send

    def lost_response(method, path, body):  # nw_renewal: request arrives, response is lost
        out = real_send(method, path, body)
        if body and body.get("workflow_id") == "nw_renewal":
            raise KeyboardInterrupt("killed after POST")
        return out

    def never_sent(method, path, body):     # nw_kyc: killed before the request leaves
        if body and body.get("workflow_id") == "nw_kyc":
            raise KeyboardInterrupt("killed before POST")
        return lost_response(method, path, body)

    monkeypatch.setattr(w.sim, "send", never_sent)
    w.sim.advance_to(10 * 60)
    ingest_pending(w.sim, w.conn, w.run, ("events",))
    for wid in ("nw_renewal", "nw_kyc"):
        with pytest.raises(KeyboardInterrupt):
            w.gov._approval_step(wid, w.now, w.gov._queued(wid))
    assert {p["workflow_id"]: p["status"] for p in w.proposals()} == {"nw_renewal": "sending",
                                                                       "nw_kyc": "sending"}
    monkeypatch.setattr(w.sim, "send", real_send)
    Governor(w.sim, w.conn, w.run, w.sim.workflows()).reconcile(w.now)  # the restart
    mine = {p["workflow_id"]: p for p in w.proposals()}
    assert all(p["status"] == "pending" and p["approval_id"] for p in mine.values())
    sims = [a for wid in mine for a in w.sim.approvals(wid)]
    assert sorted(a["proposal_id"] for a in sims) == sorted(p["proposal_id"] for p in mine.values())


VOICES = [{"V1": 0.5, "V4": 0.5}, {"V2": 1.0}, {"V3": 1.0}, {"V1": 0.5, "V2": 0.5}, {"V4": 1.0}]


def first_approval(w, wid, key_prefix):
    """Propose until the approver says yes (it may judge-reject any one), stopping before we apply.
    Leaves the approved proposal unapplied and returns it."""
    for i, voice in enumerate(VOICES):
        w.gov.submit(change(wid, {"allocations": {"default": {"voice": voice}}}, urgent=True,
                            key=f"{key_prefix}-{i}"), w.now)
        n = len(w.proposals(wid))

        def decided():
            ps = w.proposals(wid)
            return len(ps) > n and ps[-1]["status"] not in ("sending", "pending")
        w.step(w.now + 2 * D, stop_before_tick=decided)
        last = w.proposals(wid)[-1]
        if last["status"] == "approved":
            return last
        w.gov.tick(w.now)  # record the rejection, then try another
    pytest.fail("approver rejected every attempt")


def test_crash_mid_apply_never_applies_twice(world01, monkeypatch):
    w = world01
    approved = first_approval(w, "nw_renewal", "mid-apply")
    real_send = w.sim.send

    def killed_after_apply(method, path, body):
        out = real_send(method, path, body)
        if path.endswith("/apply"):
            raise KeyboardInterrupt("killed after apply")
        return out

    monkeypatch.setattr(w.sim, "send", killed_after_apply)
    with pytest.raises(KeyboardInterrupt):
        w.gov._apply_approved(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "applying"
    monkeypatch.setattr(w.sim, "send", real_send)
    g = Governor(w.sim, w.conn, w.run, w.sim.workflows())
    g.reconcile(w.now)
    g.tick(w.now)
    hist = w.sim.config_history("nw_renewal")
    assert [h["proposal_id"] for h in hist].count(approved["proposal_id"]) == 1
    assert w.gov._proposal(approved["proposal_id"])["status"] == "applied"


def test_supervisor_edit_supersedes_and_holds(world02):
    """public_02: a supervisor sets nw_renewal's voice at D5 15:00."""
    w = world02
    w.step(5 * D + 14 * 60 + 45)  # fastest answer is 30 min, so it is still pending at 15:00
    w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V1": 0.5, "V3": 0.5}}}},
                        urgent=True, key="voice-test"), w.now)
    w.gov.tick(w.now)
    assert w.proposals("nw_renewal")[0]["status"] == "pending"
    w.step(5 * D + 15 * 60 + 30)
    assert w.proposals("nw_renewal")[0]["status"] == "superseded"
    assert w.sim.config("nw_renewal")["actor"] == "supervisor"   # we never overwrote it
    hold = [d for d in w.diary("supervisor_hold") if d["workflow_id"] == "nw_renewal"]
    assert hold and "supervisor changed voice" in hold[0]["reason"]
    assert any(d["workflow_id"] == "ks_cancel" and "prompt_version" in d["reason"]
               for d in w.diary("supervisor_hold"))  # the D3 bad-script edit was held too
    w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V3": 1.0}}}}), w.now)
    assert w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V4": 1.0}}}}),
                        w.now) == "held"
    w.step(7 * D)
    assert any(d["action"] == "released" and d["workflow_id"] == "nw_renewal"
               for d in w.diary("supervisor_hold"))
    superseded_id = w.proposals("nw_renewal")[0]["proposal_id"]
    assert all(h["proposal_id"] != superseded_id for h in w.sim.config_history("nw_renewal"))


# ------------------------------------------------------------------ review findings (round 2)

def test_crash_after_lapsed_apply_does_not_strand_the_change(world01, monkeypatch):
    """H1: apply returns 410, controller dies before recording it; restart must free the change."""
    w = world01
    approved = first_approval(w, "nw_renewal", "lapse")
    key = w.conn.execute("SELECT intent_key FROM changes WHERE change_id = ANY(%s)",
                         (approved["change_ids"],)).fetchone()["intent_key"]
    w.sim.advance_to(w.now + 3 * 60)  # controller asleep past the 2h apply window
    ingest_pending(w.sim, w.conn, w.run, ("events",))
    real_send = w.sim.send

    def killed_after_410(method, path, body):
        out = real_send(method, path, body)
        if path.endswith("/apply"):
            assert out[0] == 410
            raise KeyboardInterrupt("killed after 410")
        return out

    monkeypatch.setattr(w.sim, "send", killed_after_410)
    with pytest.raises(KeyboardInterrupt):
        w.gov._apply_approved(w.now)
    monkeypatch.setattr(w.sim, "send", real_send)
    Governor(w.sim, w.conn, w.run, w.sim.workflows()).reconcile(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "lapsed"
    assert w.gov.submit(change("nw_renewal", {"allocations": {"default": {"voice": {"V4": 1.0}}}}, key=key),
                        w.now) == "refreshed"  # back in the queue, not stuck in flight
    assert w.diary("request_lapsed")


def test_auto_path_checks_fields_and_refusals_count_against_the_idea(world01):
    """H2: an auto change with no rationale is never sent; repeated refusals stop the idea."""
    w = world01
    bad = change("nw_renewal", {"allocations": {"default": {"caller_id": {"D1": 0.7, "D2": 0.3}}}})
    bad.rationale = ""
    w.gov.submit(bad, w.now)
    w.gov.tick(w.now)
    assert w.proposals() == [] and "rationale" in w.diary("change_dropped")[0]["reason"]
    key = "refused-twice"
    for _ in range(2):
        w.conn.execute("INSERT INTO changes (run_id, intent_key, workflow_id, urgent, status, created_minute, "
                       "updated_minute, body) VALUES (%s,%s,'nw_renewal',false,'refused',0,0,'{}')",
                       (w.run.run_id, key))
    assert w.gov.submit(change("nw_renewal", {"prompt_version": "P3"}, key=key), w.now) == "given_up"


def test_urgent_never_auto_overrides_a_supervisor_setting(world01):
    """M2: ks_cancel may auto-apply voice, but not on a setting a supervisor just chose."""
    w = world01
    w.conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) "
                   "VALUES (%s,'ks_cancel','voice',0,true)", (w.run.run_id,))
    w.step(11 * 60)
    w.gov.submit(change("ks_cancel", {"allocations": {"default": {"voice": {"V1": 0.9, "V3": 0.1}}}},
                        urgent=True), w.now)
    w.gov.tick(w.now)
    (p,) = w.proposals("ks_cancel")
    assert p["path"] == "approval" and "overrides a recent supervisor setting: voice" in p["request"]["change_summary"]


def test_urgent_midday_note_carries_only_urgent_changes(world01):
    """M3 / G7: normal changes wait for the morning even when an urgent note goes out."""
    w = world01
    w.step(12 * 60)
    w.gov.submit(change("nw_kyc", {"allocations": {"default": {"welcome_message": {"W2": 1.0}}}}), w.now)
    w.gov.submit(change("nw_kyc", {"allocations": {"default": {"voice": {"V2": 1.0}}}}, urgent=True), w.now)
    w.gov.tick(w.now)
    (p,) = w.proposals("nw_kyc")
    assert p["dims"] == ["voice"] and p["request"]["change_summary"].startswith("[URGENT]")
    queued = w.conn.execute("SELECT body->'dims' AS d FROM changes WHERE run_id=%s AND status='queued'",
                            (w.run.run_id,)).fetchall()
    assert [q["d"] for q in queued] == [["welcome_message"]]


def test_one_prompt_change_per_request(world01):
    """M6: two prompt changes in one note would carry the wrong validation; send one, keep one."""
    w = world01
    for pv, val in (("P3", "val_a"), ("P4", "val_b")):
        c = change("ks_cancel", {"prompt_version": pv})
        c.evidence = {"validation_run_id": val, "validation_pass_rate": 0.99}
        w.gov.submit(c, w.now)
    w.step(10 * 60)
    (p,) = w.proposals("ks_cancel")
    assert p["dims"] == ["prompt_version"]
    assert p["request"]["evidence"]["validation_run_id"] == \
        {"P3": "val_a", "P4": "val_b"}[p["request"]["proposed_config"]["prompt_version"]]
    assert w.conn.execute("SELECT count(*) AS n FROM changes WHERE run_id=%s AND status='queued'",
                          (w.run.run_id,)).fetchone()["n"] == 1


def test_same_world_gives_same_proposal_ids():
    """R1: ids feed the approver's random draw, so they must be reproducible per world."""
    s = {"scenario": "x", "ts": "2027-01-04T00:00:00+05:30", "sim_minute": 0, "last_event_id": 5}
    assert store.run_token(s) == store.run_token(dict(s))
    assert store.run_token(s) != store.run_token({**s, "sim_minute": 600, "last_event_id": 99})


def test_malformed_schedule_is_dropped_not_crashing(world01):
    """R2: a bad schedule patch must be refused cleanly, not crash-loop the controller."""
    w = world01
    w.gov.submit(change("nw_renewal", {"schedule": {"hour_weights": {"default": {"9am": 1, "10": None, "11.0": 1}},
                                                     "lead_preferred_hour": {"nw_renewal:p1": 11.0}}}), w.now)
    w.gov.tick(w.now)
    assert w.proposals() == []
    reason = w.diary("change_dropped")[0]["reason"]
    assert all(bit in reason for bit in ("'9am'", "None", "'11.0'", "11.0 not a whole hour"))


# ------------------------------------------------------------------ caller-ID review (round 1)

def test_rejections_count_per_target_not_per_idea(world01):
    """H2: two 'no's to 'remove D2' must not block a later 'remove D3' under the same intent."""
    w = world01
    key = "caller_id_health:nw_kyc"
    for _ in range(2):
        w.conn.execute("INSERT INTO changes (run_id, intent_key, workflow_id, urgent, status, created_minute, "
                       "updated_minute, body) VALUES (%s,%s,'nw_kyc',true,'rejected',0,0,%s)",
                       (w.run.run_id, key, '{"attempts": ["remove D2"]}'))
    again = change("nw_kyc", {"allocations": {"default": {"caller_id": {"D3": 1.0}}}}, urgent=True, key=key)
    again.attempts = ["remove D2", "remove D3"]          # a new bundle still can't re-ask D2 (N3)
    assert w.gov.submit(again, w.now) == "given_up"
    new = change("nw_kyc", {"allocations": {"default": {"caller_id": {"D2": 1.0}}}}, urgent=True, key=key)
    new.attempts = ["remove D1"]
    assert w.gov.attempt_blocked(key, "remove D2", w.now) == "given_up"
    assert w.gov.attempt_blocked(key, "remove D1", w.now) is None
    assert w.gov.submit(new, w.now) == "queued"


def test_crash_after_a_partial_step_resumes_the_steps(world01, monkeypatch):
    """M5: killed right after the sim applied step 1; the restart records it and step 2 follows."""
    w = world01
    w.gov.submit(change("ks_cancel", {"allocations": {"default": {"voice": {"V1": 0.5, "V3": 0.5}}}}), w.now)
    real_send = w.sim.send

    def killed_after_put(method, path, body):
        out = real_send(method, path, body)
        raise KeyboardInterrupt("killed after PUT")

    monkeypatch.setattr(w.sim, "send", killed_after_put)
    with pytest.raises(KeyboardInterrupt):
        w.gov.tick(w.now)
    monkeypatch.setattr(w.sim, "send", real_send)
    g = Governor(w.sim, w.conn, w.run, w.sim.workflows())
    g.reconcile(w.now)
    (p,) = w.proposals("ks_cancel")
    assert (p["status"], p["partial"]) == ("applied", True)
    c = w.conn.execute("SELECT status, steps_done, not_before_minute FROM changes WHERE run_id=%s",
                       (w.run.run_id,)).fetchone()
    assert (c["status"], c["steps_done"], c["not_before_minute"]) == ("queued", 1, 60)
    w.gov = g
    w.step(3 * 60)
    assert [h["proposal_id"] for h in w.sim.config_history("ks_cancel")[1:]] == \
        [p["proposal_id"] for p in w.proposals("ks_cancel")]           # each step applied once
    assert len(w.proposals("ks_cancel")) == 3



def test_withdrawn_change_is_dropped_or_never_applied(world01):
    """N1: a feature that stops asking withdraws at once: queued copies drop, an approved request
    is not applied."""
    w = world01
    w.step(60)
    w.gov.submit(change("nw_kyc", {"allocations": {"default": {"voice": {"V2": 1.0}}}}, key="feat:nw_kyc"), w.now)
    w.gov.withdraw_unlisted("feat:", set(), w.now)
    assert w.diary("change_dropped")[0]["action"] == "withdrawn"
    approved = first_approval(w, "nw_renewal", "feat:wd")
    w.gov.withdraw_unlisted("feat:", set(), w.now)
    w.gov.tick(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "withdrawn"
    assert all(h["proposal_id"] != approved["proposal_id"] for h in w.sim.config_history("nw_renewal"))
    assert "not applied" in w.diary("request_withdrawn")[0]["reason"]


def test_refresh_with_rounding_drift_keeps_the_step_wait(world01):
    """N2: same targets, weights a millionth apart: not a new target, so the hourly gap stays."""
    w = world01
    c = change("ks_cancel", {"allocations": {"default": {"voice": {"V1": 0.5, "V3": 0.5}}}}, urgent=True, key="k")
    c.attempts = ["voice V3"]
    w.gov.submit(c, w.now)
    w.gov.tick(w.now)                                          # step 1
    c.patch = {"allocations": {"default": {"voice": {"V1": 0.500001, "V3": 0.499999}}}}
    assert w.gov.submit(c, w.now) == "refreshed"
    row = w.conn.execute("SELECT steps_done, not_before_minute FROM changes WHERE run_id=%s", (w.run.run_id,)).fetchone()
    assert (row["steps_done"], row["not_before_minute"]) == (1, 60)



def test_withdrawn_then_wanted_again_is_applied(world01):
    """W1: withdrawn while pending, wanted again before the answer: an approval is applied, not wasted."""
    w = world01
    approved = first_approval(w, "nw_renewal", "feat:again")
    key = w.conn.execute("SELECT intent_key, body FROM changes WHERE change_id = ANY(%s)",
                         (approved["change_ids"],)).fetchone()
    w.gov.withdraw_unlisted("feat:", set(), w.now)
    again = change("nw_renewal", key["body"]["patch"], urgent=True, key=key["intent_key"])
    assert w.gov.submit(again, w.now) == "in_flight"
    w.gov.tick(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "applied"


def _approved_bundle(w, attempts_of, urgent):
    """Propose (until approved) a change whose attempts are attempts_of(i); return (approved, key, voice, i)."""
    for i, voice in enumerate(VOICES):
        key = f"exp:nw_renewal-{urgent}-{i}"
        w.gov.submit(Change(intent_key=key, workflow_id="nw_renewal", urgent=urgent,
                            patch={"allocations": {"default": {"voice": voice, "welcome_message": {"W2": 1.0}}}},
                            summary="bundle", rationale="t", affected_leads=1, impact_score=0.1, dry_run={"n": 1},
                            attempts=attempts_of(i)), w.now)
        n = len(w.proposals("nw_renewal"))
        w.step(w.now + 2 * D, stop_before_tick=lambda: len(w.proposals("nw_renewal")) > n and
               w.proposals("nw_renewal")[-1]["status"] not in ("sending", "pending"))
        approved = w.proposals("nw_renewal")[-1]
        if approved["status"] == "approved":
            return approved, key, voice, i
        w.gov.tick(w.now)
    pytest.fail("approver rejected every attempt")


def test_urgent_request_is_never_withdrawn_when_its_targets_change(world01):
    """R3-H: an approved urgent 'stop W2' is still applied when the next step asks to stop W3 instead."""
    w = world01
    approved, key, voice, i = _approved_bundle(w, lambda i: [f"stop greeting W2 {i}"], urgent=True)
    w.gov.submit(Change(intent_key=key, workflow_id="nw_renewal", urgent=True,
                        patch={"allocations": {"default": {"voice": voice}}}, summary="stop W3", rationale="t",
                        affected_leads=1, impact_score=0.1, dry_run={"n": 1}, attempts=[f"stop greeting W3 {i}"]),
                 w.now)
    w.gov.tick(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "applied"


def test_request_whose_targets_shrink_is_never_applied(world01):
    """R2-H (E15): a routine request carrying 'promote greeting W2' and a voice change is approved, but the
    promotion was withdrawn meanwhile (the feature now asks for the voice change only). The old request
    is not applied; a fresh one with only what is still wanted follows once this one is closed."""
    w = world01
    approved, key, voice, i = _approved_bundle(w, lambda i: ["promote greeting W2", f"voice {i}"], urgent=False)
    voice_only = Change(intent_key=key, workflow_id="nw_renewal", urgent=False,
                        patch={"allocations": {"default": {"voice": voice}}}, summary="voice", rationale="t",
                        affected_leads=1, impact_score=0.1, dry_run={"n": 1}, attempts=[f"voice {i}"])
    assert w.gov.submit(voice_only, w.now) == "in_flight"
    w.gov.tick(w.now)
    assert w.gov._proposal(approved["proposal_id"])["status"] == "withdrawn"
    assert all(h["proposal_id"] != approved["proposal_id"] for h in w.sim.config_history("nw_renewal"))
    assert w.gov.submit(voice_only, w.now) == "queued"                   # the fresh request
