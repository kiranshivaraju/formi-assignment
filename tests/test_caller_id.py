"""Shared tools (T1, T2), checker knobs, and the caller-ID checker (C1-C10) on a scripted scoreboard.

The checker only reads the scoreboard and the current config, so these tests write scoreboard rows
directly and hand it a config: fast and exact, no simulator needed. End-to-end behaviour on the
real incident is checked by running scenario 02 (see notes.md, X1).
"""
import pytest

from controller import config_ops, settings, stats, store
from controller.features import Context, caller_id
from tests.test_controller_store import STATE, TEST_DSN, URL, test_db  # noqa: F401

D = 1440
KS = {"governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.2}},
      "options": {"caller_id": ["D1", "D2", "D3"]}, "mid_flight": "allowed"}
KYC = {"governance": {"auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}},
       "options": {"caller_id": ["D1", "D2", "D3"]}, "mid_flight": "next_campaign_only"}


# ------------------------------------------------------------------ T1, T2 on known answers

def test_z_score_matches_kestrel_numbers():
    assert stats.z_score(573, 2385, 224, 2415) == pytest.approx(13.7, abs=0.05)   # day 2: D2 dead
    assert stats.z_score(565, 2385, 558, 2415) == pytest.approx(0.48, abs=0.05)   # day 1: luck
    assert stats.z_score(0, 0, 5, 10) == 0.0


def test_step_toward_and_step_config():
    p = {"D1": 0.5, "D2": 0.5}
    seen = []
    for _ in range(3):
        p = config_ops.step_toward(p, {"D1": 1.0}, 0.2)
        seen.append(p)
    assert seen == [{"D1": 0.7, "D2": 0.3}, {"D1": 0.9, "D2": 0.1}, {"D1": 1.0}]
    three = config_ops.step_toward({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, {"a": 1.0}, 0.2)
    assert config_ops.tv_distance({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, three) <= 0.2
    old = {"allocations": {"default": {"caller_id": {"D1": 0.5, "D2": 0.5}},
                           "young": {"voice": {"V1": 1.0}}}}            # young inherits caller_id
    new = config_ops.apply_patch(old, {"allocations": {"default": {"caller_id": {"D1": 1.0}}}})
    step = config_ops.step_config(old, new, 0.2)
    assert step["allocations"]["default"]["caller_id"] == {"D1": 0.7, "D2": 0.3}
    assert "caller_id" not in step["allocations"]["young"]               # still follows default
    assert config_ops.max_shift(old, step) <= 0.2
    assert config_ops.steps_needed(old, new, 0.2) == 3


def test_settings_reject_typos_and_silly_values(tmp_path):
    good = settings.load()
    assert good.feature("caller_id_health", "any")["probe_share"] == 0.05
    raw = settings.DEFAULT_PATH.read_text()
    for bad, msg in [(raw.replace("min_calls: 200", "min_calls: -5"), "min_calls"),
                     (raw.replace("min_calls: 200", "min_cals: 200"), "unknown setting"),
                     (raw.replace("workflows: {}", "workflows: {nw_kyc: {probe_share: 0.9}}"), "probe_share")]:
        f = tmp_path / "s.yaml"
        f.write_text(bad)
        with pytest.raises(settings.SettingsError, match=msg):
            settings.load(f)
    f.write_text(raw.replace("workflows: {}", "workflows: {nw_kyc: {min_calls: 150}}"))
    s = settings.load(f)
    assert s.feature("caller_id_health", "nw_kyc")["min_calls"] == 150
    assert s.feature("caller_id_health", "ks_cancel")["min_calls"] == 200


# ------------------------------------------------------------------ the checker

@pytest.fixture
def db(request):
    with store.connect(TEST_DSN) as conn:
        run, _ = store.resolve_run(conn, URL, {**STATE, "scenario": request.node.name})
        yield conn, run


def put(conn, run, wid, day, hour, number, dials, answered):
    conn.execute("INSERT INTO call_stats VALUES (%s,%s,%s,%s,'default','W1','V1',%s,'P1',%s,%s,0,0) "
                 "ON CONFLICT (run_id, workflow_id, sim_day, hour, cohort_id, welcome_message, voice, caller_id, "
                 "prompt_version) DO UPDATE SET dials=EXCLUDED.dials, connected=EXCLUDED.connected",
                 (run.run_id, wid, day, hour, number, dials, answered))


def cfg(split):
    return {"version": 1, "config": {"allocations": {"default": {"caller_id": split}}}}


def ctx(conn, run, now, configs, workflows, spread=False, gov=None):
    """spread=False: the tests below pin the original rules (spares only when nothing healthy is left),
    which still apply with `spread_spares: false`; load spreading has its own tests at the end."""
    st = settings.load()
    st.raw["caller_id_health"]["defaults"]["spread_spares"] = spread
    c = Context(conn, run, sim=None, gov=gov, settings=st, workflows=workflows, now=now)
    c._configs = dict(configs)
    return c


def hours(conn, run, wid, day, hrs, rates, per_hour=200):
    """Fill hours of a day: rates = {number: answer rate}."""
    for h in hrs:
        for n, r in rates.items():
            put(conn, run, wid, day, h, n, per_hour, round(per_hour * r))


def diary(conn, run):
    return [r["reason"] for r in conn.execute("SELECT reason FROM decisions WHERE run_id=%s ORDER BY decision_id",
                                               (run.run_id,)).fetchall()]


def test_dead_number_flagged_with_measured_note(db):
    conn, run = db
    hours(conn, run, "ks", 2, [11, 12, 13], {"D1": 0.24, "D2": 0.095})
    now = 2 * D + 14 * 60
    (ch,) = caller_id.check(ctx(conn, run, now, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS}))
    assert ch.intent_key == "caller_id_health:ks" and ch.urgent
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D1": 1.0}}}}
    assert ch.summary == "Stop calling from caller ID D2 (answered 9.5% vs 24.0% on D1, flagged D2 14:00); calls go to D1"
    assert 0 < ch.impact_score <= 1 and ch.dry_run["D2"]["extra_answered_per_day"] == round((0.24 - 0.095) * 600)
    assert ch.ttl_min == 60
    assert "Removing it" in diary(conn, run)[0]


def test_luck_sized_gap_is_ignored(db):
    conn, run = db
    hours(conn, run, "ks", 1, [11, 12, 13], {"D1": 0.237, "D2": 0.231})
    assert caller_id.check(ctx(conn, run, D + 14 * 60, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS})) == []
    assert diary(conn, run) == []


def test_quiet_workflow_widens_the_window(db):
    conn, run = db
    hours(conn, run, "ks", 2, range(1, 14), {"D1": 0.3, "D2": 0.1}, per_hour=20)  # 20 calls/hour each
    (ch,) = caller_id.check(ctx(conn, run, 2 * D + 14 * 60, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS}))
    assert ch.evidence["verdicts"]["D2"]["hours"] == 12   # 3 h and 6 h had fewer than 200 calls


def test_single_number_vs_its_past_switches_to_a_spare(db):
    conn, run = db
    for day in (-3, -2, -1):
        hours(conn, run, "kyc", day, [11, 12, 13], {"D1": 0.34})
    hours(conn, run, "kyc", 0, [11, 12, 13], {"D1": 0.12})
    (ch,) = caller_id.check(ctx(conn, run, 14 * 60, {"kyc": cfg({"D1": 1.0})}, {"kyc": KYC}))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D2": 1.0}}}}
    assert "its own past" in ch.summary and "spare caller ID D2" in ch.summary
    assert "no automatic second chance" not in " ".join(diary(conn, run))  # not removed yet


def test_single_number_shared_drop_is_not_the_number(db):
    conn, run = db
    for day in (-3, -2, -1):
        hours(conn, run, "kyc", day, [11, 12, 13], {"D1": 0.34})
        hours(conn, run, "other", day, [11, 12, 13], {"D1": 0.30})
    hours(conn, run, "kyc", 0, [11, 12, 13], {"D1": 0.12})
    hours(conn, run, "other", 0, [11, 12, 13], {"D1": 0.11})            # everyone dropped: a holiday
    assert caller_id.check(ctx(conn, run, 14 * 60, {"kyc": cfg({"D1": 1.0})}, {"kyc": KYC})) == []
    assert "shared drop, not the number" in diary(conn, run)[0]


def test_full_life_removed_trial_restored(db):
    """C9, C10: removed -> verified -> trial at 5% after 24 h -> passes -> back to 50/50 -> forgotten."""
    conn, run = db
    wf = {"ks": KS}
    hours(conn, run, "ks", 2, [11, 12, 13], {"D1": 0.24, "D2": 0.09})
    t = 2 * D + 14 * 60
    assert caller_id.check(ctx(conn, run, t, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, wf))
    t += 120                                               # the gate finished stepping
    hours(conn, run, "ks", 2, [16, 17, 18], {"D1": 0.24})
    assert caller_id.check(ctx(conn, run, t, {"ks": cfg({"D1": 1.0})}, wf)) == []
    state = store.get_state(conn, run, "caller_id_health:ks")["numbers"]["D2"]
    assert state["status"] == "removed" and state["retry_at"] == t + 24 * 60
    assert caller_id.check(ctx(conn, run, t + 180, {"ks": cfg({"D1": 1.0})}, wf)) == []
    assert any(r.startswith("after removing caller ID D2: workflow pickup 24.0%") for r in diary(conn, run))

    t += 24 * 60                                           # second chance
    (probe,) = caller_id.check(ctx(conn, run, t, {"ks": cfg({"D1": 1.0})}, wf))
    assert not probe.urgent and probe.patch["allocations"]["default"]["caller_id"] == {"D1": 0.95, "D2": 0.05}
    on_trial = cfg({"D1": 0.95, "D2": 0.05})
    assert caller_id.check(ctx(conn, run, t + 15, {"ks": on_trial}, wf)) == []   # live, collecting calls
    hours(conn, run, "ks", 4, range(9, 19), {"D1": 0.24, "D2": 0.235}, per_hour=60)  # the next day
    (restore,) = caller_id.check(ctx(conn, run, 4 * D + 19 * 60, {"ks": on_trial}, wf))
    assert restore.patch["allocations"]["default"]["caller_id"] == {"D1": 0.5, "D2": 0.5}
    assert caller_id.check(ctx(conn, run, 4 * D + 20 * 60, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, wf)) == []
    assert store.get_state(conn, run, "caller_id_health:ks")["numbers"] == {}
    assert "back to its normal share" in diary(conn, run)[-1]


def test_failed_trial_removes_again_and_waits_twice_as_long(db):
    conn, run = db
    wf = {"ks": KS}
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "probing", "before": {"default": {"D1": 0.5, "D2": 0.5}}, "cooldown_h": 24,
        "removed_minute": 0, "live_minute": 3 * D, "flagged_minute": 0}}}, 3 * D)
    hours(conn, run, "ks", 3, range(1, 11), {"D1": 0.24, "D2": 0.08}, per_hour=40)
    (ch,) = caller_id.check(ctx(conn, run, 3 * D + 12 * 60, {"ks": cfg({"D1": 0.95, "D2": 0.05})}, wf))
    assert ch.urgent and ch.patch["allocations"]["default"]["caller_id"] == {"D1": 1.0}
    s = store.get_state(conn, run, "caller_id_health:ks")["numbers"]["D2"]
    assert (s["status"], s["cooldown_h"]) == ("removing", 48)
    assert "Failed its trial: next second chance in 48 h" in diary(conn, run)[-1]


def test_no_trial_where_the_client_must_approve_caller_ids(db):
    conn, run = db
    store.put_state(conn, run, "caller_id_health:kyc", {"numbers": {"D1": {
        "status": "removed", "before": {"default": {"D1": 1.0}}, "cooldown_h": 24, "removed_minute": 0,
        "retry_at": 60, "verify_at": 10**6, "flagged_minute": 0}}}, 0)
    assert caller_id.check(ctx(conn, run, 5 * D, {"kyc": cfg({"D2": 1.0})}, {"kyc": KYC})) == []
    assert store.get_state(conn, run, "caller_id_health:kyc")["numbers"]["D1"]["status"] == "removed"


def test_trial_needs_evidence_of_health_not_just_too_few_calls(db):
    """Found in the scenario 02 run: 10.9% vs 20.0% on 202 trial calls is 45% worse but only 3.2 luck
    units. That is not a pass: keep the trial running, and count it failed if still unclear later."""
    conn, run = db
    wf = {"ks": KS}
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "probing", "before": {"default": {"D1": 0.5, "D2": 0.5}}, "cooldown_h": 24,
        "removed_minute": 0, "live_minute": 3 * D, "flagged_minute": 0}}}, 3 * D)
    hours(conn, run, "ks", 3, range(1, 6), {"D1": 0.20, "D2": 0.11}, per_hour=42)    # 210 trial calls
    on_trial = {"ks": cfg({"D1": 0.95, "D2": 0.05})}
    assert caller_id.check(ctx(conn, run, 3 * D + 7 * 60, on_trial, wf)) == []          # still on trial
    assert store.get_state(conn, run, "caller_id_health:ks")["numbers"]["D2"]["status"] == "probing"
    hours(conn, run, "ks", 3, range(1, 21), {"D1": 0.20, "D2": 0.20}, per_hour=42)   # 840 calls, as good
    (ch,) = caller_id.check(ctx(conn, run, 3 * D + 22 * 60, on_trial, wf))
    assert ch.patch["allocations"]["default"]["caller_id"] == {"D1": 0.5, "D2": 0.5}  # now a clear pass


def test_trial_still_unclear_after_800_calls_counts_as_failed(db):
    """H3: 25% worse is under the alarm line but never clearly fine; don't trial forever."""
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "probing", "before": {"default": {"D1": 0.5, "D2": 0.5}}, "cooldown_h": 24,
        "removed_minute": 0, "live_minute": 3 * D, "flagged_minute": 0}}}, 3 * D)
    hours(conn, run, "ks", 3, range(1, 21), {"D1": 0.20, "D2": 0.15}, per_hour=42)
    (ch,) = caller_id.check(ctx(conn, run, 3 * D + 22 * 60, {"ks": cfg({"D1": 0.95, "D2": 0.05})}, {"ks": KS}))
    assert ch.urgent and ch.patch["allocations"]["default"]["caller_id"] == {"D1": 1.0}
    assert "trial still unclear after 840 calls" in diary(conn, run)[-1]


def test_three_number_split_can_be_stepped():
    """H1: rounding left 1/3 splits a hair over the limit once normalised, so stepping gave up."""
    old = {"allocations": {"default": {"caller_id": {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}}}}
    new = {"allocations": {"default": {"caller_id": {"D1": 0.5, "D3": 0.5}}}}
    for limit in (0.1, 0.15, 0.2, 0.25):
        step = config_ops.step_config(old, new, limit)
        assert step is not None and config_ops.max_shift(old, step) <= limit


def test_supervisor_edit_during_trial_is_respected(db):
    """M2: a supervisor touching caller IDs while our trial runs wins; we never re-add the number."""
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "probing", "probe_minute": 3 * D, "before": {"default": {"D1": 0.5, "D2": 0.5}},
        "cooldown_h": 24, "removed_minute": 0, "live_minute": 3 * D + 15, "flagged_minute": 0}}}, 3 * D)
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute) VALUES (%s,'ks','caller_id',%s)",
                 (run.run_id, 3 * D + 60))
    assert caller_id.check(ctx(conn, run, 3 * D + 75, {"ks": cfg({"D1": 1.0})}, {"ks": KS})) == []
    assert store.get_state(conn, run, "caller_id_health:ks")["numbers"] == {}
    assert "leaving their split alone" in diary(conn, run)[-1]


def test_turned_down_removal_is_rejudged_and_cancelled(db):
    """H2: a removal the approver said no to is not stuck forever: once the number is clearly fine,
    we stop asking."""
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "removing", "flagged_minute": 0, "before": {"default": {"D1": 0.5, "D2": 0.5}},
        "cooldown_h": 24}}}, 0)
    hours(conn, run, "ks", 2, [11, 12, 13], {"D1": 0.24, "D2": 0.24})
    assert caller_id.check(ctx(conn, run, 2 * D + 14 * 60, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS})) == []
    assert store.get_state(conn, run, "caller_id_health:ks")["numbers"] == {}
    assert "no longer looks bad" in diary(conn, run)[-1]


def test_change_names_its_targets(db):
    conn, run = db
    hours(conn, run, "ks", 2, [11, 12, 13], {"D1": 0.24, "D2": 0.095})
    (ch,) = caller_id.check(ctx(conn, run, 2 * D + 14 * 60, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS}))
    assert ch.attempts == ["remove D2"]


class FakeGov:
    def __init__(self, blocked):
        self.blocked = blocked

    def attempt_blocked(self, intent, attempt, now):
        return self.blocked.get(attempt)


def test_turned_down_removal_is_left_out_of_new_bundles(db):
    """N3: D2's removal was given up; when D3 goes bad, only D3 is removed and D2 stays in use."""
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "removing", "flagged_minute": 0, "before": {"default": {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}},
        "cooldown_h": 24, "verdict": None}}}, 0)
    hours(conn, run, "ks", 2, [11, 12, 13], {"D1": 0.24, "D2": 0.12, "D3": 0.08})
    c = ctx(conn, run, 2 * D + 14 * 60, {"ks": cfg({"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3})}, {"ks": KS})
    c.gov = FakeGov({"remove D2": "given_up"})
    (ch,) = caller_id.check(c)
    assert ch.attempts == ["remove D3"]
    assert set(ch.patch["allocations"]["default"]["caller_id"]) == {"D1", "D2"}
    assert any("turned it down (given_up)" in r for r in diary(conn, run))


def test_number_on_trial_never_takes_other_numbers_calls(db):
    """Found in the scenario 02 rerun: D1 was removed while dead D2 was on its 5% trial, and the
    checker moved D1's calls onto D2. A trial number keeps its trial share; the rest goes to a spare."""
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "probing", "probe_minute": 0, "before": {"default": {"D1": 0.5, "D2": 0.5}},
        "cooldown_h": 24, "removed_minute": 0, "live_minute": 6 * D, "flagged_minute": 0}}}, 6 * D)
    for day in (3, 4, 5):
        hours(conn, run, "ks", day, [14, 15, 16], {"D1": 0.25})
    hours(conn, run, "ks", 6, [14, 15, 16], {"D1": 0.15})                  # D1 now worn out
    (ch,) = caller_id.check(ctx(conn, run, 6 * D + 17 * 60, {"ks": cfg({"D1": 0.95, "D2": 0.05})}, {"ks": KS}))
    assert ch.patch["allocations"]["default"]["caller_id"] == {"D3": 0.95, "D2": 0.05}


def test_verify_waits_for_calls_after_a_late_switch(db):
    conn, run = db
    store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
        "status": "removed", "before": {"default": {"D1": 0.5, "D2": 0.5}}, "cooldown_h": 24,
        "removed_minute": 22 * 60, "verify_at": D + 60, "retry_at": 10 * D, "flagged_minute": 20 * 60}}}, 0)
    hours(conn, run, "ks", 0, [17, 18, 19], {"D1": 0.24, "D2": 0.09})
    assert caller_id.check(ctx(conn, run, D + 60, {"ks": cfg({"D1": 1.0})}, {"ks": KS})) == []
    assert not any("after removing" in r for r in diary(conn, run))        # 01:00, no calls yet
    hours(conn, run, "ks", 1, [9, 10], {"D1": 0.24})
    caller_id.check(ctx(conn, run, D + 11 * 60, {"ks": cfg({"D1": 1.0})}, {"ks": KS}))
    assert any(r.startswith("after removing caller ID D2: workflow pickup 24.0% on 400 calls")
               for r in diary(conn, run))


# ------------------------------------------------------------------ load spreading (L1-L8)

def test_never_used_spares_get_a_trial_share_then_an_equal_split(db):
    """L1, L2: KYC has only ever used D1. D2 and D3 (no calls in our records) get 20% between them on
    trial; when they prove themselves, every number gets a third."""
    conn, run = db
    hours(conn, run, "kyc", 0, [10, 11, 12], {"D1": 0.30})
    (ch,) = caller_id.check(ctx(conn, run, 13 * 60, {"kyc": cfg({"D1": 1.0})}, {"kyc": KYC}, spread=True))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D1": 0.9, "D2": 0.05, "D3": 0.05}}}}
    assert not ch.urgent and sorted(ch.attempts) == ["trial spare D2", "trial spare D3"]
    assert ch.summary.startswith("Try never-used caller IDs D2, D3 with 5% of calls each")
    trial = cfg({"D1": 0.9, "D2": 0.05, "D3": 0.05})
    caller_id.check(ctx(conn, run, 13 * 60 + 15, {"kyc": trial}, {"kyc": KYC}, spread=True))  # live
    hours(conn, run, "kyc", 1, [10, 11, 12, 13], {"D1": 0.24, "D2": 0.33, "D3": 0.32}, per_hour=200)
    (ch,) = caller_id.check(ctx(conn, run, D + 14 * 60, {"kyc": trial}, {"kyc": KYC}, spread=True))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D1": 0.333333, "D2": 0.333333, "D3": 0.333333}}}}
    assert ch.summary.startswith("Spread calls evenly") and ch.attempts == ["spread over D1/D2/D3"]


def test_spare_that_fails_its_trial_is_left_out_for_good(db):
    conn, run = db
    hours(conn, run, "kyc", 0, [10, 11, 12], {"D1": 0.30})
    caller_id.check(ctx(conn, run, 13 * 60, {"kyc": cfg({"D1": 1.0})}, {"kyc": KYC}, spread=True))
    trial = cfg({"D1": 0.9, "D2": 0.05, "D3": 0.05})
    caller_id.check(ctx(conn, run, 13 * 60 + 15, {"kyc": trial}, {"kyc": KYC}, spread=True))
    hours(conn, run, "kyc", 1, [10, 11, 12, 13], {"D1": 0.30, "D2": 0.30, "D3": 0.10}, per_hour=200)
    (ch,) = caller_id.check(ctx(conn, run, D + 14 * 60, {"kyc": trial}, {"kyc": KYC}, spread=True))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D1": 0.5, "D2": 0.5}}}}
    assert any("spare caller ID D3 failed its trial" in r for r in diary(conn, run))


def test_number_someone_else_removed_is_not_a_spare(db):
    """L6: D2 has calls in our records and is now unused: a supervisor took it out. Never brought back."""
    conn, run = db
    hours(conn, run, "ks", -1, [10, 11], {"D1": 0.25, "D2": 0.25})
    hours(conn, run, "ks", 0, [10, 11], {"D1": 0.25})
    (ch,) = caller_id.check(ctx(conn, run, 12 * 60, {"ks": cfg({"D1": 1.0})}, {"ks": KS}, spread=True))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D1": 0.9, "D3": 0.1}}}}  # D3 only


def test_no_spreading_after_a_supervisor_caller_id_edit(db):
    conn, run = db
    conn.execute("INSERT INTO holds (run_id, workflow_id, dim, since_minute, processed) VALUES (%s,'kyc','caller_id',60,true)",
                 (run.run_id,))
    hours(conn, run, "kyc", 0, [10, 11, 12], {"D1": 0.30})
    assert caller_id.check(ctx(conn, run, 13 * 60, {"kyc": cfg({"D1": 1.0})}, {"kyc": KYC}, spread=True)) == []
    assert store.get_state(conn, run, "caller_id_health:kyc").get("numbers", {}) == {}    # no spare trial started


def test_no_second_chance_while_two_healthy_numbers_carry_the_calls(db):
    """L9: D2 was removed; D1 and D3 are healthy. Trying dead D2 again only risks harm, so no trial
    until fewer than 2 healthy numbers are left."""
    conn, run = db
    with conn.transaction():
        store.put_state(conn, run, "caller_id_health:ks", {"numbers": {"D2": {
            "status": "removed", "flagged_minute": 600, "removed_minute": 660, "verify_at": 840, "retry_at": 2100,
            "verified": True, "cooldown_h": 24, "before": {"default": {"D1": 0.5, "D2": 0.5}}}}}, 660)
    hours(conn, run, "ks", 1, [11, 12, 13], {"D1": 0.25, "D3": 0.25})
    assert caller_id.check(ctx(conn, run, D + 14 * 60, {"ks": cfg({"D1": 0.5, "D3": 0.5})}, {"ks": KS})) == []
    assert any("no second chance for caller ID D2" in r for r in diary(conn, run))



# ------------------------------------------------------------------ spreading review round 1 (spreading on)

def _removing(minute=600):
    return {"status": "removing", "flagged_minute": minute, "cooldown_h": 24,
            "before": {"default": {"D1": 0.45, "D2": 0.45, "D3": 0.1}},
            "verdict": {"number": "D1", "basis": "D2", "hours": 3, "calls": 600, "answered": 60, "ref_calls": 600,
                        "ref_answered": 180, "rate": 0.1, "ref_rate": 0.3, "worse_by": 0.67, "z": 9.0, "bad": True,
                        "note": "", "pass_z": -5.0}}


def test_turned_down_removal_of_a_failed_spare_never_blocks_other_fixes(db):
    """R-H1: removing failed spare D3 was turned down twice; dead D1 is still removed, and D3 keeps its
    share (it is not given more calls)."""
    conn, run = db
    with conn.transaction():
        store.put_state(conn, run, "caller_id_health:kyc", {"numbers": {
            "D1": _removing(), "D3": {"status": "spare_failed", "flagged_minute": 500, "verdict": None}}}, 600)
    gov = FakeGov({"remove D3": "given_up"})
    (ch,) = caller_id.check(ctx(conn, run, 615, {"kyc": cfg({"D1": 0.45, "D2": 0.45, "D3": 0.1})}, {"kyc": KYC},
                                spread=True, gov=gov))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D2": 0.9, "D3": 0.1}}}}
    assert ch.attempts == ["remove D1"] and ch.urgent


def test_spare_on_trial_takes_over_when_every_used_number_is_dead(db):
    """R-H3: D1 and D2 are both being removed, D3 is on trial but not live yet: all calls go to D3."""
    conn, run = db
    with conn.transaction():
        store.put_state(conn, run, "caller_id_health:ks", {"numbers": {
            "D1": _removing(), "D2": {**_removing(), "before": {"default": {"D1": 0.5, "D2": 0.5}}},
            "D3": {"status": "spare_trial", "since": 0, "live_minute": None}}}, 600)
    (ch,) = caller_id.check(ctx(conn, run, 615, {"ks": cfg({"D1": 0.5, "D2": 0.5})}, {"ks": KS}, spread=True))
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D3": 1.0}}}} and ch.urgent


def test_live_spare_trial_is_not_a_target_of_later_changes(db):
    """R-H2: D2/D3 trials are already live; a later removal of D1 carries only 'remove D1', so a
    rejection of it can never count against (or skip) the spares' trial."""
    conn, run = db
    with conn.transaction():
        store.put_state(conn, run, "caller_id_health:kyc", {"numbers": {
            "D1": _removing(), "D2": {"status": "spare_trial", "since": 0, "live_minute": 60},
            "D3": {"status": "spare_trial", "since": 0, "live_minute": 60}}}, 600)
    (ch,) = caller_id.check(ctx(conn, run, 615, {"kyc": cfg({"D1": 0.8, "D2": 0.1, "D3": 0.1})}, {"kyc": KYC},
                                spread=True))
    assert ch.attempts == ["remove D1"]
    assert ch.patch == {"allocations": {"default": {"caller_id": {"D2": 0.5, "D3": 0.5}}}}
