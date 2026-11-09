"""Greeting and voice experiments (C6), winner promotion, fading detection and rollback (C7).

Decision ids E1-E14 are in notes.md; numbers below quote the settings.yaml defaults.

Per workflow, each dimension (welcome_message = the greeting, voice) has its own life, kept in
feature_state and explained line by line in the diary:

    idle --start--> testing --daily winner check--> promoted --faded or harmful--> testing again
      ^               |  |   (z >= 3, guardrails,      (80% winner, 20% on the     without the faded
      |               |  |    group harm check)          old option to compare)     option (retired for good)
      |               |  +-- an option clearly worse: stopped at once (urgent)
      |               |  +-- no winner after enough calls: an option still ahead (2+ luck units) keeps
      |               |      running 50/50 against today's option until it proves itself or falls back
      |               +----- otherwise: back to today's option, test done (no automatic re-test, E9)

    any phase --a supervisor edits it--> supervisor_owned (for good: we never test it again, E14)
    the approver turns the same thing down twice: a safety move (stop / move off) is asked again a
    day later; a promotion or narrowing ends the test instead; a test start or end is left as it is

Urgent safety moves (stop a harmful option, move off a faded one) go out under their own intent,
so they never wait behind a routine request the approver is still thinking about (R-H2).

Only workflows whose primary KPI is engagement take part (E7). Every comparison is made inside each
hour of each day and cohort, then added up (E4), between options that ran at the same time, so a bad
day for everyone (a bad script, a holiday) cannot make an option look better or worse.

Routine changes go in one change per workflow carrying both dimensions and any group carve-outs,
because carve-outs live in the config's single cohort list and two changes would overwrite it.
Carve-outs stay in place after a rollback or the end of a test: they pin a group to the old option,
which is harmless, and removing them would need the approver (cohorts are never auto-applied).
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict

from .. import config_ops, stats, store
from ..governance import Change
from ..store import MINUTES_PER_DAY

FEATURE = "experiments"
DIMS = ("welcome_message", "voice")
NAME = {"welcome_message": "greeting", "voice": "voice"}
CARVE_PREFIX = "exp_"       # cohorts we own: exp_<feature>_<value>
MAX_COHORTS = 20            # the sim's limit
EPS = 1e-3
# (calls, hits) positions in a stratum's [dials, connected, engaged, hallucinations]
METRIC = {"engagement_rate": (1, 2), "pickup_rate": (0, 1), "made_up": (1, 3)}
log = logging.getLogger(__name__)


# ---------------------------------------------------------------- pure helpers (tested alone)

def strata(rows: list[dict], extra: tuple[str, ...] = ()) -> dict:
    """{(day, hour, cohort, *extra): {option: [dials, connected, engaged, hallucinations]}}"""
    out: dict = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    for r in rows:
        c = out[(r["sim_day"], r["hour"], r["cohort_id"], *(r[f] for f in extra))][r["option"]]
        c[0] += r["dials"]
        c[1] += r["connected"]
        c[2] += r["engaged"]
        c[3] += r["hallucinations"]
    return out


def compare(st: dict, a: str, b: str, metric: str = "engagement_rate") -> stats.Lift:
    """Option b against option a, compared inside each stratum where both ran (E4)."""
    n, h = METRIC[metric]
    return stats.stratified_lift((s[a][n], s[a][h], s[b][n], s[b][h]) for s in st.values() if a in s and b in s)


def test_split(arms: list[str], control: str, share) -> dict[str, float]:
    """E11: 'equal' gives every option the same share; a number is today's option's share."""
    others = [o for o in arms if o != control]
    if not others:
        return {control: 1.0}
    if share == "equal":
        return {o: round(1 / len(arms), 6) for o in arms}
    return {control: float(share), **{o: round((1 - share) / len(others), 6) for o in others}}


def same(a: dict, b: dict) -> bool:
    return config_ops.tv_distance(_norm(a), _norm(b)) < EPS


def _norm(d: dict) -> dict:
    t = sum(d.values())
    return {k: v / t for k, v in d.items()} if t else {}


def lift_dict(x: stats.Lift) -> dict:
    return {**asdict(x), "rate_a": x.rate_a, "rate_b": x.rate_b}


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def carve_id(feature: str, value: str) -> str:
    return f"{CARVE_PREFIX}{feature}_{value}".replace(" ", "_")


# ---------------------------------------------------------------- the feature

def check(ctx) -> list[Change]:
    out = []
    for wid in ctx.workflows:
        try:
            changes = Workflow(ctx, wid).check()
        except Exception as e:  # one workflow's problem must not skip the others
            if type(e).__module__.startswith(("httpx", "psycopg")):
                raise
            log.exception("experiments check failed for %s at sim minute %s", wid, ctx.now)
            ctx.keep |= {f"{FEATURE}:{wid}", f"{FEATURE}:{wid}:urgent"}  # couldn't judge: keep what it asked
            ctx.diary(f"feature_error:{FEATURE}:{wid}:{ctx.now // 60}", wid, "feature_error", "skipped",
                      f"experiments check raised {type(e).__name__}: {e}; skipped this step", {"feature": FEATURE})
            continue
        out += changes
    return out


class Workflow:
    def __init__(self, ctx, wid: str):
        self.ctx, self.wid, self.now = ctx, wid, ctx.now
        self.k = ctx.settings.feature(FEATURE, wid)
        self.wf = ctx.workflows[wid]
        self.intent = f"{FEATURE}:{wid}"
        self.notes: list[tuple[str, str, str, dict]] = []

    def note(self, key: str, action: str, reason: str, inputs: dict | None = None) -> None:
        self.notes.append((f"{FEATURE}:{self.wid}:{key}", action, reason, inputs or {}))

    def why_not(self) -> str | None:
        if not self.k["enabled"]:
            return "experiments are turned off for this workflow in settings"
        primary = (self.wf.get("objective") or {}).get("primary")
        if primary != "engagement_rate":
            return (f"no greeting/voice tests: the primary KPI is {primary}, and people decide to pick up before "
                    f"they hear the greeting or voice, so a test could only risk the guardrail (E7)")
        return None

    # ------------------------------------------------------------ one loop step
    def check(self) -> list[Change]:
        ctx, wid = self.ctx, self.wid
        reason = self.why_not()
        if reason:
            ctx.diary(f"{FEATURE}:{wid}:skip", wid, FEATURE, "no_action", reason, {})
            return []
        self.cfg = ctx.config(wid)["config"]
        skey = f"{FEATURE}:{wid}"
        dims = self.dims = store.get_state(ctx.conn, ctx.run, skey).get("dims", {})
        day = store.sum_rows(store.hourly_counts(ctx.conn, ctx.run, since=self.now - MINUTES_PER_DAY,
                                                 workflow_id=wid))
        self.answered_24h = day["connected"]
        frozen = set()
        for dim in DIMS:
            if len(self.wf["options"].get(dim, [])) < 2:
                continue
            s = dims.setdefault(dim, {"phase": "idle"})
            self.step(dim, s)
            if self.blocked(dim, s):
                frozen.add(dim)
        with ctx.conn.transaction():  # state and the diary lines that explain it commit together
            store.put_state(ctx.conn, ctx.run, skey, {"dims": dims}, self.now)
            for key, action, reason, inputs in self.notes:
                ctx.diary(key, wid, FEATURE, action, reason, inputs)
        return self.change(dims, frozen)

    def urgent(self, dim: str, s: dict) -> bool:
        """A safety move still to make: an option to stop or move off still has more than its target."""
        target, cur = s.get("target") or {}, self.current(dim)
        return any(target.get(o, 0.0) + EPS < cur.get(o, 0.0) for o in s.get("urgent_off", []))

    def intent_for(self, dim: str, s: dict) -> str:
        return f"{self.intent}:urgent" if self.urgent(dim, s) else self.intent

    def blocked(self, dim: str, s: dict) -> bool:
        """G17: the approver turned this exact thing down. Cooling down: wait a day. Twice (R-M1):
        a safety move is asked again a day later (fresh evidence, it must not be dropped for good);
        a promotion or narrowing ends the test instead (never leave a split nobody watches); a test
        start or end is left as it is."""
        if self.ctx.gov is None or not s.get("attempt") or not s.get("target"):
            return False
        a = s["attempt"]
        if s.get("reask_for") != a:  # R2-L1: a wait belongs to one attempt only
            s.pop("reask_at", None)
            s.pop("reask_for", None)
        outcome = self.ctx.gov.attempt_blocked(self.intent_for(dim, s), a, self.now)
        if outcome == "given_up" and s["phase"] == "supervisor_owned":  # R2-L3
            self.note(f"{dim}:leftover_declined:{a}", "declined",
                      f"the approver turned down '{a}' twice; our old {NAME[dim]} split stays live and is no longer "
                      f"watched by us (the supervisor owns the {NAME[dim]})", {"attempt": a})
            s.update(target=None)
            return True
        if outcome == "given_up":
            if s.get("urgent_off"):
                if "reask_at" not in s:
                    s["reask_at"], s["reask_for"] = self.now + MINUTES_PER_DAY, a
                    self.note(f"{dim}:reask:{a}", "wait", f"the approver turned down '{a}' twice; it is a safety "
                              f"move, so we ask again after {store.sim_label(s['reask_at'])}", {"attempt": a})
                if self.now < s["reask_at"]:
                    return True
                s["attempt"] = f"{a.split(' (asked again')[0]} (asked again {store.sim_label(self.now)})"
                return False
            self.note(f"{dim}:declined:{a}", "declined",
                      f"the approver turned down '{a}' twice; we stop asking for it", {"attempt": a})
            if a.startswith("promote") and a.endswith("step 2"):
                # R4-L: the approver refused only the main share; the proven winner stays at its step-1
                # share, still watched for fading (E13)
                s.update(promo_step=None, step2_pending=False, target=self.current(dim))
                return True
            if a.startswith(("promote", "narrow")):
                if a.startswith("promote"):  # R3-M1: its carve-outs never went live; don't apply them later
                    s.update(carve=s.get("live_carve", {}), champion=None, promo=None, phase="testing")
                self.end_test(dim, s, f"the approver turned down '{a}' twice")
            elif a.startswith("end") and s.get("arms"):
                # R2-M1: the approver keeps the test running; so do our harm checks (judging resumes)
                s.update(phase="testing", target=self.current(dim))
            else:
                s.update(phase="done", target=None)
            return True
        if outcome and a.startswith("promote") and "step 2" not in a and s["phase"] == "promoted" \
                and s.get("live") is None:
            # E17: a turned-down promotion is not re-sent on the evidence the approver already said no
            # to. The test goes on; the winner must win again on calls made after that decision.
            w = s["champion"]
            s.update(phase="testing", target=s.get("test_split") or self.current(dim), champion=None, promo=None,
                     carve=s.get("live_carve", {}), promo_step=None,
                     evidence_since=s["promoted"], judged_day=self.now // MINUTES_PER_DAY,
                     attempt=f"test {NAME[dim]} {'/'.join(s['arms'])}", impact=0.0, urgent_off=[])
            self.note(f"{dim}:promotion_declined:{w}:{s['evidence_since']}", "wait",
                      f"the approver turned down '{a}'; the test goes on and {NAME[dim]} {w} must win again on calls "
                      f"made after {store.sim_label(s['evidence_since'])} before it is proposed again (E17)", {})
            return False
        if outcome:
            self.note(f"{dim}:cooling:{s['attempt']}", "wait",
                      f"the approver turned down '{s['attempt']}'; asking once more after a day", {})
            return True
        return False

    def step(self, dim: str, s: dict) -> None:
        now, name = self.now, NAME[dim]
        sup = store.last_supervisor_edit(self.ctx.conn, self.ctx.run, self.wid, dim)
        if s["phase"] == "supervisor_owned" and sup is not None and sup > s["since"]:
            if s.get("target"):  # R2-H1: they edited again: whatever is there now is theirs
                self.note(f"{dim}:supervisor_again:{sup}", "supervisor_owned",
                          f"a supervisor changed the {name} again at {store.sim_label(sup)}; we drop our clean-up of "
                          f"the old test split and leave it all to them", {"supervisor_minute": sup})
            s.update(since=sup, target=None)
        if sup is not None and s["phase"] != "supervisor_owned":
            # E14 (Kiran): the supervisor's choice stands for good; we never test this setting again.
            # Re-testing after 24 h (the first version) split calls away from a supervisor's good voice
            # in scenario 02 and was graded harmful. Calls so far stay in the scoreboard (they happened).
            was, ours, control = s["phase"], s.get("target"), s.get("control")
            s.clear()
            s.update(phase="supervisor_owned", since=sup)
            if ours and control and len(ours) > 1 and same(self.current(dim), ours):
                # R-M5: their edit left our split in `default` (e.g. they changed one cohort only).
                # Nobody would watch it any more, so it goes back to today's option, once.
                s.update(leftover=ours, target={control: 1.0}, attempt=f"end {name} test after supervisor edit",
                         urgent_off=[], impact=0.0, summary=f"End our {name} test (a supervisor changed the {name})",
                         rationale=f"a supervisor changed the {name}; our test split is still live and nobody would "
                                   f"watch it, so it goes back to {control} (E14)",
                         dry_run={"split_after": {control: 1.0}, "split_now": ours}, evidence={"supervisor_minute": sup})
            self.note(f"{dim}:supervisor:{sup}", "supervisor_owned",
                      f"a supervisor changed the {name} at {store.sim_label(sup)}"
                      + (f" while we were {'testing' if was == 'testing' else 'watching a promoted'} {name}s"
                         if was in ("testing", "promoted") else "")
                      + f"; their choice stands for good. We stop our {name} work and never propose a {name} "
                        f"test on this workflow again (E14)", {"supervisor_minute": sup, "phase_before": was})
        if s["phase"] == "idle":
            self.maybe_start(dim, s)
        elif s["phase"] == "testing":
            self.judge_test(dim, s)
        elif s["phase"] == "promoted":
            self.watch(dim, s)

    def current(self, dim: str) -> dict[str, float]:
        return config_ops.distribution(self.cfg, "default", dim)

    # ------------------------------------------------------------ idle -> testing
    def maybe_start(self, dim: str, s: dict) -> None:
        if self.ctx.gov is not None and dim in self.ctx.gov.held_dims(self.wid):
            return  # G21: the gate is still judging a supervisor's choice
        cur = self.current(dim)
        opts = self.wf["options"][dim]
        control = max(sorted(cur), key=cur.get) if cur else opts[0]
        arms = [control] + [o for o in opts if o != control and o not in self.retired(s)]
        if len(arms) < 2:
            return
        self.start_test(dim, s, control, arms)

    @staticmethod
    def retired(s: dict) -> set[str]:
        """Options never tested again: faded winners (E13) and options stopped as harmful (R-M2)."""
        return set(s.get("faded", [])) | set(s.get("harmed", []))

    def start_test(self, dim: str, s: dict, control: str, arms: list[str]) -> None:
        name, k = NAME[dim], self.k
        split = test_split(arms, control, k["control_share"])
        others = [o for o in arms if o != control]
        shares = ", ".join(f"{o} {v:.0%}" for o, v in split.items())
        per_day = {o: round(self.answered_24h * v) for o, v in split.items()}
        s.pop("evidence_since", None)
        s.update(phase="testing", control=control, arms=arms, stopped={}, start=self.now, live=None, narrowed=False,
                 target=split, urgent_off=[], attempt=f"test {name} {'/'.join(arms)}",
                 impact=0.0, champion=None, promo=None)
        s["summary"] = f"Test {name}s {', '.join(arms)} side by side ({shares}); {control} is today's {name}"
        s["rationale"] = (f"We have no calls on {', '.join(others)} for this workflow, so the only way to know whether "
                          f"they keep more people on the call is to try them on real calls at the same time (E3). "
                          f"The winner is chosen by statistics only, once a day: {k['min_calls']}+ answered calls and "
                          f"{k['win_z']} luck units better than {control} (E1, E2). An option clearly worse "
                          f"({k['harm_z']} luck units) is stopped at once (E5).")
        s["dry_run"] = {"split": split, "answered_calls_per_day_at_this_split": per_day,
                        "expected_engagement_change": "unknown until tested: no history exists for "
                                                      + ", ".join(others),
                        "worst_case_exposure": f"an option worse than {control} reaches its share of calls until "
                                               f"{k['harm_min_calls']} answered calls show it ({k['harm_z']} luck units)"}
        s["evidence"] = {"arms": arms, "control": control, "split": split}
        self.note(f"{dim}:start:{self.now}", "test", s["summary"], {"arms": arms, "split": split})

    # ------------------------------------------------------------ testing
    def judge_test(self, dim: str, s: dict) -> None:
        k, name, c, now = self.k, NAME[dim], s["control"], self.now
        cur = self.current(dim)
        if s.get("live") is None and same(cur, s["target"]):
            s["live"] = now
            self.note(f"{dim}:live:{s['start']}", "live", f"{name} test is live: {cur}", {"split": cur})
        st = strata(store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim, since=s["start"] // 60 * 60))
        active = [o for o in s["arms"] if o not in s["stopped"]]
        # E5: harm stop, every step
        bad = []
        for o in active:
            if o == c:
                continue
            x = compare(st, c, o)
            if x.n_b >= k["harm_min_calls"] and x.z <= -k["harm_z"]:
                s["stopped"][o] = {"minute": now, **lift_dict(x)}
                s["harmed"] = sorted(set(s.get("harmed", [])) | {o})
                bad.append((o, x))
        if bad:
            active = [o for o in active if o not in s["stopped"]]
            removed = sum(cur.get(o, 0.0) for o, _ in bad)
            s.update(target=test_split(active, c, k["control_share"]),
                     urgent_off=sorted(set(s["urgent_off"]) | {o for o, _ in bad}),
                     attempt=f"stop {name} {'/'.join(o for o, _ in bad)}",
                     impact=round(min(sum(max(-x.diff, 0) * cur.get(o, 0.0) for o, x in bad), 1.0), 3))
            why = "; ".join(f"{o}: {pct(x.rate_b)} of answered calls engaged vs {pct(x.rate_a)} on {c} "
                            f"({x.n_b} vs {x.n_a} calls, {-x.z:.1f} luck units worse; we stop at {k['harm_z']})"
                            for o, x in bad)
            s["summary"] = f"Stop {name} {', '.join(o for o, _ in bad)} in the test (clearly worse than {c})"
            s["rationale"] = f"{why} (E5)"
            s["dry_run"] = {"share_taken_off": round(removed, 3), "split_after": s["target"],
                            "per_option": {o: lift_dict(x) for o, x in bad}}
            s["evidence"] = {"stopped": {o: lift_dict(x) for o, x in bad}}
            self.note(f"{dim}:stop:{'/'.join(o for o, _ in bad)}:{s['start']}", "stop", f"{s['summary']}: {why}",
                      s["evidence"])
            if len(active) == 1:
                self.end_test(dim, s, f"every new {name} was clearly worse than {c}", urgent=True)
            return
        # E2: winners once a day, in the morning
        day = now // MINUTES_PER_DAY
        if now % MINUTES_PER_DAY < k["judge_hour"] * 60 or s.get("judged_day") == day:
            return
        s["judged_day"] = day
        res = {o: compare(st, c, o) for o in active if o != c}
        ready = {o: x for o, x in res.items() if min(x.n_a, x.n_b) >= k["min_calls"]}
        winners = sorted((o for o, x in ready.items() if x.z >= k["win_z"]), key=lambda o: -res[o].diff)
        failed, fading = set(), set()

        def is_fading(o: str) -> bool:
            """E15: E13's own fading bar, on the last `recent_hours`: recent lead under half the whole
            test's. Needs `decay_min_calls` a side (a quiet day proves nothing)."""
            recent = self.recent_lift(dim, s, c, o)
            if not self.fading_bar(recent, res[o].diff):
                return False
            fading.add(o)
            self.note(f"{dim}:fading:{o}:{day}", "not_promoted",
                      f"{name} {o} leads {c} over the whole test ({res[o].diff:+.1%}, {res[o].z:.1f} luck units) "
                      f"but over the last {k['recent_hours']} h only {recent.diff:+.1%} ({recent.n_b} vs "
                      f"{recent.n_a} calls), under half of that: it may already be fading, so it gets no more calls "
                      f"today; judged again tomorrow (E15)", {"whole": lift_dict(res[o]), "recent": lift_dict(recent)})
            return True

        fresh_since = s.get("evidence_since")
        if fresh_since:  # E17: only calls made after a turned-down promotion count for promoting/narrowing
            fresh_st = strata(store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim,
                                                  since=fresh_since // 60 * 60 + 60))
        for w in winners:  # best first; one failing a guardrail or the group check can't be promoted
            if self.ctx.gov is not None and self.ctx.gov.attempt_blocked(self.intent, f"promote {name} {w}", now):
                failed.add(w)  # R4-L: still cooling down from the approver's "no"
                continue
            if fresh_since:
                f = compare(fresh_st, c, w)
                if min(f.n_a, f.n_b) < k["min_calls"] or f.z < k["win_z"]:
                    failed.add(w)
                    self.note(f"{dim}:not_fresh:{w}:{day}", "not_promoted",
                              f"{name} {w} leads over the whole test, but since the approver turned its promotion "
                              f"down ({store.sim_label(fresh_since)}) it has {f.diff:+.1%} on {f.n_b} vs {f.n_a} calls "
                              f"({f.z:.1f} luck units; it needs {k['win_z']} on {k['min_calls']}+ new calls) (E17)",
                              {"fresh": lift_dict(f)})
                    continue
            if is_fading(w):
                continue
            failed.add(w)
            problems = self.guardrails(st, c, w)
            if problems:
                self.note(f"{dim}:guard:{w}:{day}", "not_promoted",
                          f"{name} {w} beats {c} on engagement but fails a guardrail: {'; '.join(problems)} (E6)",
                          {"problems": problems})
                continue
            carve, hurt = self.group_check(dim, s, c, w)
            if carve is None:
                self.note(f"{dim}:uneven:{w}:{day}", "not_promoted",
                          f"{name} {w} beats {c} overall but is clearly worse for {len(hurt)} customer groups "
                          f"({', '.join(hurt)}); more than {k['max_carve_outs']}, so it is too uneven to promote "
                          f"(E12)", {"hurt": hurt})
                continue
            self.promote(dim, s, c, w, res[w], carve)
            return
        base = compare(st, c, c)  # control's own totals
        # E9: a narrowed test (one leader vs today's option) runs on until it could find half the lift
        need = stats.calls_needed(base.rate_a or 0.5, k["mde"] / (2 if s.get("narrowed") else 1), k["win_z"])
        status = "; ".join(f"{o}: {pct(x.rate_b)} vs {pct(x.rate_a)} on {x.n_b} calls ({x.z:+.1f} luck units)"
                           for o, x in res.items())
        # E15 / R2-M3: a leader that may be fading gets no more calls, and nothing is decided about it
        # today (a day's wait, never a loss), except that a narrowed test that has had all its calls
        # ends instead of keeping a fading leader at 50% (R3-M).
        if fresh_since:  # E17: a leader is narrowed onto only if it also leads on the new calls
            fresh = {o: compare(fresh_st, c, o) for o in res}
            if any(min(x.n_a, x.n_b) < need for x in fresh.values()):
                # R4-M3: too few new calls to end or narrow fairly; the test goes on
                self.note(f"{dim}:fresh_wait:{day}", "no_winner_yet",
                          f"{name} test continues: after a turned-down promotion every decision waits for "
                          f"{need} new calls per option ({', '.join(f'{o}: {x.n_b}' for o, x in fresh.items())})", {})
                return
            failed |= {o for o, x in fresh.items() if x.z < k["lead_z"]}
        if fading or any(is_fading(o) for o, x in res.items() if x.z >= k["lead_z"] and o not in failed):
            if s.get("narrowed") and res and all(x.n_b >= need for x in res.values()):
                self.end_test(dim, s, f"the option that was ahead may be fading ({status})")
            return
        if s.get("narrowed") and res and all(x.z < k["lead_z"] for x in res.values()):
            self.end_test(dim, s, f"the option that was ahead fell back ({status})")
            return
        if s.get("narrowed") and res and set(res) <= failed:  # R3-L1: it can never be promoted
            self.end_test(dim, s, f"the option that was ahead failed a guardrail or the group check ({status})")
            return
        if res and all(x.n_b >= need for x in res.values()):  # R-L2: incl. winners that failed a check
            leaders = sorted((o for o, x in res.items() if x.z >= k["lead_z"] and o not in failed),
                             key=lambda o: -res[o].z)  # R2-M2: never double a failed winner's exposure
            if leaders and not s.get("narrowed"):
                self.narrow(dim, s, leaders[0], res[leaders[0]], status)
                return
            self.end_test(dim, s, f"no clear winner after {need}+ answered calls per option, enough to find a "
                                  f"{k['mde'] / (2 if s.get('narrowed') else 1):.0%} lift ({status})")
            return
        self.note(f"{dim}:day:{day}:{s['start']}", "no_winner_yet",
                  f"{name} test, day {day - s['start'] // MINUTES_PER_DAY + 1}: no winner yet ({status}); a winner "
                  f"needs {k['min_calls']}+ calls and {k['win_z']} luck units; the test ends without one at "
                  f"{need} calls per option", {o: lift_dict(x) for o, x in res.items()})

    def guardrails(self, st: dict, c: str, w: str) -> list[str]:
        """E6: the guardrail KPI must not be clearly worse by more than the policy's margin
        (an absolute drop), and made-up calls must not be clearly up."""
        problems = []
        for key, margin in ((self.wf.get("objective") or {}).get("guardrail") or {}).items():
            metric = key.removesuffix("_max_drop")
            if metric not in METRIC:
                continue  # business outcome is per lead, not per call: can't be split by option
            x = compare(st, c, w, metric)
            if x.diff < -margin and x.z <= -self.k["guard_z"]:
                problems.append(f"{metric.replace('_', ' ')} {pct(x.rate_b)} vs {pct(x.rate_a)} "
                                f"(more than the allowed {margin:.0%} drop)")
        x = compare(st, c, w, "made_up")
        if x.z >= self.k["guard_z"]:
            problems.append(f"made-up calls {pct(x.rate_b)} vs {pct(x.rate_a)} ({x.z:.1f} luck units more)")
        return problems

    def group_check(self, dim: str, s: dict, c: str, w: str) -> tuple[dict | None, list[str]]:
        """E12: the winner, group by group (one feature at a time). A group where it is clearly worse
        keeps today's option. Returns ({group: numbers} to carve out, or None if too many, names hurt)."""
        k = self.k
        rows = store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim, since=s["start"] // 60 * 60,
                                   segments=True)
        groups: dict = defaultdict(dict)
        for key, opts in strata(rows, ("feature", "value")).items():
            groups[key[-2:]][key[:-2]] = opts
        hurt = {}
        for (f, v), st in sorted(groups.items()):
            if v in ("?", ""):
                continue
            x = compare(st, c, w)
            if min(x.n_a, x.n_b) >= k["segment_min_calls"] and x.z <= -k["segment_harm_z"]:
                hurt[f"{f}={v}"] = {"feature": f, "value": v, **lift_dict(x)}
        if len(hurt) > k["max_carve_outs"]:
            return None, sorted(hurt)
        allocs = self.cfg.get("allocations") or {}
        others = {carve_id(g["feature"], g["value"]) for d, o in self.dims.items() if d != dim
                  for g in (o.get("carve") or {}).values()}
        others |= {x["id"] for x in self.cfg.get("cohorts", []) if x["id"].startswith(CARVE_PREFIX)
                   and any(d != dim for d in (allocs.get(x["id"]) or {}))}  # R2-L2: kept for a supervisor
        room = MAX_COHORTS - len([x for x in self.cfg.get("cohorts", []) if not x["id"].startswith(CARVE_PREFIX)])
        mine = {carve_id(g["feature"], g["value"]) for g in hurt.values()}
        if len(mine | others) > room:  # R-L1
            return None, sorted(hurt)
        return hurt, sorted(hurt)

    def promote(self, dim: str, s: dict, c: str, w: str, x: stats.Lift, carve: dict) -> None:
        k, name = self.k, NAME[dim]
        h = k["holdout_share"]
        cur = self.current(dim)
        s["test_split"] = s.get("target")
        s.pop("evidence_since", None)
        # E16: a canary first step (the winner's current share + `first_step_add`); the main share only
        # after a day's re-check. A fade that starts right after promotion then reaches few extra calls.
        first = min(cur.get(w, 0.0) + k["first_step_add"], 1 - h)
        two = first < 1 - h - EPS and not s.get("narrowed")  # R4-M4: a narrowed 50/50 test already was step 1
        target = {w: round(first, 6), c: round(1 - first, 6)} if two else {w: round(1 - h, 6), c: round(h, 6)}
        s.update(phase="promoted", champion=w, promo=lift_dict(x), carve=carve, promoted=self.now, live=None,
                 target=target, promo_step=1 if two else 2, urgent_off=[], attempt=f"promote {name} {w}",
                 impact=round(min(max(x.diff, 0) * max(target[w] - cur.get(w, 0.0), 0), 1.0), 3))
        groups = ", ".join(carve)
        s["summary"] = ((f"Give {name} {w} {first:.0%} of calls (step 1 of 2): " if two else
                         f"Make {name} {w} the main one ({1 - h:.0%}): ")
                        + f"{pct(x.rate_b)} of answered calls engaged vs {pct(x.rate_a)} on {c}; "
                        + (f"it goes to {1 - h:.0%} tomorrow only if still ahead over the last {k['recent_hours']} h"
                           if two else f"{c} keeps {h:.0%} so we notice if {w} fades")
                        + (f"; {groups} keep {c} ({w} did worse for them)" if carve else ""))
        s["rationale"] = (f"{name} {w}: {x.hits_b}/{x.n_b} engaged vs {x.hits_a}/{x.n_a} on {c} over the test, "
                          f"compared hour by hour: +{x.diff:.1%} ({x.z:.1f} luck units; we need {k['win_z']} because "
                          f"we compare several options every day, E1). Guardrails held (E6). Customer groups checked "
                          f"one by one (E12)" + (f": {groups} did worse with {w} and keep {c}" if carve else
                                                 ": none did clearly worse"))
        gain = x.diff * (1 - h - cur.get(w, 0.0)) * self.answered_24h
        s["dry_run"] = {"replayed_on": "this test's own calls", "winner": lift_dict(x),
                        "answered_calls_last_24h": self.answered_24h,
                        "extra_engaged_per_day_at_new_split": round(gain), "carve_outs": carve}
        s["evidence"] = {"winner": w, "control": c, "lift": lift_dict(x), "carve_outs": carve}
        self.note(f"{dim}:promote:{w}:{s['start']}", "promote", s["summary"], s["evidence"])

    def narrow(self, dim: str, s: dict, w: str, x: stats.Lift, status: str) -> None:
        """E9 (Kiran): no proven winner, but one option is ahead. Drop the rest; it runs 50/50 against
        today's option (twice the calls it had), so a real win shows up instead of being thrown away."""
        name, c = NAME[dim], s["control"]
        s.update(arms=[c, w], narrowed=True, target={c: 0.5, w: 0.5}, attempt=f"narrow {name} test to {c}/{w}",
                 impact=0.0, urgent_off=[])
        s["summary"] = f"Narrow the {name} test to {w} vs {c} (half the calls each); drop the others"
        s["rationale"] = (f"No option proved itself ({status}), but {w} is ahead by {x.diff:+.1%} ({x.z:.1f} luck units; "
                          f"a winner needs {self.k['win_z']}). The others are dropped; {w} keeps running against {c} "
                          f"until it proves itself or falls back below {self.k['lead_z']} (E9).")
        s["dry_run"] = {"split_after": s["target"], "leader": lift_dict(x)}
        s["evidence"] = {"leader": w, "lift": lift_dict(x)}
        self.note(f"{dim}:narrow:{w}:{s['start']}", "narrow", f"{s['summary']}: {s['rationale']}", s["evidence"])

    def fading_bar(self, recent: stats.Lift, lead: float) -> bool:
        """E15, E13's own bar: over the last `recent_hours` the lead is under half the claimed lead.
        Needs `decay_min_calls` a side: a quiet day proves nothing (R2-M2)."""
        return min(recent.n_a, recent.n_b) >= self.k["decay_min_calls"] and recent.diff < lead / 2

    def recent_lift(self, dim: str, s: dict, c: str, o: str) -> stats.Lift:
        since = max(s.get("start", 0), self.now - self.k["recent_hours"] * 60) // 60 * 60
        return compare(strata(store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim, since=since)), c, o)

    def recheck_pending(self, dim: str, s: dict) -> None:
        """E15: a promotion not yet live (turned down once and waiting, or in flight) is re-judged every
        morning on the last `recent_hours`. If its recent lead is under half its claimed lead (E13's own
        fading bar) it may be fading, so the
        promotion is withdrawn and the test goes on (public_02: W3 won on D2, was turned down, and
        the retry on D4 would have promoted it after it had faded)."""
        k, now, name = self.k, self.now, NAME[dim]
        day = now // MINUTES_PER_DAY
        if (now % MINUTES_PER_DAY < k["judge_hour"] * 60 or s.get("rechecked_day") == day
                or day <= s["promoted"] // MINUTES_PER_DAY):
            return
        s["rechecked_day"] = day
        w, c = s["champion"], s["control"]
        recent = self.recent_lift(dim, s, c, w)
        if not self.fading_bar(recent, s["promo"]["diff"]):
            return
        cur = s.get("test_split") or self.current(dim)  # R2-L1: back to the test split, not a half-step
        s.update(phase="testing", target=cur, judged_day=day, champion=None, promo=None, carve=s.get("live_carve", {}),
                 attempt=f"test {name} {'/'.join(s['arms'])}", impact=0.0, urgent_off=[])
        s["summary"] = f"Keep testing {name}s ({w} is no longer ahead)"
        s["rationale"] = (f"{name} {w} was chosen to be promoted on {store.sim_label(s['promoted'])}, but over the last "
                          f"{k['recent_hours']} h it is {recent.diff:+.1%} vs {c} ({recent.n_b} vs {recent.n_a} calls): "
                          f"it may be fading, so the promotion is withdrawn before it goes live (E15)")
        s["dry_run"] = {"split_after": cur}
        s["evidence"] = {"recent": lift_dict(recent)}
        self.note(f"{dim}:promotion_withdrawn:{w}:{s['promoted']}", "promotion_withdrawn", s["rationale"],
                  s["evidence"])

    def end_test(self, dim: str, s: dict, why: str, urgent: bool = False) -> None:
        name, c = NAME[dim], s["control"]
        cur = self.current(dim)
        s.update(phase="done", target={c: 1.0},
                 attempt=f"end {name} test of {'/'.join(s['arms'])}", impact=0.0,
                 urgent_off=sorted(o for o in cur if o != c) if urgent else [])
        s["summary"] = f"End the {name} test; back to {c} for everyone"
        s["rationale"] = (f"{why} (E9). No automatic re-test: re-tests found nothing in our runs and once put "
                          f"calls back on a faded option. Options return only if a promoted winner fades (E13).")
        s["dry_run"] = {"split_after": {c: 1.0}, "split_now": cur}
        s["evidence"] = {"reason": why}
        self.note(f"{dim}:end:{s.get('start')}", "end_test", f"{s['summary']}: {why}", s["evidence"])

    # ------------------------------------------------------------ promoted: watch for fading (E13)
    def watch(self, dim: str, s: dict) -> None:
        k, name, now = self.k, NAME[dim], self.now
        w, c = s["champion"], s["control"]
        if s.get("live") is None:
            if not same(self.current(dim), s["target"]):
                self.recheck_pending(dim, s)  # E15: still waiting for the approver (or the steps)
                return
            s["live"], s["live_carve"] = now, s.get("carve", {})
            share = s["target"][w]
            self.note(f"{dim}:promoted_live:{w}:{now}", "live",
                      f"{name} {w} now has {share:.0%} of calls; {c} keeps {1 - share:.0%} to compare against", {})
        if s.get("step2_pending") and same(self.current(dim), s["target"]):
            # R4-M2: step 1 stayed live (and watched) while step 2 waited; now step 2 is live
            s.update(step2_pending=False, live=now)
            self.note(f"{dim}:step2_live:{w}:{now}", "live",
                      f"{name} {w} now has {s['target'][w]:.0%} of calls; {c} keeps {s['target'][c]:.0%}", {})
        since = max(s["live"] // 60 * 60 + 60, (now - k["decay_window_hours"] * 60) // 60 * 60)
        st = strata(store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim, since=since))
        x = compare(st, c, w)
        if x.n_a < k["decay_min_calls"]:
            return
        p = s["promo"]
        se = math.sqrt(p["se"] ** 2 + x.se ** 2)
        drop = (p["diff"] - x.diff) / se if se else 0.0
        harmful = x.n_b >= k["harm_min_calls"] and x.z <= -k["harm_z"]
        faded = drop >= k["decay_z"] and x.diff < p["diff"] / 2
        if not (harmful or faded):
            self.second_step(dim, s)
            return
        why = (f"{name} {w} now engages {pct(x.rate_b)} vs {pct(x.rate_a)} on {c} over the last "
               f"{(now - since) / 60:.0f} h ({x.n_b} vs {x.n_a} calls): a lead of {x.diff:+.1%}, against "
               f"{p['diff']:+.1%} when we promoted it"
               + (f" ({-x.z:.1f} luck units worse than {c})" if harmful else f" (fell {drop:.1f} luck units)"))
        self.rollback(dim, s, x, why, "is now worse" if harmful else "stopped working")

    def second_step(self, dim: str, s: dict) -> None:
        """E16: step 1 of a promotion is live (a canary share). Next morning, the winner goes
        to its main share only if, over the last `recent_hours` on the step-1 split, it keeps at least
        half its claimed lead (E15's bar). Otherwise it is rolled back, having only ever had half."""
        k, now, name = self.k, self.now, NAME[dim]
        day = now // MINUTES_PER_DAY
        if (s.get("promo_step") != 1 or day <= s["live"] // MINUTES_PER_DAY
                or now % MINUTES_PER_DAY < k["judge_hour"] * 60 or s.get("step2_day") == day):
            return
        w, c = s["champion"], s["control"]
        since = max(s["live"] // 60 * 60 + 60, (now - k["recent_hours"] * 60) // 60 * 60)
        recent = compare(strata(store.strata_counts(self.ctx.conn, self.ctx.run, self.wid, dim, since=since)), c, w)
        if min(recent.n_a, recent.n_b) < k["decay_min_calls"]:
            return  # not enough calls on the step-1 split yet: wait
        s["step2_day"] = day
        p = s["promo"]
        if self.fading_bar(recent, p["diff"]):
            self.rollback(dim, s, recent,
                          f"on its step-1 share for a day, {name} {w} led {c} by only {recent.diff:+.1%} over the last "
                          f"{(now - since) / 60:.0f} h ({recent.n_b} vs {recent.n_a} calls), under half of the "
                          f"{p['diff']:+.1%} it was promoted on", "may be fading", step2=True)
            return
        h = k["holdout_share"]
        s.update(promo_step=2, step2_pending=True, target={w: round(1 - h, 6), c: round(h, 6)},
                 attempt=f"promote {name} {w} step 2",
                 impact=round(min(max(p["diff"], 0) * max(1 - h - s["target"][w], 0), 1.0), 3))
        s["summary"] = (f"Make {name} {w} the main one ({1 - h:.0%}, step 2 of 2): still ahead of {c} on its "
                        f"step-1 share ({recent.diff:+.1%} over the last {(now - since) / 60:.0f} h); {c} keeps {h:.0%} so "
                        f"we notice if {w} fades")
        s["rationale"] = (f"step 1 gave {name} {w} a canary share; over the last {(now - since) / 60:.0f} h it led "
                          f"{c} {recent.diff:+.1%} ({recent.hits_b}/{recent.n_b} vs {recent.hits_a}/{recent.n_a}), at "
                          f"least half the {p['diff']:+.1%} it was promoted on, so it is not fading (E15, E16)")
        s["dry_run"] = {"split_after": s["target"], "recent": lift_dict(recent), "at_promotion": p}
        s["evidence"] = {"recent": lift_dict(recent), "at_promotion": p}
        self.note(f"{dim}:promote_step2:{w}:{s['live']}", "promote", s["summary"], s["evidence"])

    def rollback(self, dim: str, s: dict, x: stats.Lift, why: str, verb: str, step2: bool = False) -> None:
        """E13: move off a promoted option that faded (or turned harmful); it is retired for good."""
        k, name = self.k, NAME[dim]
        w, c, p = s["champion"], s["control"], s["promo"]
        s["faded"] = sorted(set(s.get("faded", [])) | {w})
        self.note(f"{dim}:faded:{w}:{s['live']}", "rollback",
                  f"{name} {w} {verb}: {why}. Moving off it for good"
                  + (" before it got the main share" if step2 else "")
                  + ", and testing the other options once more (E13, E16)", {"now": lift_dict(x), "at_promotion": p})
        opts = self.wf["options"][dim]
        others = [o for o in opts if o != c and o not in self.retired(s)]
        if others:
            self.start_test(dim, s, c, [c] + others)
            s["summary"] = f"Move off {name} {w} (it faded); back to {c} and test {', '.join(others)} again"
        else:
            s.update(phase="done", target={c: 1.0}, impact=0.0, rationale="",
                     dry_run={"split_after": {c: 1.0}}, evidence={})
            s["summary"] = f"Move off {name} {w} (it faded); back to {c} for everyone"
        s.update(urgent_off=[w], attempt=f"move off {name} {w}",
                 impact=round(min(max(-x.diff, 0) * (1 - k["holdout_share"]) + 0.01, 1.0), 3))
        s["rationale"] = f"{why}. A winner that fades is rolled back (E13). " + s.get("rationale", "")
        s["evidence"] = {**s.get("evidence", {}), "faded": w, "now": lift_dict(x), "at_promotion": p}

    # ------------------------------------------------------------ the changes for this workflow
    def change(self, dims: dict, frozen: set) -> list[Change]:
        # R2-H1: a leftover clean-up stays ours until it reaches its target (auto steps move `current`
        # away from our old split after the first step); a newer supervisor edit clears it in step().
        managed = {d: s for d, s in dims.items() if s.get("target") and d not in frozen}
        urgent = {d for d, s in managed.items() if self.urgent(d, s)}
        out = []
        if urgent:  # R-H2: never waits behind a routine request that is still out
            patch = {"allocations": {"default": {d: managed[d]["target"] for d in sorted(urgent)}}}
            out.append(self._make(f"{self.intent}:urgent", patch, sorted(urgent), dims, urgent=True))
        routine = self._routine_patch(dims, {d: s for d, s in managed.items() if d not in urgent})
        if routine:
            patch, touched = routine
            out.append(self._make(self.intent, patch, touched, dims, urgent=False))
        return out

    def _routine_patch(self, dims: dict, managed: dict) -> tuple[dict, list[str]] | None:
        cfg = self.cfg
        allocs = cfg.get("allocations") or {}
        touched: set[str] = set()
        alloc_patch: dict = {}
        for d, s in managed.items():
            if not same(self.current(d), s["target"]):
                alloc_patch.setdefault("default", {})[d] = s["target"]
                touched.add(d)
        # E12 carve-outs: our cohorts, rebuilt from what each dimension wants; others' cohorts untouched
        existing = list(cfg.get("cohorts") or [])
        ours = {x["id"]: x for x in existing if x["id"].startswith(CARVE_PREFIX)}
        want: dict = defaultdict(dict)
        where: dict = {}
        for d in DIMS:
            s = dims.get(d)
            if d in managed and s["phase"] != "supervisor_owned":  # R2-H2: theirs: never touch the cohorts
                for g in (s.get("carve") or {}).values():
                    cid = carve_id(g["feature"], g["value"])
                    want[cid][d] = {s["control"]: 1.0}
                    where[cid] = {g["feature"]: [g["value"]]}
            else:  # not ours to change right now: keep exactly what is there
                for cid, x in ours.items():
                    if (allocs.get(cid) or {}).get(d):
                        want[cid][d] = allocs[cid][d]
                        where[cid] = x["where"]
        cohorts = [x for x in existing if not x["id"].startswith(CARVE_PREFIX)]
        cohorts += [{"id": cid, "where": where[cid]} for cid in sorted(want)]
        for cid in sorted(set(ours) | set(want)):
            have = allocs.get(cid) or {}
            diff = {}
            for d in DIMS:
                a, b = have.get(d), want.get(cid, {}).get(d)
                if (a or b) and not (a and b and same(a, b)):
                    diff[d] = b  # None removes it
                    touched.add(d)
            if cid not in want and have:
                diff = None  # the whole cohort goes
            if diff is None or diff:
                alloc_patch[cid] = diff
        patch: dict = {}
        if alloc_patch:
            patch["allocations"] = alloc_patch
        if cohorts != existing:
            patch["cohorts"] = cohorts
        touched &= set(managed)
        return (patch, [d for d in DIMS if d in touched]) if patch and touched else None

    def _make(self, intent: str, patch: dict, touched: list[str], dims: dict, urgent: bool) -> Change:
        parts = [dims[d] for d in touched]
        shift = max([config_ops.tv_distance(self.current(d), dims[d]["target"]) for d in touched] + [0.0])
        affected = int(self.ctx.leads_called_24h(self.wid) * max(shift, 0.05 if "cohorts" in patch else 0.0))
        return Change(
            intent_key=intent, workflow_id=self.wid, urgent=urgent, patch=patch,
            summary="; ".join(s["summary"] for s in parts), rationale=" | ".join(s["rationale"] for s in parts),
            affected_leads=affected, impact_score=round(min(sum(s.get("impact", 0.0) for s in parts), 1.0), 3),
            dry_run={NAME[d]: dims[d]["dry_run"] for d in touched},
            evidence={NAME[d]: dims[d]["evidence"] for d in touched},
            ttl_min=self.ctx.settings.gate("change_ttl_minutes"),
            attempts=[dims[d]["attempt"] for d in touched])
