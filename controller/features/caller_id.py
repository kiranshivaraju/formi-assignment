"""Caller-ID health (C1-C10): stop dialling from a phone number people have stopped answering.

Each loop step, per workflow:
  1. Judge every number in use (C1-C5). With 2+ numbers: compare each with the others over the same
     recent hours. With one number: compare it with its own past at the same hours of day, unless
     other workflows dropped just as much (then it is not the number).
  2. Move each number through its life (kept in feature_state, every move written to the diary):
         flagged -> removing -> removed -> probing -> restoring -> (healthy again, forgotten)
                                   ^           |            |
                                   +-----------+------------+   bad again: remove, wait twice as long
  3. Work out the caller-ID split we want (C6, C10) and, if it differs from the current config,
     return ONE change for the workflow carrying that whole split.

Load spreading (L1-L8 in notes.md): a number wears out when it carries everything, and rest restores it.
So calls are spread evenly over every healthy number. A never-used spare (no calls anywhere in our
records) first gets a trial share (20% shared between the spares), judged like any trial; it passes into
the equal split or is excluded for good. No spreading on a workflow whose caller IDs a supervisor edited.
Spare life:  spare_trial -> (passed: an ordinary number, equal share) | spare_failed | spare_declined One change per workflow, not per
     number, because two changes to the same split would overwrite each other in the gate.

Knobs come from settings.yaml (`caller_id_health`); numbers below quote the defaults.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from .. import config_ops, stats, store
from ..governance import Change
from ..store import MINUTES_PER_DAY

FEATURE = "caller_id_health"
DIM = "caller_id"
PAST_DAYS = 14   # C4: how far back a single number's "own past" reaches
EPS = 1e-6
NOT_HEALTHY = ("removing", "probing", "restoring", "spare_trial", "spare_failed", "spare_declined")  # never part of the "healthy numbers" reference (M3)
log = logging.getLogger(__name__)
TRIAL_MAX_CALLS = 4  # C10: a trial still unclear after 4 x min_calls counts as failed


@dataclass
class Verdict:
    number: str
    basis: str            # what it was compared with
    hours: float          # window length actually used
    calls: int
    answered: int
    ref_calls: int
    ref_answered: int
    rate: float
    ref_rate: float
    worse_by: float       # relative: 0.6 = answers 60% less often
    z: float              # luck units
    bad: bool
    note: str = ""
    pass_z: float = 0.0   # luck units clear of the 30% line; a trial passes at k["pass_z"] (C10)


# ---------------------------------------------------------------- config helpers

def splits(cfg: dict) -> dict[str, dict[str, float]]:
    """Caller-ID split of every cohort that sets its own, plus default."""
    allocs = cfg.get("allocations") or {}
    owners = ["default"] + sorted(c for c, d in allocs.items() if c != "default" and (d or {}).get(DIM))
    return {c: config_ops.distribution(cfg, c, DIM) for c in owners}


def in_use(sp: dict[str, dict[str, float]]) -> list[str]:
    return sorted({n for d in sp.values() for n, v in d.items() if v > EPS})


def _scaled(dist: dict[str, float], number: str, share: float) -> dict[str, float]:
    """Give `number` exactly `share`, shrinking the others in proportion."""
    rest = {o: v for o, v in dist.items() if o != number}
    total = sum(rest.values())
    if not total:
        return {number: 1.0}
    return {**{o: v * (1 - share) / total for o, v in rest.items()}, number: share}


def _same(a: dict[str, float], b: dict[str, float]) -> bool:
    return config_ops.tv_distance(a, b) < 1e-3


# ---------------------------------------------------------------- judging (C1-C5)

def _totals(rows: list[dict], since: int, options: set[str] | None = None) -> tuple[int, int]:
    t = store.sum_rows(rows, since=since, options=options)
    return t["dials"], t["connected"]


def _verdict(number, basis, hours, nd, nc, rd, rc, k, note="") -> Verdict:
    rate, ref = nc / nd, rc / rd
    worse = 1 - rate / ref if ref else 0.0
    z = stats.z_score(rc, rd, nc, nd)
    return Verdict(number, basis, hours, nd, nc, rd, rc, rate, ref, worse, z,
                   worse >= k["min_relative_drop"] and z >= k["min_z"], note,
                   stats.clearly_not_worse(rc, rd, nc, nd, k["min_relative_drop"]))


def clearly_fine(v: Verdict, k) -> bool:
    """Evidence it is healthy, not just a lack of evidence it is bad (H3: a trial is looked at every
    15 minutes, so a point estimate dipping under the line once would pass dead numbers often)."""
    return v.pass_z >= k["pass_z"]


def _window_start(now: int, hours: int) -> int:
    return (now - hours * 60) // 60 * 60


def judge_against_others(rows, now, number, others, k) -> Verdict | None:
    """C1-C3: the smallest window (3, 6, 12, 24 h) with enough calls on both sides."""
    for h in k["window_hours"]:
        since = _window_start(now, h)
        nd, nc = _totals(rows, since, {number})
        od, oc = _totals(rows, since, set(others))
        if nd >= k["min_calls"] and od >= k["min_calls"]:
            return _verdict(number, f"{', '.join(others)}", h, nd, nc, od, oc, k)
    return None


def judge_since(rows, now, since, number, others, k) -> Verdict | None:
    """C10: a number on trial is judged only on calls since its trial started."""
    since = since // 60 * 60 + 60  # whole hours only after the trial went live
    nd, nc = _totals(rows, since, {number})
    od, oc = _totals(rows, since, set(others))
    if nd >= k["min_calls"] and od >= k["min_calls"]:
        return _verdict(number, f"{', '.join(others)} since the trial began", round((now - since) / 60, 1),
                        nd, nc, od, oc, k)
    return None


def judge_against_past(ctx, wid, rows, now, number, k) -> Verdict | None:
    """C4 + C5: a workflow's only number against its own past at the same hours of day."""
    for h in k["window_hours"]:
        since = _window_start(now, h)
        nd, nc = _totals(rows, since, {number})
        if nd >= k["min_calls"]:
            break
    else:
        return None
    hours = sorted({r["start"] % MINUTES_PER_DAY // 60 for r in rows if r["start"] >= since and r["option"] == number})
    past = store.hourly_counts(ctx.conn, ctx.run, since=since - PAST_DAYS * MINUTES_PER_DAY, until=since,
                               workflow_id=wid, by=DIM, hours=hours)
    pd, pc = _totals(past, -10**9, {number})
    if pd < k["min_calls"] or not pc:
        return None
    v = _verdict(number, "its own past at the same hours", h, nd, nc, pd, pc, k)
    if v.bad:  # C5: did everyone else drop too? Then it is the day, not the number.
        cur = store.hourly_counts(ctx.conn, ctx.run, since=since, exclude_workflow=wid, hours=hours)
        old = store.hourly_counts(ctx.conn, ctx.run, since=since - PAST_DAYS * MINUTES_PER_DAY, until=since,
                                  exclude_workflow=wid, hours=hours)
        cd, cc = _totals(cur, -10**9)
        od, oc = _totals(old, -10**9)
        if cd >= k["min_calls"] and od >= k["min_calls"] and oc:
            others_worse = 1 - (cc / cd) / (oc / od)
            if others_worse >= v.worse_by / 2:
                v.bad = False
                v.note = f"other workflows dropped {others_worse:.0%} at the same hours: a shared drop, not the number"
    return v


# ---------------------------------------------------------------- the feature

def self_change_allowed(ctx, wid: str) -> bool:
    auto = ctx.workflows[wid]["governance"].get("auto_apply") or {}
    return bool(auto.get("enabled")) and DIM in auto.get("allowed_dims", [])


def pick_spare(ctx, wid, rows, avoid: set[str], k) -> str | None:
    """Spare rule: an unused offered number, the best-answering one if we have seen it, else the first."""
    offered = [o for o in ctx.workflows[wid]["options"].get(DIM, []) if o not in avoid]
    if not offered:
        return None
    seen = []
    for o in offered:
        d, c = _totals(rows, -10**9, {o})
        if d >= k["min_calls"]:
            seen.append((c / d, o))
    return max(seen)[1] if seen else offered[0]


def check(ctx) -> list[Change]:
    out = []
    for wid in ctx.workflows:
        try:
            change = check_workflow(ctx, wid)
        except Exception as e:  # one workflow's problem must not skip the others (L2)
            if type(e).__module__.startswith(("httpx", "psycopg")):
                raise
            log.exception("caller-ID check failed for %s at sim minute %s", wid, ctx.now)
            ctx.keep.add(f"{FEATURE}:{wid}")  # couldn't judge: don't withdraw what it asked for before
            ctx.diary(f"feature_error:{FEATURE}:{wid}:{ctx.now // 60}", wid, "feature_error", "skipped",
                      f"caller-ID check raised {type(e).__name__}: {e}; skipped this step", {"feature": FEATURE})
            continue
        if change:
            out.append(change)
    return out


def check_workflow(ctx, wid: str) -> Change | None:
    k = ctx.settings.feature(FEATURE, wid)
    now = ctx.now
    cfg = ctx.config(wid)["config"]
    sp = splits(cfg)
    using = in_use(sp)
    skey = f"{FEATURE}:{wid}"
    state = store.get_state(ctx.conn, ctx.run, skey)
    nums: dict[str, dict] = dict(state.get("numbers", {}))
    since = min([_window_start(now, max(k["window_hours"]))]
                + [s["live_minute"] for s in nums.values() if s.get("live_minute") is not None])
    rows = store.hourly_counts(ctx.conn, ctx.run, since=since, workflow_id=wid, by=DIM)
    notes: list[tuple[str, str, str, dict]] = []  # (key, action, reason, inputs) for the diary

    def note(key, action, reason, inputs=None):
        notes.append((f"{FEATURE}:{wid}:{key}", action, reason, inputs or {}))

    # ---- 1. judge every number in use
    # R2-M1: spares on trial with no ordinary healthy number left to compare against carry the calls
    # alone; they become ordinary numbers, judged like any other (against each other or their past).
    if not any(nums.get(o, {}).get("status") not in NOT_HEALTHY for o in using):
        for o in [o for o in using if nums.get(o, {}).get("status") == "spare_trial"]:
            del nums[o]
            note(f"spare_carrier:{o}:{now}", "spread", f"spare caller ID {o} now carries calls with no healthy number "
                 f"to compare it against; it is judged as an ordinary number from now on", {"number": o})
    verdicts: dict[str, Verdict] = {}
    for n in using:
        s = nums.get(n, {})
        others = [o for o in using if o != n and nums.get(o, {}).get("status") not in NOT_HEALTHY]
        if s.get("status") in ("probing", "spare_trial"):  # judged on trial calls only, once live
            live = s.get("live_minute")
            v = judge_since(rows, now, live, n, others, k) if others and live is not None else None
        elif others:
            v = judge_against_others(rows, now, n, others, k)
        else:
            v = judge_against_past(ctx, wid, rows, now, n, k)
        if v:
            verdicts[n] = v
            if v.note and not v.bad:
                note(f"shared:{n}:{now // MINUTES_PER_DAY}", "no_action", f"caller ID {n}: {v.note}", asdict(v))

    # ---- 2. move each number through its life
    sup = store.last_supervisor_edit(ctx.conn, ctx.run, wid, DIM)
    spreading = k["spread_spares"] and sup is None
    for n, v in verdicts.items():
        s = nums.get(n)
        if s is not None and s["status"] == "spare_trial":  # L2: a never-used spare on trial
            fine = clearly_fine(v, k)
            if v.bad or (not fine and v.calls >= TRIAL_MAX_CALLS * k["min_calls"]):
                nums[n] = {"status": "spare_failed", "flagged_minute": now, "verdict": asdict(v)}
                note(f"spare_failed:{n}", "remove",
                     f"spare caller ID {n} failed its trial: {v.rate:.1%} vs {v.ref_rate:.1%} on {v.calls} trial calls "
                     f"({v.pass_z:.1f} luck units clear of the {k['min_relative_drop']:.0%} line, needs {k['pass_z']}); "
                     f"taking it out and not using it again", asdict(v))
            elif fine:
                del nums[n]
                note(f"spare_passed:{n}", "spread",
                     f"spare caller ID {n} passed its trial: {v.rate:.1%} vs {v.ref_rate:.1%} on {v.calls} trial calls "
                     f"({v.pass_z:.1f} luck units clear of the {k['min_relative_drop']:.0%} line); it now takes an "
                     f"equal share of calls", asdict(v))
            continue
        on_trial = s is not None and s["status"] == "probing"
        # C10: passing a trial needs evidence it is fine (under the 30% line), not just too few calls
        # to prove it bad. Worse but unproven: keep collecting, up to TRIAL_MAX_CALLS x min_calls.
        unclear = on_trial and not v.bad and not clearly_fine(v, k)
        if unclear and v.calls >= TRIAL_MAX_CALLS * k["min_calls"]:
            v.bad, v.note = True, f"trial still unclear after {v.calls} calls: counted as failed"
        if v.bad and (s is None or s["status"] in ("probing", "restoring")):
            again = s is not None
            cooldown = (s["cooldown_h"] * 2) if again else k["probe_after_hours"]
            before = s["before"] if again else {c: d for c, d in sp.items() if d.get(n, 0) > EPS}
            nums[n] = {"status": "removing", "flagged_minute": now, "before": before, "cooldown_h": cooldown,
                       "verdict": asdict(v)}
            note(f"flag:{n}:{now}", "remove",
                 f"caller ID {n} answers {v.rate:.1%} vs {v.ref_rate:.1%} on {v.basis} over {v.calls} calls "
                 f"({v.worse_by:.0%} worse, {v.z:.1f} luck units; thresholds {k['min_relative_drop']:.0%} and "
                 f"{k['min_z']}). " + (f"{v.note + '. ' if v.note else ''}Failed its trial: next second chance in {cooldown} h."
                                       if again else "Removing it."), asdict(v))
        elif on_trial and not v.bad and not unclear:
            s.update(status="restoring", verdict=asdict(v))
            note(f"recovered:{n}:{now}", "restore",
                 f"caller ID {n} passed its trial: {v.rate:.1%} vs {v.ref_rate:.1%} on {v.calls} trial calls, "
                 f"{v.pass_z:.1f} luck units clear of the {k['min_relative_drop']:.0%} line (needs "
                 f"{k['pass_z']}). Returning it to its normal share.", asdict(v))
        elif s and s["status"] == "removing" and clearly_fine(v, k):
            # H2: a removal that never went through (turned down, still waiting) is re-judged
            moved = any(not _same(sp.get(c, {}), dict(b)) for c, b in s["before"].items())
            note(f"cleared:{n}:{now}", "cancel",
                 f"caller ID {n} no longer looks bad: {v.rate:.1%} vs {v.ref_rate:.1%} on {v.calls} calls "
                 f"({v.pass_z:.1f} luck units clear of the line). Cancelling its removal"
                 + ("; returning it to its normal share." if moved else "."), asdict(v))
            if moved:
                s.update(status="restoring", verdict=asdict(v))
            else:
                del nums[n]
    for n, s in list(nums.items()):
        live = any(d.get(n, 0) > EPS for d in sp.values())
        started = s.get("probe_minute", s.get("flagged_minute", 0))
        if s["status"] in ("probing", "restoring") and sup is not None and sup >= started:
            # M2: a supervisor changed caller IDs during our trial/restore. Their choice stands;
            # we never put a number back over it.
            note(f"supervisor:{n}:{sup}", "forget", f"a supervisor changed caller IDs at {store.sim_label(sup)} "
                 f"during caller ID {n}'s {'trial' if s['status'] == 'probing' else 'restore'}; leaving their split alone and forgetting ours"
                 + (f" ({n} is not in their split, and we will not trial it again)"
                    if not live else ""),
                 {"number": n, "supervisor_minute": sup})
            del nums[n]
            continue
        if s["status"] == "removing" and not live:
            s.update(status="removed", removed_minute=now, verify_at=now + k["verify_after_minutes"],
                     retry_at=now + s["cooldown_h"] * 60)
            note(f"removed:{n}:{now}", "removed", f"caller ID {n} now gets no calls"
                 + (f"; second chance after {store.sim_label(s['retry_at'])}" if self_change_allowed(ctx, wid)
                    else "; no automatic second chance, as this client's policy needs approval for caller IDs")
                 + ("; applies to new campaigns only (mid_flight: next_campaign_only)"
                    if ctx.workflows[wid].get("mid_flight") == "next_campaign_only" else ""), {"number": n})
        elif s["status"] == "removed":
            if live:  # someone else (a supervisor) put it back: their call, forget ours
                note(f"reinstated:{n}:{now}", "forget", f"caller ID {n} was put back by someone else; "
                     f"no longer tracking it as removed", {"number": n})
                del nums[n]
                continue
            if not s.get("verified") and now >= s["verify_at"]:
                result = _verify(ctx, wid, n, s, k)
                if result:  # waits for enough calls (a switch at 22:00 is judged the next morning)
                    s["verified"] = True
                    notes.append(result)
            healthy = [o for o in using if o != n and nums.get(o, {}).get("status") not in
                       (*NOT_HEALTHY, "removed", "spare_failed", "spare_declined")]
            if now >= s["retry_at"] and len(healthy) >= k["probe_min_healthy"] and self_change_allowed(ctx, wid):
                # L9: enough healthy numbers carry the calls; a trial of a number that was dead only
                # risks harm (public_02: both trials of dead D2 were graded harmful). Re-checked each step.
                note(f"no_probe:{n}:{s['retry_at']}", "no_action",
                     f"no second chance for caller ID {n} for now: {len(healthy)} healthy numbers "
                     f"({', '.join(healthy)}) carry the calls; it is retried only if fewer than "
                     f"{k['probe_min_healthy']} are left", {"number": n, "healthy": healthy})
            elif now >= s["retry_at"] and self_change_allowed(ctx, wid):
                if not s.get("verified"):  # N6: never leave 'removed' without a verify line
                    s["verified"] = True
                    notes.append(_verify(ctx, wid, n, s, k, allow_few=True))
                s.update(status="probing", probe_minute=now, live_minute=None)
                note(f"probe:{n}:{now}", "probe", f"second chance for caller ID {n}: "
                     f"{k['probe_share']:.0%} of calls on trial, judged on {k['min_calls']}+ trial calls",
                     {"number": n, "share": k["probe_share"]})
        elif s["status"] in ("probing", "spare_trial") and live and s.get("live_minute") is None:
            s["live_minute"] = now
        elif s["status"] == "spare_trial" and not spreading:  # a supervisor edited caller IDs meanwhile
            note(f"spare_forgot:{n}:{sup}", "forget", f"a supervisor changed caller IDs; dropping our trial of "
                 f"spare {n} and not spreading calls on this workflow", {"number": n})
            del nums[n]
            continue
        elif s["status"] == "restoring" and (
                all(sp[c].get(n, 0) > k["probe_share"] + EPS for c in s["before"] if c in sp) if spreading
                else all(_same(sp.get(c, {}), _restored(sp.get(c, {}), n, b)) for c, b in s["before"].items() if c in sp)):
            # R-M1: with spreading it is done once its share has grown past the trial share in config
            note(f"healthy:{n}:{now}", "healthy", f"caller ID {n} is back"
                 + (" and takes an equal share of calls" if spreading else " to its normal share"), {"number": n})
            del nums[n]
    if spreading:  # L1, L3, L6: bring in never-used offered numbers, on trial first
        used = store.callers_used(ctx.conn, ctx.run, wid)
        for o in ctx.workflows[wid]["options"].get(DIM, []):
            if o not in using and o not in nums and o not in used:
                nums[o] = {"status": "spare_trial", "since": now, "live_minute": None}
                note(f"spare_trial:{o}", "spread",
                     f"spreading calls so no number wears out: trying never-used caller ID {o} with part of the "
                     f"{k['spare_trial_share']:.0%} spare-trial share; it joins the equal split once it proves itself",
                     {"number": o, "share": k["spare_trial_share"]})

    # ---- 3. the split we want
    # N3: a removal the approver turned down (cooling down / given up) is left out, so it neither
    # blocks fixes for other numbers nor gets re-asked inside a new bundle. The number stays live, at
    # its current share. A spare is only "asked for" until its trial is live (R-H2).
    intent = f"{FEATURE}:{wid}"
    live_now = {o for dist in sp.values() for o, v in dist.items() if v > EPS}
    blocked = {}
    for n, s in nums.items():
        if ctx.gov is None:
            break
        if s["status"] in ("removing", "spare_failed"):  # R-H1: a failed spare's removal is a removal too
            outcome = ctx.gov.attempt_blocked(intent, f"remove {n}", now)
            if outcome:
                blocked[n] = outcome
                note(f"blocked:{n}:{outcome}:{s['flagged_minute']}", "wait",
                     f"not asking again to remove caller ID {n}: the approver turned it down ({outcome}); "
                     f"it stays in use at its current share and is still watched", {"number": n, "outcome": outcome})
        elif s["status"] == "spare_trial" and n not in live_now:
            outcome = ctx.gov.attempt_blocked(intent, f"trial spare {n}", now)
            if outcome == "given_up":
                s["status"] = "spare_declined"
                note(f"spare_declined:{n}", "declined", f"the approver turned down trying spare caller ID {n} "
                     f"twice; not asking again", {"number": n})
            elif outcome:
                blocked[n] = outcome
    trials = [n for n, s in nums.items() if s["status"] == "spare_trial" and n not in blocked]
    removed = {n for n, s in nums.items() if s["status"] in ("removing", "removed", "spare_failed") and n not in blocked}
    # A number on trial keeps only its trial share; it never takes other numbers' calls.
    no_more = removed | {n for n, s in nums.items() if s["status"] in ("probing", "spare_declined")} \
        | {n for n, s in nums.items() if s["status"] == "spare_trial" and n in blocked}

    def build(spread: bool) -> tuple[dict, str | None]:
        want, spare_used = {}, None
        for c, dist in sp.items():
            d = {o: v for o, v in dist.items() if o not in no_more and v > EPS}
            if not d and dist:  # spare rule: nothing healthy left in this cohort
                healthy = [o for o in using if o not in no_more]
                if healthy:
                    d = {o: 1 / len(healthy) for o in healthy}
                elif trials:  # R-H3: spares already on trial take over, untried or not
                    d = {o: 1 / len(trials) for o in trials}
                else:
                    spare_used = spare_used or pick_spare(ctx, wid, rows, set(nums) | set(using), k)
                    if spare_used:
                        d = {spare_used: 1.0}
                    else:
                        note(f"no_spare:{now // MINUTES_PER_DAY}", "no_action",
                             "every caller ID looks bad and there is no spare to switch to", {})
                        d = dict(dist)
            for n, s in nums.items():
                b = s.get("before", {}).get(c)
                if not b or n not in b:
                    continue
                if s["status"] == "probing":
                    d = _scaled(d, n, k["probe_share"])
                elif s["status"] == "restoring" and not spread:
                    d = _restored(d, n, b)
            if spread:  # L1, L2: equal shares for healthy numbers; trial spares share the trial share.
                # Probing numbers and numbers whose removal was turned down keep their share (R-M2).
                # Each cohort spreads over its own numbers plus the trial spares (documented).
                fixed = {o: v for o, v in d.items() if nums.get(o, {}).get("status") == "probing" or o in blocked}
                plain = [o for o in d if o not in fixed and o not in trials]
                if plain:
                    free = 1 - sum(fixed.values())
                    tsh = k["spare_trial_share"] * free if trials else 0.0
                    d = {**fixed, **{o: tsh / len(trials) for o in trials},
                         **{o: (free - tsh) / len(plain) for o in plain}}
            total = sum(d.values())
            want[c] = {o: round(v / total, 6) for o, v in d.items() if v > EPS} if total else d
        return want, spare_used

    want, spare_used = build(spreading)
    spread_target = None
    if spreading:
        spread_target = f"spread over {'/'.join(sorted({o for w in want.values() for o in w}))}"
        if ctx.gov is not None:  # G17: an even split the approver turned down is not re-asked daily
            outcome = ctx.gov.attempt_blocked(intent, spread_target, now)
            if outcome:
                note(f"spread_blocked:{spread_target}:{outcome}", "wait",
                     f"not asking again to {spread_target}: the approver turned it down ({outcome})",
                     {"outcome": outcome})
                want, spare_used = build(False)
                spread_target = None

    with ctx.conn.transaction():  # state and the diary lines that explain it commit together
        store.put_state(ctx.conn, ctx.run, skey, {"numbers": nums}, now)
        for key, action, reason, inputs in notes:
            ctx.diary(key, wid, "caller_id_health", action, reason, inputs)

    if all(_same(sp[c], want[c]) for c in sp):
        return None
    return _change(ctx, wid, k, sp, want, nums, verdicts, spare_used, spread_target)


def _restored(dist: dict[str, float], n: str, before: dict[str, float]) -> dict[str, float]:
    return _scaled(dist, n, before[n])


def _change(ctx, wid, k, sp, want, nums, verdicts, spare, spread_target=None) -> Change:
    """C6-C8: one change carrying the whole wanted split, with measured numbers for the note."""
    now = ctx.now
    day_ago = now - MINUTES_PER_DAY
    rows24 = store.hourly_counts(ctx.conn, ctx.run, since=day_ago, workflow_id=wid, by=DIM)
    wf_dials, wf_conn = _totals(rows24, -10**9)
    leaving = sorted({o for c in sp for o in sp[c] if sp[c][o] > EPS and want[c].get(o, 0) <= EPS})
    parts, rationale, dry_run, gain, affected = [], [], {}, 0.0, 0
    for n in leaving:
        s = nums.get(n, {})
        flag = Verdict(**s["verdict"]) if s.get("verdict") else None   # the evidence that met our bar
        latest = verdicts.get(n)
        nd, _ = _totals(rows24, -10**9, {n})
        affected += ctx.leads_called_24h(wid, n)
        if flag:
            v = latest or flag
            extra = (v.ref_rate - v.rate) * nd
            gain += max(extra, 0)
            when = store.sim_label(s["flagged_minute"])
            parts.append(f"stop calling from caller ID {n} (answered {flag.rate:.1%} vs {flag.ref_rate:.1%} on "
                         f"{flag.basis}, flagged {when})")
            text = (f"{n} flagged {when}: {flag.answered}/{flag.calls} answered over {flag.hours} h vs "
                    f"{flag.ref_answered}/{flag.ref_calls} on {flag.basis}: {flag.worse_by:.0%} worse, {flag.z:.1f} "
                    f"luck units (we act at {k['min_relative_drop']:.0%} and {k['min_z']})")
            if latest and latest != flag:
                text += (f"; latest: {latest.rate:.1%} vs {latest.ref_rate:.1%} on {latest.calls} calls "
                         f"({latest.worse_by:.0%} worse, {latest.z:.1f} luck units)")
            rationale.append(text)
            dry_run[n] = {"calls_last_24h": nd, "answered_at_its_rate": round(v.rate * nd),
                          "answered_at_other_numbers_rate": round(v.ref_rate * nd), "extra_answered_per_day": round(extra)}
    for n, s in nums.items():
        if s["status"] == "probing":
            parts.append(f"give caller ID {n} a second chance with {k['probe_share']:.0%} of calls")
            rationale.append(f"{n} was removed at {store.sim_label(s['removed_minute'])}; a removed number gets no "
                             f"calls, so a small trial is the only way to see if it recovered")
        elif s["status"] == "restoring":
            v = s["verdict"]
            parts.append(f"return recovered caller ID {n} to its normal share")
            rationale.append(f"{n} answered {v['rate']:.1%} vs {v['ref_rate']:.1%} on {v['calls']} trial calls")
    # R-H2: only spares whose trial is not yet live are this change's targets
    trial_spares = sorted(n for n, s in nums.items() if s["status"] == "spare_trial"
                          and any(want[c].get(n, 0) > EPS for c in want)
                          and not any(sp[c].get(n, 0) > EPS for c in sp))
    if trial_spares:
        share = max(want[c].get(trial_spares[0], 0) for c in want)
        parts.append(f"try never-used caller ID{'s' if len(trial_spares) > 1 else ''} {', '.join(trial_spares)} "
                     f"with {share:.0%} of calls each")
        rationale.append("spreading calls so no number wears out: a number carrying every call answers less and "
                         "less over days, and a fresh or rested one answers better; a never-used number gets a small "
                         "trial first, then an equal share")
    if spread_target and not parts:
        parts.append("spread calls evenly over the healthy caller IDs so no number wears out")
        rationale.append("load spreading: every healthy caller ID carries an equal share")
    if spare:
        parts.append(f"switch to spare caller ID {spare} (not used recently, health unknown)")
    shift = max(config_ops.tv_distance(sp[c], want[c]) for c in sp)
    if not affected:
        affected = int(ctx.leads_called_24h(wid) * shift)
    goes_to = sorted({o for c in want for o in want[c]})
    summary = "; ".join(parts) or "rebalance caller IDs"
    summary = summary[0].upper() + summary[1:] + f"; calls go to {', '.join(goes_to)}"
    return Change(
        intent_key=f"{FEATURE}:{wid}", workflow_id=wid, urgent=bool(leaving),
        attempts=[f"remove {n}" for n in leaving] + [f"trial spare {n}" for n in trial_spares]
        + ([spread_target] if spread_target and not leaving and not trial_spares else [])
        + [f"{s['status']} {n}" for n, s in sorted(nums.items()) if s["status"] in ("probing", "restoring")],
        patch={
            "allocations": {c: {DIM: want[c]} for c in sp if not _same(sp[c], want[c])}},
        summary=summary, rationale=" | ".join(rationale) or summary,
        affected_leads=int(affected),
        impact_score=round(min(gain / wf_conn, 1.0), 3) if wf_conn else 0.0,
        dry_run=dry_run or {"note": "trial / restore of a recovered number: no measurable change in answers expected",
                            "share_moved": round(shift, 3)},
        evidence={"split_now": sp, "split_wanted": want, "workflow_last_24h": {"dials": wf_dials, "answered": wf_conn},
                  "verdicts": {n: asdict(v) for n, v in verdicts.items()}},
        ttl_min=ctx.settings.gate("change_ttl_minutes"))


def _verify(ctx, wid, n, s, k, allow_few=False):
    """C9: before/after answer rate, written once, `verify_after_minutes` after the switch finished
    and once at least min_calls were dialled since. None while there are too few calls."""
    k_before = s["flagged_minute"]
    before = store.hourly_counts(ctx.conn, ctx.run, since=k_before - 180, until=k_before, workflow_id=wid, by=DIM)
    after = store.hourly_counts(ctx.conn, ctx.run, since=s["removed_minute"] // 60 * 60 + 60, workflow_id=wid,
                                by=DIM)
    bd, bc = _totals(before, -10**9)
    hd, hc = _totals(before, -10**9, {o for o in {r["option"] for r in before} if o != n})
    ad, ac = _totals(after, -10**9)
    if ad < k["min_calls"] and not allow_few:
        return None
    b, h, a = stats.rate(bc, bd), stats.rate(hc, hd), stats.rate(ac, ad)
    fmt = lambda x: "n/a" if x is None else f"{x:.1%}"  # noqa: E731
    ok = a is not None and b is not None and a > b
    return (f"{FEATURE}:{wid}:verify:{n}:{s['removed_minute']}", "verified" if ok else "not_improved",
            f"after removing caller ID {n}: workflow pickup {fmt(a)} on {ad} calls, vs {fmt(b)} in the 3 h before "
            f"it was flagged (healthy numbers then: {fmt(h)})",
            {"number": n, "before": {"dials": bd, "answered": bc}, "healthy_before": {"dials": hd, "answered": hc},
             "after": {"dials": ad, "answered": ac}})
