"""Script health (C8): a script that starts making things up is replaced, with proof.

Per workflow, each loop step (decision ids H1-H10 in notes.md):
  1. Judge the running script (H1-H3), two ways, alarming on either:
       - since it went live (if it changed in the last reference_days) vs the script before it;
       - its last day vs its own earlier use (a slow drift).
     Scripts that were ever the subject of an incident are never used as the clean reference.
  2. Pick the fix (H4-H7). First choice: go back to a script real calls proved clean (its test drive
     only needs the approver's bar: real calls already proved it). In parallel, ask the AI and
     test-drive the first `ai_ideas_to_test` new ideas; they are reported, not switched to. Only with
     no proven script does the cleanest idea that clears the bar with margin become the fix. A fix the
     approver turned down (cooling down / given up) is replaced by the next option.
  3. Return one urgent change (H8). The gate adds "overrides a recent supervisor setting".
  4. Verify against normal on enough calls, made-up claims and engagement (H9).

Paid calls: the cost is saved with a "pending" marker BEFORE the call and the answer straight after,
so neither a crash nor an error can make us pay again without the budget knowing. Paid calls are
never retried automatically (they carry no id).
"""
from __future__ import annotations

import logging
import math

from .. import stats, store
from ..governance import Change
from ..store import MINUTES_PER_DAY, sum_rows

FEATURE = "script_health"
DIM = "prompt_version"
AI_COST = 3          # credits per AI question (docs/SIMULATOR.md section 5)
MAX_ERRORS = 2       # a paid call the sim refuses this often is given up for the incident
log = logging.getLogger(__name__)


def _rate(t, key="hallucinations"):
    return stats.rate(t[key], t["connected"])


def _window_start(now: int, hours: int) -> int:
    return (now - hours * 60) // 60 * 60


# ---------------------------------------------------------------- judging (H1-H3)

def _verdict(pv, prev, basis, window, cur, ref, k, live_since) -> dict | None:
    if cur["connected"] < k["min_calls"] or ref["connected"] < k["min_calls"] or not ref["hallucinations"]:
        return None
    r_cur, r_ref = _rate(cur), _rate(ref)
    z = stats.z_score(cur["hallucinations"], cur["connected"], ref["hallucinations"], ref["connected"])
    return {"script": pv, "reference_script": prev, "basis": basis, "window": window, "live_since": live_since,
            "calls": cur["connected"], "made_up": cur["hallucinations"], "rate": r_cur,
            "ref_calls": ref["connected"], "ref_made_up": ref["hallucinations"], "ref_rate": r_ref,
            "ref_engaged": ref["engaged"], "ratio": r_cur / r_ref, "z": z,
            "engagement": _rate(cur, "engaged"), "ref_engagement": _rate(ref, "engaged"),
            "bad": (r_cur >= k["min_ratio"] * r_ref and z >= k["min_z"]
                    and cur["hallucinations"] >= k["min_made_up"])}


def judge(ctx, wid: str, pv: str, k: dict, bad_scripts: list[str]) -> dict | None:
    """Both checks; the bad one if any, else the since-switch one, else the drift one."""
    now = ctx.now
    lookback = now - k["reference_days"] * MINUTES_PER_DAY
    rows = store.hourly_counts(ctx.conn, ctx.run, since=lookback, workflow_id=wid, by=DIM)
    live_since = store.last_config_change(ctx.conn, ctx.run, wid, DIM)
    found = []
    if live_since is not None and live_since > lookback:
        switch_hour = live_since // 60 * 60  # rows are per script, so pv rows from this hour on are after it
        cur = sum_rows(rows, since=switch_hour, options={pv})
        before = [r for r in rows if r["start"] < switch_hour + 60 and r["option"] not in {pv, *bad_scripts}]
        prev = max({r["option"] for r in before}, key=lambda o: sum_rows(before, options={o})["connected"],
                   default=None)
        ref = sum_rows(before, options={prev}) if prev else None
        basis = f"script {prev} before the switch at {store.sim_label(live_since)}"
        own = sum_rows(rows, until=switch_hour, options={pv})
        if (not ref or _rate(ref) is None or _rate(ref) > k["proven_max_rate"]) and own["connected"] >= k["min_calls"]:
            prev, ref, basis = pv, own, "its own earlier use"   # the script before was not a clean reference
        if ref:
            found.append(_verdict(pv, prev, basis, f"since {pv} went live", cur, ref, k, live_since))
    since = _window_start(now, k["drift_window_hours"])  # a slow drift: judged on a whole day
    cur = sum_rows(rows, since=since, options={pv})
    if cur["connected"] >= k["drift_min_calls"]:
        ref = sum_rows(rows, until=since, options={pv})
        found.append(_verdict(pv, pv, f"its own previous {k['reference_days']} days",
                              f"last {k['drift_window_hours']} h", cur, ref, k, live_since))
    found = [v for v in found if v]
    return next((v for v in found if v["bad"]), found[0] if found else None)


def proven_scripts(ctx, wid: str, k: dict, exclude: set[str]) -> list[dict]:
    """H4: scripts real calls proved clean (>= proven_min_calls answered, <= proven_max_rate made up),
    most-used first, never one that was the subject of an incident."""
    rows = store.hourly_counts(ctx.conn, ctx.run, since=ctx.now - k["reference_days"] * MINUTES_PER_DAY,
                               workflow_id=wid, by=DIM)
    out = []
    for o in sorted({r["option"] for r in rows} - exclude - {"?"}):
        t = sum_rows(rows, options={o})
        if (t["connected"] >= k["proven_min_calls"] and _rate(t) <= k["proven_max_rate"]
                and o in ctx.workflows[wid]["options"].get(DIM, [])):
            out.append({"script": o, "calls": t["connected"], "rate": _rate(t), "engagement": _rate(t, "engaged")})
    return sorted(out, key=lambda x: -x["calls"])


# ---------------------------------------------------------------- credits (H7)

def _start_credits(ctx) -> float:
    key = f"{FEATURE}:credits_start"
    st = store.get_state(ctx.conn, ctx.run, key)
    if not st:
        st = {"credits": ctx.sim_state.get("credits_remaining", 0)}
        store.put_state(ctx.conn, ctx.run, key, st, ctx.now)
    return st["credits"]


def _can_spend(ctx, inc: dict, cost: int, k: dict, essential: bool) -> str | None:
    """None if allowed, else why not. Spending needed to reach a fix may dip into the reserve (it is
    what the reserve is for); optional spending (ideas when a fix exists) may not."""
    left = ctx.sim_state.get("credits_remaining", 0)
    if inc["spent"] + cost > k["credits_per_incident"]:
        return f"incident budget {k['credits_per_incident']} credits ({inc['spent']} spent)"
    if left - cost < 0:
        return f"only {left} credits left"
    if not essential and left - cost < k["credit_reserve_share"] * _start_credits(ctx):
        return f"would go below the {k['credit_reserve_share']:.0%} reserve ({left} left)"
    return None


# ---------------------------------------------------------------- the feature

def check(ctx) -> list[Change]:
    out = []
    for wid in ctx.workflows:
        try:
            ch = Workflow(ctx, wid).check()
        except Exception as e:  # one workflow's problem must not stop the others
            if type(e).__module__.startswith(("httpx", "psycopg")):
                raise
            log.exception("script check failed for %s at sim minute %s", wid, ctx.now)
            ctx.keep.add(f"{FEATURE}:{wid}")
            ctx.diary(f"feature_error:{FEATURE}:{wid}:{ctx.now // 60}", wid, "feature_error", "skipped",
                      f"script check raised {type(e).__name__}: {e}; skipped this step", {"feature": FEATURE})
            continue
        if ch:
            out.append(ch)
    return out


class Workflow:
    """One workflow's script health for one loop step. State: {"incident": ..., "bad_scripts": [...]}."""

    def __init__(self, ctx, wid: str):
        self.ctx, self.wid = ctx, wid
        self.k = ctx.settings.feature(FEATURE, wid)
        self.pv = ctx.config(wid)["config"].get(DIM)
        self.skey, self.intent = f"{FEATURE}:{wid}", f"{FEATURE}:{wid}"
        st = store.get_state(ctx.conn, ctx.run, self.skey)
        self.inc = st.get("incident")
        flagged = st.get("bad_scripts") or {}
        if isinstance(flagged, list):  # older state
            flagged = {x: ctx.now for x in flagged}
        horizon = ctx.now - self.k["reference_days"] * MINUTES_PER_DAY
        self.flagged = {x: m for x, m in flagged.items() if m > horizon}  # one false alarm doesn't last forever
        self.bad_scripts = sorted(self.flagged)
        self.bar = ctx.workflows[wid]["governance"].get("min_validation_pass_rate", 0)

    def save(self, action=None, reason="", inputs=None, key=None):
        """State, and the diary line explaining it, in one transaction."""
        with self.ctx.conn.transaction():
            store.put_state(self.ctx.conn, self.ctx.run, self.skey,
                            {"incident": self.inc, "bad_scripts": self.flagged}, self.ctx.now)
            if action:
                self.ctx.diary(f"{FEATURE}:{self.wid}:{key}", self.wid, "script_health", action, reason, inputs or {})

    # ---- one step
    def check(self) -> Change | None:
        inc, pv = self.inc, self.pv
        if inc and inc["status"] == "open" and pv != inc["bad"]:
            if pv == inc.get("target"):
                inc.update(status="fixed", fixed_minute=self.ctx.now)
                self.save("fixed", f"script {pv} is live in place of {inc['bad']}; checking the effect once "
                          f"{self.k['verify_after_minutes']} min and {self.k['verify_min_calls']} answered calls have "
                          f"passed", {"script": pv}, f"fixed:{inc['flagged_minute']}")
            else:
                self._recover_lost_payments()
                self.inc = None
                self.save("forget", f"script changed from {inc['bad']} to {pv} by someone else; closing our incident",
                          {"script": pv}, f"elsewhere:{self.ctx.now}")
        elif inc and inc["status"] == "fixed" and pv != inc["target"]:
            # the fix is no longer live (e.g. a supervisor switched again): judge the new script at once,
            # and never blame our fix for theirs in the verify line
            self._recover_lost_payments()
            self.inc = None
            self.save("superseded", f"our fix {inc['target']} was replaced by {pv} before it could be verified; "
                      f"judging {pv} afresh", {"script": pv}, f"superseded:{self.ctx.now}")
        if self.inc and self.inc["status"] == "fixed":
            self._verify()
            return None
        if not self.inc:
            v = judge(self.ctx, self.wid, pv, self.k, self.bad_scripts)
            if not v or not v["bad"]:
                return None
            self.inc = {"status": "open", "bad": pv, "flagged_minute": self.ctx.now, "verdict": v, "spent": 0,
                        "target": None, "tested": {}, "ai": None, "skipped": [], "pending": {}, "errors": {}}
            self.flagged[pv] = self.ctx.now
            self.bad_scripts = sorted(self.flagged)
            self.save("incident", f"script {pv} makes things up on {v['rate']:.1%} of answered calls ({v['made_up']}/"
                      f"{v['calls']}, {v['window']}) vs {v['ref_rate']:.1%} on {v['basis']}: {v['ratio']:.1f}x, "
                      f"{v['z']:.1f} luck units (we act at {self.k['min_ratio']}x and {self.k['min_z']}). Engagement "
                      f"{v['engagement']:.1%} vs {v['ref_engagement']:.1%}.", v, f"incident:{self.ctx.now}")
        self.flagged[self.inc["bad"]] = self.ctx.now  # still live and bad: keep its flag fresh
        self._recover_lost_payments()
        self._drop_turned_down_target()
        self._choose_target()
        if not self.inc["target"]:
            return None
        return self._change()

    # ---- the fix
    def _drop_turned_down_target(self):
        """M: a fix the approver turned down (cooling down / given up): move to the next option."""
        inc, gov = self.inc, self.ctx.gov
        t = inc["target"]
        if not t or gov is None:
            return
        outcome = gov.attempt_blocked(self.intent, f"script {t}", self.ctx.now)
        if outcome:
            inc["skipped"].append(t)
            inc.update(target=None, proven=None, target_noted=False, stuck_noted=False)
            self.save("next_option", f"the approver turned down going to {t} ({outcome}); trying the next option",
                      {"script": t, "outcome": outcome}, f"turned_down:{inc['flagged_minute']}:{t}")

    def _choose_target(self):
        inc = self.inc
        exclude = {inc["bad"], *self.bad_scripts, *inc["skipped"]}
        proven_waiting = False  # a proven script whose refused test drive will be retried beats any AI idea
        if not inc["target"]:
            for p in proven_scripts(self.ctx, self.wid, self.k, exclude):
                t = self._test_drive(p["script"], essential=True)
                if t == "no_credits":
                    break
                if t == "refused":             # retry next step (it may still be paid for)
                    proven_waiting = True
                    break
                if t in ("exhausted", "turned_down"):
                    inc["skipped"].append(p["script"])
                    self.save("candidate_skipped", f"skipping proven script {p['script']}: "
                              + ("the approver turned it down" if t == "turned_down"
                                 else f"the sim refused its test drive {MAX_ERRORS} times"),
                              {"script": p["script"], "reason": t}, f"skip:{inc['flagged_minute']}:{p['script']}:{t}")
                    continue                   # this one is out; the next proven script may do
                if not isinstance(t, dict):
                    raise ValueError(f"unexpected test-drive outcome {t!r}")
                if t["pass_rate"] >= self.bar:  # real calls proved it; the test drive only needs the bar
                    inc.update(target=p["script"], mode="proven", proven=p)
                    break
                inc["skipped"].append(p["script"])
        self._ai_ideas(optional=bool(inc["target"]) or proven_waiting)
        if not inc["target"] and not proven_waiting:  # H5: nothing proven: the cleanest idea clearing the bar WITH margin
            ideas = [t for s, t in inc["tested"].items()
                     if "pass_rate" in t and t["lower"] >= self.bar and s not in exclude]
            if ideas:
                best = max(ideas, key=lambda t: t["pass_rate"])
                inc.update(target=best["script"], mode="ai_idea", proven=None)
        if inc["target"] and not inc.get("target_noted"):
            inc["target_noted"] = True
            how = ("proven on real calls" if inc["mode"] == "proven"
                   else "an AI idea that passed its test drive; engagement unknown until it runs")
            self.save("target", f"fix for {inc['bad']}: switch to {inc['target']} ({how})", {"target": inc["target"]},
                      f"target:{inc['flagged_minute']}:{inc['target']}")
        elif (not inc["target"] and not inc.get("stuck_noted")
              and (inc["ai"] is not None or inc.get("ai_skipped") or self.k["ai_ideas_to_test"] == 0)):
            inc["stuck_noted"] = True
            self.save("no_fix", f"no fix available for {inc['bad']} yet (nothing proven, no idea passed, or no "
                      f"credits); still watching", {"tested": inc["tested"]}, f"nofix:{inc['flagged_minute']}")

    def _ai_ideas(self, optional: bool):
        """H5 (Kiran): ask the AI once per incident; test-drive the first `ai_ideas_to_test` new ideas.
        Needed for a fix when nothing proven exists (or is coming), so only then may it use the reserve."""
        inc, n_test = self.inc, self.k["ai_ideas_to_test"]
        essential = not optional
        if n_test == 0:
            return  # asking without testing would buy names we can't use
        if inc["ai"] is None:
            why_not = _can_spend(self.ctx, inc, AI_COST, self.k, essential)
            if why_not:  # not saved as final: re-checked every step, e.g. once it becomes essential
                inc["ai_skipped"] = why_not
                self.save("ai_skipped", f"not asking the AI for ideas ({'needed' if essential else 'optional'}): "
                          f"{why_not}", {}, f"ai_skip:{inc['flagged_minute']}:{essential}")
                return
            v = inc["verdict"]
            res = self._paid("ai", AI_COST, "/llm/suggest-fixes", {"workflow_id": self.wid, "problem": (
                f"made-up claims on {v['rate']:.1%} of answered calls with script {inc['bad']} vs "
                f"{v['ref_rate']:.1%} before; engagement {v['engagement']:.1%} vs {v['ref_engagement']:.1%}")})
            if res.get("error"):
                if inc["errors"].get("ai", 0) >= MAX_ERRORS:
                    inc["ai"] = {"asked": False, "why_not": f"refused {MAX_ERRORS} times", "ideas": []}
                self.save("ai_failed", f"AI question refused by the sim: {res['error']}", res,
                          f"ai_err:{inc['flagged_minute']}:{inc['errors'].get('ai')}")
                return
            ideas = res.get("suggestions", [])
            # "the first N new ideas" is decided now, so choosing one as the fix later doesn't shift the list
            inc["ai"] = {"asked": True, "ideas": ideas, "exclude": sorted({inc["bad"], inc["target"] or inc["bad"]})}
            self.save("ai_ideas", "AI suggested: " + (", ".join(f"{i['prompt_version']} ({i['summary']})"
                                                               for i in ideas) or "nothing") + f" ({AI_COST} credits)",
                      {"ideas": ideas}, f"ai:{inc['flagged_minute']}")
        offered = self.ctx.workflows[self.wid]["options"].get(DIM, [])
        skip = set(inc["ai"].get("exclude") or []) | set(self.bad_scripts)
        new = [i["prompt_version"] for i in inc["ai"]["ideas"] if i["prompt_version"] not in skip
               and i["prompt_version"] in offered]
        for script in list(dict.fromkeys(new))[:n_test]:
            if self._test_drive(script, essential=essential) == "no_credits":
                break

    # ---- paid calls (H6, H7)
    def _paid(self, label: str, cost: int, path: str, body: dict) -> dict:
        """Count the cost and mark it pending BEFORE calling, so a crash or error after the sim charged us
        can't make us pay again unnoticed. Never retried automatically."""
        inc = self.inc
        inc["spent"] += cost
        inc["pending"][label] = cost
        self.save()
        code, resp = self.ctx.sim.send("POST", path, body, retry=False)
        del inc["pending"][label]
        if code != 200:  # refused: not charged
            inc["spent"] -= cost
            inc["errors"][label] = inc["errors"].get(label, 0) + 1
            log.warning("%s refused: %s %s", path, code, resp)
            return {"error": code, "detail": resp}
        if "credits_remaining" in resp:
            self.ctx.sim_state["credits_remaining"] = resp["credits_remaining"]
        return resp

    def _recover_lost_payments(self):
        inc = self.inc
        for label in list(inc.get("pending", {})):
            del inc["pending"][label]
            self.save("paid_lost", f"lost the answer to a paid call ({label}) after a crash or error; its credits "
                      f"stay counted against this incident", {"label": label}, f"lost:{inc['flagged_minute']}:{label}:"
                      f"{inc['spent']}")

    def _test_drive(self, script: str, *, essential: bool) -> dict | str:
        """One test drive per script per incident. Returns the result, or why there is none:
        'turned_down' (the approver already refused this fix), 'exhausted' (the sim refused it
        MAX_ERRORS times), 'refused' (retry next step), 'no_credits'."""
        inc, k = self.inc, self.k
        if script in inc["tested"]:
            return inc["tested"][script]
        if self.ctx.gov is not None and self.ctx.gov.attempt_blocked(self.intent, f"script {script}", self.ctx.now):
            return "turned_down"  # don't pay to test a fix the approver won't take
        if inc["errors"].get(script, 0) >= MAX_ERRORS:
            return "exhausted"
        cost = math.ceil(k["validation_n"] / 100)
        why_not = _can_spend(self.ctx, inc, cost, k, essential)
        if why_not:
            self.save("no_credits", f"not test-driving {script}: {why_not}", {"script": script},
                      f"nocredit:{inc['flagged_minute']}:{script}")
            return "no_credits"
        res = self._paid(script, cost, "/validation/runs",
                         {"workflow_id": self.wid, "prompt_version": script, "n": k["validation_n"]})
        if res.get("error"):
            self.save("test_drive_failed", f"test drive of {script} refused by the sim: {res['error']}", res,
                      f"val_err:{inc['flagged_minute']}:{script}:{inc['errors'][script]}")
            return "refused" if inc["errors"][script] < MAX_ERRORS else "exhausted"
        p, n = res["pass_rate"], res["n"]
        lower = p - k["validation_margin_z"] * math.sqrt(max(p * (1 - p), 1e-9) / n)
        t = {"script": script, "validation_run_id": res["validation_run_id"], "pass_rate": p, "n": n,
             "lower": round(lower, 4)}
        inc["tested"][script] = t
        self.save("test_drive", f"test drive of {script}: {p:.1%} clean on {n} conversations ({cost} credits); "
                  f"approver's bar {self.bar:.0%}" + (f", cleared with margin" if lower >= self.bar
                                                       else f", lower bound {lower:.1%}"),
                  t, f"val:{inc['flagged_minute']}:{script}")
        return t

    # ---- after (H9)
    def _verify(self):
        inc, k, pv = self.inc, self.k, self.pv
        if self.ctx.now < inc["fixed_minute"] + k["verify_after_minutes"]:
            return
        after = sum_rows(store.hourly_counts(self.ctx.conn, self.ctx.run, since=inc["fixed_minute"] // 60 * 60 + 60,
                                             workflow_id=self.wid, by=DIM), options={pv})
        if after["connected"] < k["verify_min_calls"]:
            return
        v = inc["verdict"]
        r, e = _rate(after), _rate(after, "engaged")
        # judged against NORMAL (the rate before the alarm), not just the alarm's spike
        z_made_up = stats.z_score(after["hallucinations"], after["connected"], v["ref_made_up"], v["ref_calls"])
        z_engaged = (stats.z_score(v["ref_engaged"], v["ref_calls"], after["engaged"], after["connected"])
                     if v.get("ref_engaged") is not None else 0.0)
        if r >= v["rate"]:
            action = "not_improved"
        elif z_made_up >= k["verify_z"]:
            action = "not_back_to_normal"
        else:
            action = "verified"
        key = f"verify:{inc['flagged_minute']}"
        self.inc = None
        self.save(action, f"after switching to {pv}: made-up claims {r:.1%} on {after['connected']} answered calls; "
                  f"{v['rate']:.1%} during the incident, {v['ref_rate']:.1%} normally"
                  + (f" ({z_made_up:.1f} luck units above normal)" if action == "not_back_to_normal" else "")
                  + f". Engagement {e:.1%} (incident {v['engagement']:.1%}, normally {v['ref_engagement']:.1%})",
                  {"after": after, "incident_verdict": v, "z_made_up": z_made_up, "z_engaged": z_engaged}, key)
        if z_engaged >= k["verify_z"]:  # the fix is safe but costs engagement: say so loudly
            self.save("engagement_drop", f"script {pv} keeps engagement at {e:.1%} vs {v['ref_engagement']:.1%} "
                      f"normally ({z_engaged:.1f} luck units lower): needs a person's look", {"after": after},
                      f"engagement:{inc['flagged_minute']}")

    # ---- the change (H8)
    def _change(self) -> Change:
        inc = self.inc
        v, target = inc["verdict"], inc["target"]
        t = inc["tested"][target]
        proven = inc.get("proven")
        others = [x for s, x in inc["tested"].items() if s != target and "pass_rate" in x]
        others_text = "; ".join(f"{x['script']} {x['pass_rate']:.1%} clean" for x in others)
        day = sum_rows(store.hourly_counts(self.ctx.conn, self.ctx.run, since=self.ctx.now - MINUTES_PER_DAY,
                                           workflow_id=self.wid, by=DIM))
        target_rate = proven["rate"] if proven else (1 - t["pass_rate"])
        if proven:
            summary = (f"Switch script back from {inc['bad']} to proven {target}: {inc['bad']} makes things up on "
                       f"{v['rate']:.1%} of calls vs {proven['rate']:.1%} for {target} over {proven['calls']} real calls")
        else:
            summary = (f"Replace script {inc['bad']} ({v['rate']:.1%} made up) with {target}: AI idea, test drive "
                       f"{t['pass_rate']:.1%} clean; engagement unknown until it runs")
        sup = store.last_supervisor_edit(self.ctx.conn, self.ctx.run, self.wid, DIM)
        by_supervisor = sup is not None and v.get("live_since") is not None and sup >= v["live_since"] - 15
        rationale = ((f"The script being replaced was set by a supervisor at {store.sim_label(sup)}. "
                      if by_supervisor else "")
                     + f"{inc['bad']}: {v['made_up']}/{v['calls']} answered calls flagged ({v['window']}) vs "
                     f"{v['ref_made_up']}/{v['ref_calls']} on {v['basis']}: {v['ratio']:.1f}x, {v['z']:.1f} luck "
                     f"units. Engagement {v['engagement']:.1%} vs {v['ref_engagement']:.1%}. Test drive of {target}: "
                     f"{t['pass_rate']:.1%} clean on {t['n']} ({t['validation_run_id']}). "
                     + (f"Other options checked (test drives measure made-up claims, not engagement, so not "
                        f"switched to): {others_text}." if others_text else ""))
        return Change(
            intent_key=self.intent, workflow_id=self.wid, urgent=True, patch={DIM: target},
            attempts=[f"script {target}"], summary=summary, rationale=rationale,
            affected_leads=self.ctx.leads_called_24h(self.wid),
            impact_score=round(min(max(v["rate"] - target_rate, 0) / v["rate"], 1.0), 3) if v["rate"] else 0.0,
            dry_run={"answered_calls_last_24h": day["connected"], "made_up_last_24h": day["hallucinations"],
                     "made_up_at_target_rate": round(target_rate * day["connected"]),
                     "fewer_made_up_per_day": round(max(v["rate"] - target_rate, 0) * day["connected"]),
                     "target_engagement_measured": proven["engagement"] if proven else None},
            evidence={"validation_run_id": t["validation_run_id"], "validation_pass_rate": t["pass_rate"],
                      "incident": v, "other_options": others, "credits_spent": inc["spent"]},
            ttl_min=self.ctx.settings.gate("change_ttl_minutes"))
