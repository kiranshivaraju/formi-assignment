"""The governance client (C9): the only code that changes a workflow's config.

Features describe what they want as a Change and `submit` it. The Governor decides how and when it
reaches the client: auto-apply when the workflow's own policy allows it, otherwise a request to the
approver, timed and bundled. Decision ids (G1-G23, D12-D17) refer to notes.md.

Status machine (the diary records every arrow; state changes and their diary line commit together)

  changes:   queued -> in_flight -> applied
                    |            -> rejected | expired | refused      (terminal; submit() may re-queue
                    |            -> queued   (stale, lapsed, superseded-but-unrelated: re-queued;
                    |                         or one step of a stepped change applied, T3)
                    -> dropped   (unsendable, supervisor took the setting, no-op, or no longer
                                  wanted: its feature stopped re-submitting it, F3)

  proposals: sending -> pending -> approved -> applying -> applied
             sending -> applied                                   (auto path)
             any open -> stale | lapsed | superseded | refused     (changes re-queued or dropped)
             approved -> withdrawn   (its feature stopped wanting it before we applied: not applied, N1)
             pending  -> rejected | expired       (set by ingest from the event, or by _adopt_existing
                                                   when a restart finds the sim already decided)

Which path (T3, T4): if the workflow's policy lets us change every setting a change touches, we do
it ourselves, in steps of at most max_shift, `step_gap_minutes` apart. Otherwise we ask the approver
for the full change. A change on the self-change path never goes to the approver, and vice versa.

Crash safety: a request is written as 'sending' with our own id before it leaves, an apply is marked
'applying' before it is attempted, and `reconcile()` resolves both at startup by asking the sim.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field

import psycopg

from . import config_ops, store
from . import settings as settings_mod
from .client import SimClient
from .store import MINUTES_PER_DAY

log = logging.getLogger(__name__)

URGENT_BUFFER_MIN = 30        # G7: expected answer must land this long before the approver leaves
MORNING_WINDOW_MIN = 60       # G5 / D16: the morning note goes out early in the approver's day
REJECT_COOLDOWN_MIN = MINUTES_PER_DAY  # G17: wait a day before one retry of a rejected change
MAX_REJECTIONS = 2            # G17: after this many rejections/refusals, stop proposing it
HOLD_MIN_CONNECTED = 400      # G21: answered calls after a supervisor edit before we judge it
MIN_LATENCY_SAMPLES = 3       # G7 / D17: real answers needed before trusting a manager's average

OPEN = ("sending", "pending", "approved", "applying")   # proposals still in play
NOT_ACCEPTED = ("rejected", "refused")                   # change outcomes that count against an idea


@dataclass
class Change:
    """What a feature wants changed (G1). `patch` is merged into the current config."""
    intent_key: str            # stable id of the idea, e.g. 'caller_id_health:ks_cancel:D2' (D12)
    workflow_id: str
    urgent: bool
    patch: dict
    summary: str               # one line a supervisor understands
    rationale: str
    affected_leads: int
    impact_score: float        # [0, 1]
    dry_run: dict
    evidence: dict = field(default_factory=dict)  # numbers; a prompt change needs
                                                  # validation_run_id and validation_pass_rate
    ttl_min: int | None = None  # F3: drop it if not re-submitted for this long (None: keep)
    attempts: list[str] | None = None  # G17: the targets this change carries, e.g. ["remove D2"].
                                       # Rejections count per target, not per intent: one intent can carry
                                       # different targets over time, and a "no" to one must not block
                                       # another. None: the intent itself is the one target.


# ---------------------------------------------------------------- pure timing rules (tested alone)

def day_minute(now: int) -> int:
    return now % MINUTES_PER_DAY


def in_morning_window(now: int, hours: list[int], step: int = 15) -> bool:
    """D16, widened to one loop step so a large --step can't jump over it."""
    start = hours[0] * 60
    return start <= day_minute(now) < start + max(MORNING_WINDOW_MIN, step)


def urgent_can_send_now(now: int, hours: list[int], avg_latency: float) -> bool:
    """G7: the approver is in, and now + their average answer time lands at least URGENT_BUFFER_MIN
    before they leave."""
    start, end = hours[0] * 60, hours[1] * 60
    m = day_minute(now)
    return start <= m < end and m + avg_latency <= end - URGENT_BUFFER_MIN


def slots_free(limit: int, used_24h: int, urgent: bool) -> bool:
    """G8: normal notes leave one slot free for an emergency (when the policy allows 2+).
    A morning note that carries an urgent item counts as urgent, so it may use that reserve slot;
    it is still one note, and the normal items in it would otherwise wait another day."""
    reserve = 0 if urgent or limit < 2 else 1
    return used_24h < limit - reserve


# ---------------------------------------------------------------- the Governor

class Governor:
    def __init__(self, sim: SimClient, conn: psycopg.Connection, run: store.Run, workflows: list[dict],
                 step: int = 15, settings: settings_mod.Settings | None = None):
        self.sim, self.conn, self.run, self.step = sim, conn, run, step
        self.wf = {w["workflow_id"]: w for w in workflows}
        settings = settings or settings_mod.load()
        self.step_gap = settings.gate("step_gap_minutes")

    # -------------------------------------------------------------- small helpers
    def _q(self, sql: str, *params):
        return self.conn.execute(sql, params)

    def _diary(self, key: str, now: int, wid: str, kind: str, action: str, reason: str, inputs: dict) -> None:
        """Call inside the transaction that makes the state change, so neither exists without the other."""
        store.record_decision(self.conn, self.run, key=key, sim_minute=now, workflow_id=wid,
                              kind=kind, action=action, reason=reason, inputs=inputs)

    def _set_changes(self, change_ids: list[int], status: str, now: int) -> None:
        self._q("UPDATE changes SET status=%s, updated_minute=%s WHERE change_id = ANY(%s)",
                status, now, list(change_ids))

    def _set_proposal(self, proposal_id: str, **cols) -> None:
        sets = ", ".join(f"{k}=%s" for k in cols)
        self._q(f"UPDATE proposals SET {sets} WHERE run_id=%s AND proposal_id=%s",
                *cols.values(), self.run.run_id, proposal_id)

    def _proposal(self, proposal_id: str) -> dict:
        return self._q("SELECT * FROM proposals WHERE run_id=%s AND proposal_id=%s",
                       self.run.run_id, proposal_id).fetchone()

    def _sim_approval(self, wid: str, proposal_id: str) -> dict | None:
        return next((a for a in self.sim.approvals(wid) if a["proposal_id"] == proposal_id), None)

    def _applied_version(self, wid: str, proposal_id: str) -> int:
        """L1: the version our request created, looked up by id rather than assumed current."""
        v = next((h["version"] for h in self.sim.config_history(wid) if h["proposal_id"] == proposal_id), None)
        if v is None:
            raise RuntimeError(f"{proposal_id} should be applied but is not in {wid}'s config history")
        return v

    def held_dims(self, wid: str) -> dict[str, int]:
        """Settings a supervisor recently changed -> minute of their edit."""
        rows = self._q("SELECT dim, max(since_minute) AS since FROM holds WHERE run_id=%s AND workflow_id=%s "
                       "AND released_minute IS NULL GROUP BY dim", self.run.run_id, wid).fetchall()
        return {x["dim"]: x["since"] for x in rows}

    # -------------------------------------------------------------- features call this
    def submit(self, change: Change, now: int) -> str:
        """Queue a change, or refresh the queued copy with fresher numbers. Returns what happened:
        queued | refreshed | in_flight | held | cooling_down | given_up."""
        dims = config_ops.patch_dims(change.patch)
        r, key, wid = self.run.run_id, change.intent_key, change.workflow_id
        for attempt in change.attempts or [key]:  # safety net; features should leave blocked targets out
            outcome, n = self._attempt_record(key, attempt, now)
            if outcome:
                with self.conn.transaction():
                    self._diary(f"{outcome}:{key}:{attempt}:{n}", now, wid, "change_not_proposed", outcome,
                                f"not proposing '{change.summary}': '{attempt}' turned down {n} time(s)"
                                + ("; giving up" if outcome == "given_up" else "; waiting a day before one retry"),
                                {"intent_key": key, "attempt": attempt})
                return outcome
        held = sorted(set(dims) & set(self.held_dims(wid)))
        if held and not change.urgent:
            with self.conn.transaction():
                self._diary(f"held:{key}:{self.held_dims(wid)[held[0]]}", now, wid, "change_held", "wait",
                            f"not proposing '{change.summary}': supervisor recently set {held}; waiting for "
                            f"enough calls to judge their choice", {"dims": held})
            return "held"
        body = {**asdict(change), "dims": dims}
        with self.conn.transaction():
            row = self._q("SELECT change_id, status, body FROM changes WHERE run_id=%s AND intent_key=%s "
                          "AND status IN ('queued','in_flight')", r, key).fetchone()
            if row and row["status"] == "in_flight":
                old = row["body"]
                same = (sorted(old.get("attempts") or []) == sorted(change.attempts or []) if change.attempts
                        else old.get("patch") == change.patch)
                # Routine requests only: an urgent safety request (a harm stop, a dead number) whose
                # targets change is still wanted; withdrawing it would delay the fix (R3-H).
                shrunk = (not change.urgent and not old.get("urgent") and bool(change.attempts and old.get("attempts"))
                          and not set(old["attempts"]) <= set(change.attempts))
                if shrunk and not old.get("withdrawn"):
                    # R2-H (experiments E15): part of what the request carries is no longer wanted (e.g. a
                    # promotion withdrawn while a voice change still is). Never apply it; the feature's
                    # next submit builds a fresh request with only what is still wanted.
                    self._q("UPDATE changes SET body = body || '{\"withdrawn\": true}', updated_minute=%s "
                            "WHERE change_id=%s", now, row["change_id"])
                    self._diary(f"withdrawn:{row['change_id']}:{now}", now, wid, "change_dropped", "withdrawn",
                                f"part of the request out is no longer wanted ({sorted(set(old['attempts']) - set(change.attempts))}); "
                                f"it will not be applied, a fresh request follows", {"change_id": row["change_id"]})
                    return "in_flight"
                if old.get("withdrawn") and same:  # W1: wanted again before the answer came: un-withdraw
                    self._q("UPDATE changes SET body = body - 'withdrawn', updated_minute=%s WHERE change_id=%s",
                            now, row["change_id"])
                    self._diary(f"rewanted:{row['change_id']}:{now}", now, wid, "change_queued", "wanted_again",
                                f"wanted again while its request is out: '{change.summary}' will be applied if "
                                f"approved", {"change_id": row["change_id"]})
                return "in_flight"
            if row:
                # A new target restarts step counting (L4); an urgent new target skips the step wait (L5).
                # Compared by meaning (the targets), not by exact weights, which drift by rounding (N2).
                old = row["body"]
                new_target = (sorted(old.get("attempts") or []) != sorted(change.attempts or []) if change.attempts
                              else old.get("patch") != change.patch)
                self._q("UPDATE changes SET body=%s, urgent=%s, updated_minute=%s, "
                        "steps_done = CASE WHEN %s THEN 0 ELSE steps_done END, "
                        "not_before_minute = CASE WHEN %s THEN 0 ELSE not_before_minute END WHERE change_id=%s",
                        json.dumps(body), change.urgent, now, new_target, new_target and change.urgent,
                        row["change_id"])
                return "refreshed"
            cid = self._q("INSERT INTO changes (run_id, intent_key, workflow_id, urgent, status, created_minute, "
                          "updated_minute, body) VALUES (%s,%s,%s,%s,'queued',%s,%s,%s) RETURNING change_id",
                          r, key, wid, change.urgent, now, now, json.dumps(body)).fetchone()["change_id"]
            self._diary(f"queued:{cid}", now, wid, "change_queued", "queued",
                        f"{'URGENT ' if change.urgent else ''}{change.summary}",
                        {"change_id": cid, "intent_key": key, "dims": dims, "evidence": change.evidence})
        return "queued"

    def _attempt_record(self, intent_key: str, attempt: str, now: int) -> tuple[str | None, int]:
        """(None | 'cooling_down' | 'given_up', times turned down) for one target of an intent."""
        bad = self._q("SELECT count(*) AS n, max(updated_minute) AS last FROM changes WHERE run_id=%s AND "
                      "intent_key=%s AND status = ANY(%s) AND (body->'attempts' ? %s OR "
                      "(jsonb_typeof(body->'attempts') IS DISTINCT FROM 'array' AND intent_key = %s))",
                      self.run.run_id, intent_key, list(NOT_ACCEPTED), attempt, attempt).fetchone()
        if bad["n"] >= MAX_REJECTIONS:
            return "given_up", bad["n"]
        if bad["n"] and now - bad["last"] < REJECT_COOLDOWN_MIN:
            return "cooling_down", bad["n"]
        return None, bad["n"]

    def attempt_blocked(self, intent_key: str, attempt: str, now: int) -> str | None:
        """For features: is this target cooling down or given up (G17)? Leave it out of the change."""
        return self._attempt_record(intent_key, attempt, now)[0]

    def withdraw_unlisted(self, prefix: str, wanted: set[str], now: int) -> None:
        """F3, at once: a feature ran and no longer asks for these intents. Queued copies are dropped;
        a request already out is marked so it is never applied, even if approved later (N1)."""
        rows = self._q("SELECT * FROM changes WHERE run_id=%s AND starts_with(intent_key, %s) AND "
                       "status IN ('queued','in_flight') AND NOT (intent_key = ANY(%s)) AND "
                       "NOT (status = 'in_flight' AND coalesce((body->>'withdrawn')::bool, false))",
                       self.run.run_id, prefix, list(wanted)).fetchall()
        for c in rows:
            with self.conn.transaction():
                if c["status"] == "queued":
                    self._set_changes([c["change_id"]], "dropped", now)
                else:
                    self._q("UPDATE changes SET body = body || '{\"withdrawn\": true}' WHERE change_id=%s",
                            c["change_id"])
                self._diary(f"withdrawn:{c['change_id']}", now, c["workflow_id"], "change_dropped", "withdrawn",
                            f"no longer wanted: '{c['body']['summary']}'"
                            + (" (its request is already out; it will not be applied)" if c["status"] != "queued"
                               else ""), {"change_id": c["change_id"], "intent_key": c["intent_key"]})

    # -------------------------------------------------------------- every loop step
    def tick(self, now: int) -> None:
        self._process_holds(now)
        self._apply_approved(now)
        self._close_decided(now)
        self._drop_unwanted(now)
        for wid in self.wf:
            queued = self._queued(wid, now)
            mine = self._auto_step(wid, now, queued) if queued else set()
            queued = [c for c in self._queued(wid, now) if c["change_id"] not in mine]
            if queued:
                self._approval_step(wid, now, queued)

    def _drop_unwanted(self, now: int) -> None:
        """F3: a queued change its feature stopped asking for is no longer wanted."""
        with self.conn.transaction():
            for c in self._q("UPDATE changes SET status='dropped', updated_minute=%s WHERE run_id=%s AND "
                             "status='queued' AND body->>'ttl_min' IS NOT NULL AND "
                             "updated_minute + (body->>'ttl_min')::int < %s RETURNING *",
                             now, self.run.run_id, now).fetchall():
                self._diary(f"unwanted:{c['change_id']}", now, c["workflow_id"], "change_dropped", "dropped",
                            f"no longer wanted: '{c['body']['summary']}' was not re-submitted for "
                            f"{c['body']['ttl_min']} min, so the problem it fixed is gone"
                            + (f" (stopped after {c['steps_done']} step(s))" if c["steps_done"] else ""),
                            {"change_id": c["change_id"], "intent_key": c["intent_key"],
                             "steps_done": c["steps_done"]})

    # -------------------------------------------------------------- startup (G13)
    def reconcile(self, now: int) -> None:
        """Finish whatever a crash interrupted. Anything we can't explain stops the controller."""
        for p in self._q("SELECT * FROM proposals WHERE run_id=%s AND status IN ('sending','applying')",
                         self.run.run_id).fetchall():
            if p["status"] == "sending":
                self._transmit(p, now)
                continue
            st = self._sim_approval(p["workflow_id"], p["proposal_id"])
            if st is None:
                raise RuntimeError(f"{p['proposal_id']} is 'applying' but the sim has no such approval")
            if st["status"] == "applied":
                self._mark_applied(p, now, self._applied_version(p["workflow_id"], p["proposal_id"]))
            elif st["status"] == "approved":
                with self.conn.transaction():
                    self._set_proposal(p["proposal_id"], status="approved")  # _apply_approved retries
            elif st["status"] == "lapsed":
                self._close(p, now, "lapsed", requeue=True, reason="apply window passed during a restart; re-queued")
            else:
                raise RuntimeError(f"{p['proposal_id']} was 'applying'; sim says {st['status']}")

    # -------------------------------------------------------------- supervisor edits (G20, G21, D15)
    def _process_holds(self, now: int) -> None:
        r = self.run.run_id
        for h in self._q("SELECT * FROM holds WHERE run_id=%s AND NOT processed AND released_minute IS NULL",
                         r).fetchall():
            wid, dim = h["workflow_id"], h["dim"]
            with self.conn.transaction():
                dropped = self._q("UPDATE changes SET status='dropped', updated_minute=%s WHERE run_id=%s AND "
                                  "workflow_id=%s AND status='queued' AND body->'dims' ? %s RETURNING change_id",
                                  now, r, wid, dim).fetchall()
                superseded = self._q("UPDATE proposals SET status='superseded' WHERE run_id=%s AND workflow_id=%s "
                                     "AND status IN ('sending','pending','approved') AND %s = ANY(dims) "
                                     "RETURNING proposal_id, change_ids", r, wid, dim).fetchall()
                for p in superseded:  # its other changes go back to the queue; same-setting ones drop
                    self._q("UPDATE changes SET status = CASE WHEN body->'dims' ? %s THEN 'dropped' ELSE 'queued' "
                            "END, updated_minute=%s WHERE change_id = ANY(%s)", dim, now, p["change_ids"])
                self._q("UPDATE holds SET processed=true WHERE run_id=%s AND workflow_id=%s AND dim=%s "
                        "AND since_minute=%s", r, wid, dim, h["since_minute"])
                self._diary(f"hold:{wid}:{dim}:{h['since_minute']}", now, wid, "supervisor_hold", "hold",
                            f"supervisor changed {dim}: leaving it alone until {HOLD_MIN_CONNECTED} answered calls "
                            f"and the next morning; dropped {len(dropped)} queued change(s), superseded "
                            f"{len(superseded)} request(s)",
                            {"dropped": [d["change_id"] for d in dropped],
                             "superseded": [p["proposal_id"] for p in superseded]})
        for h in self._q("SELECT * FROM holds WHERE run_id=%s AND processed AND released_minute IS NULL",
                         r).fetchall():
            hours = self.wf[h["workflow_id"]]["governance"]["approver_hours"]
            next_morning = (h["since_minute"] // MINUTES_PER_DAY + 1) * MINUTES_PER_DAY + hours[0] * 60
            # Counts every answered call on the workflow since the edit: exact for workflow-wide settings
            # (prompt, default allocations), generous for a single cohort's setting.
            connected = self._q("SELECT coalesce(sum(connected),0) AS n FROM call_stats WHERE run_id=%s AND "
                                "workflow_id=%s AND sim_day * %s + hour * 60 >= %s", r, h["workflow_id"],
                                MINUTES_PER_DAY, h["since_minute"]).fetchone()["n"]
            if now >= next_morning and connected >= HOLD_MIN_CONNECTED:
                with self.conn.transaction():
                    self._q("UPDATE holds SET released_minute=%s WHERE run_id=%s AND workflow_id=%s AND dim=%s "
                            "AND since_minute=%s", now, r, h["workflow_id"], h["dim"], h["since_minute"])
                    self._diary(f"hold_released:{h['workflow_id']}:{h['dim']}:{h['since_minute']}", now,
                                h["workflow_id"], "supervisor_hold", "released",
                                f"{h['dim']}: {connected} answered calls since the supervisor's change; open to "
                                f"proposals again", {"connected": connected})

    # -------------------------------------------------------------- approved -> applied (G15, G16)
    def _apply_approved(self, now: int) -> None:
        for p in self._q("SELECT * FROM proposals WHERE run_id=%s AND status='approved' AND path='approval'",
                         self.run.run_id).fetchall():
            wid = p["workflow_id"]
            withdrawn = self._q("SELECT change_id, coalesce((body->>'withdrawn')::bool, false) AS w FROM changes "
                                "WHERE change_id = ANY(%s)", list(p["change_ids"])).fetchall()
            if any(x["w"] for x in withdrawn):  # N1: approved, but no longer wanted: don't apply
                with self.conn.transaction():
                    self._set_proposal(p["proposal_id"], status="withdrawn")
                    self._set_changes([x["change_id"] for x in withdrawn if x["w"]], "dropped", now)
                    self._set_changes([x["change_id"] for x in withdrawn if not x["w"]], "queued", now)
                    self._diary(f"withdrawn:{p['proposal_id']}", now, wid, "request_withdrawn", "withdrawn",
                                f"approved, but no longer wanted, so not applied (lets the apply window lapse): "
                                f"{p['request']['change_summary']}",
                                {"proposal_id": p["proposal_id"], "approval_id": p["approval_id"]})
                continue
            current = self.sim.config(wid)["version"]
            if current != p["base_version"]:
                self._close(p, now, "stale", requeue=True,
                            reason=f"approved, but config moved v{p['base_version']} -> v{current} meanwhile; "
                                   f"not applying, re-queued to propose fresh")
                continue
            with self.conn.transaction():
                self._set_proposal(p["proposal_id"], status="applying")  # G12
            code, body = self.sim.send("POST", f"/workflows/{wid}/apply", {"approval_id": p["approval_id"]})
            if code == 200:
                self._mark_applied(p, now, body["version"])
            elif code == 409 and body.get("error") == "not_approved" and body["detail"]["status"] == "applied":
                self._mark_applied(p, now, self._applied_version(wid, p["proposal_id"]))
            elif code == 410:
                self._close(p, now, "lapsed", requeue=True, reason="apply window passed; re-queued")
            elif code == 409 and body.get("error") == "stale_base_version":
                self._close(p, now, "stale", requeue=True, reason="config changed just before apply; re-queued")
            else:
                raise RuntimeError(f"apply {p['proposal_id']} failed: {code} {body}")

    def _mark_applied(self, p: dict, now: int, version: int) -> None:
        via = f"approved {p['approval_id']}" if p["path"] == "approval" else "auto-applied within policy"
        with self.conn.transaction():
            self._set_proposal(p["proposal_id"], status="applied", applied_version=version)
            if p["partial"]:  # T3: back to the queue; the next step waits step_gap minutes
                self._q("UPDATE changes SET status='queued', steps_done=steps_done+1, not_before_minute=%s, "
                        "updated_minute=%s WHERE change_id = ANY(%s)", now + self.step_gap, now,
                        list(p["change_ids"]))
                via += f"; next step not before {store.sim_label(now + self.step_gap)}"
            else:
                self._set_changes(p["change_ids"], "applied", now)
            self._diary(f"applied:{p['proposal_id']}", now, p["workflow_id"], "change_applied", "applied",
                        f"{p['request']['change_summary']} (now config v{version}, {via})",
                        {"proposal_id": p["proposal_id"], "approval_id": p["approval_id"], "version": version,
                         "dims": p["dims"], "partial": p["partial"]})

    def _close_decided(self, now: int) -> None:
        """Rejected / expired requests release their changes (G17, G18). The diary line came from ingest."""
        for p in self._q("SELECT * FROM proposals WHERE run_id=%s AND status IN ('rejected','expired') "
                         "AND EXISTS (SELECT 1 FROM changes c WHERE c.change_id = ANY(change_ids) "
                         "AND c.status='in_flight')", self.run.run_id).fetchall():
            with self.conn.transaction():
                # R3-M: a request we had already withdrawn is closed as dropped, so the approver's "no"
                # never counts against targets we still want (G17)
                self._q("UPDATE changes SET status = CASE WHEN coalesce((body->>'withdrawn')::bool, false) "
                        "THEN 'dropped' ELSE %s END, updated_minute=%s WHERE change_id = ANY(%s)",
                        p["status"], now, list(p["change_ids"]))

    def _close(self, p: dict, now: int, status: str, *, requeue: bool, reason: str,
               change_status: str = "dropped") -> None:
        with self.conn.transaction():
            self._set_proposal(p["proposal_id"], status=status)
            self._set_changes(p["change_ids"], "queued" if requeue else change_status, now)
            if requeue:  # R2-M3: its feature refreshes the body on the next step before it is re-sent
                self._q("UPDATE changes SET not_before_minute = %s WHERE change_id = ANY(%s)",
                        now + self.step, list(p["change_ids"]))
            self._diary(f"{status}:{p['proposal_id']}", now, p["workflow_id"], f"request_{status}", status,
                        reason, {"proposal_id": p["proposal_id"], "approval_id": p["approval_id"]})

    # -------------------------------------------------------------- sending (G2-G8)
    def avg_latency(self, wid: str) -> float:
        """G7 / D17: this manager's measured average answer time (same-day answers), else the policy
        range's midpoint."""
        lo, hi = self.wf[wid]["governance"]["approver_latency_minutes"]
        row = self._q("SELECT count(*) AS n, avg(decided_minute - sent_minute) AS avg FROM proposals WHERE "
                      "run_id=%s AND workflow_id=%s AND path='approval' AND decided_minute IS NOT NULL AND "
                      "status <> 'expired' AND decided_minute / %s = sent_minute / %s", self.run.run_id, wid,
                      MINUTES_PER_DAY, MINUTES_PER_DAY).fetchone()
        return float(row["avg"]) if row["n"] >= MIN_LATENCY_SAMPLES else (lo + hi) / 2

    def _queued(self, wid: str, now: int | None = None) -> list[dict]:
        """Queued changes, minus stepped ones still waiting for their next step (T3)."""
        return self._q("SELECT * FROM changes WHERE run_id=%s AND workflow_id=%s AND status='queued' "
                       "AND (%s::int IS NULL OR not_before_minute <= %s) ORDER BY urgent DESC, created_minute",
                       self.run.run_id, wid, now, now).fetchall()

    def _open(self, wid: str, path: str) -> list[dict]:
        return self._q("SELECT * FROM proposals WHERE run_id=%s AND workflow_id=%s AND path=%s AND status = ANY(%s)",
                       self.run.run_id, wid, path, list(OPEN)).fetchall()

    def _problems(self, wid: str, c: dict, path: str) -> list[str]:
        """G4: what would make the sim refuse this, checked on both paths (H2)."""
        b, gov = c["body"], self.wf[wid]["governance"]
        problems = config_ops.option_problems(self.wf[wid]["options"], b["patch"])
        problems += config_ops.schedule_problems(b["patch"], self.wf[wid]["dialling_window"])  # R2
        if not b["summary"].strip() or not b["rationale"].strip():
            problems.append("summary and rationale must be non-empty")
        if path == "approval":
            if not b["dry_run"]:
                problems.append("dry_run must be non-empty")
            if not isinstance(b["affected_leads"], int) or b["affected_leads"] < 0:
                problems.append("affected_leads must be a non-negative int")
            if not 0 <= b["impact_score"] <= 1:
                problems.append("impact_score must be in [0,1]")
            if "prompt_version" in b["dims"]:
                ev = b.get("evidence") or {}
                if not ev.get("validation_run_id"):
                    problems.append("prompt change without a validation run")
                elif ev.get("validation_pass_rate", 0) < gov["min_validation_pass_rate"]:
                    problems.append(f"validation pass rate {ev.get('validation_pass_rate')} below "
                                    f"{gov['min_validation_pass_rate']}")
        return problems

    def _drop(self, c: dict, now: int, problems: list[str]) -> None:
        with self.conn.transaction():
            self._set_changes([c["change_id"]], "dropped", now)
            # R3: keyed on the idea + its problems, so a feature resubmitting the same bad change
            # every tick writes one diary line, not one per tick
            digest = hashlib.md5("; ".join(problems).encode()).hexdigest()[:8]
            self._diary(f"dropped:{c['body']['intent_key']}:{digest}", now, c["workflow_id"], "change_dropped",
                        "dropped",
                        f"not sending '{c['body']['summary']}': {'; '.join(problems)}", {"problems": problems})

    def _auto_step(self, wid: str, now: int, queued: list[dict]) -> set[int]:
        """G2, G3, T3: apply what the policy lets us change alone, in steps of at most max_shift.
        Never on a supervisor-held setting (M2), and non-urgent changes wait while a request is out,
        since any apply would make it stale (D14). Returns the ids of every change that belongs to
        this path (applied, stepped, dropped or waiting), so none of them goes to the approver."""
        gov = self.wf[wid]["governance"]
        auto = gov.get("auto_apply") or {}
        if not auto.get("enabled"):
            return set()
        allowed, held = set(auto.get("allowed_dims", [])), set(self.held_dims(wid))
        limit = auto.get("max_shift", 0)
        request_out = bool(self._open(wid, "approval"))
        cfg = self.sim.config(wid)
        base = cfg["config"]
        mine, taken, merged, step = set(), [], base, None
        for c in queued:
            dims = set(c["body"]["dims"])
            if not dims <= allowed or dims & held:
                continue  # T4 / M2: the approver's
            mine.add(c["change_id"])
            problems = self._problems(wid, c, "auto")
            if problems:
                self._drop(c, now, problems)
                continue
            if (request_out and not c["urgent"]) or step:
                continue  # waits: D14, or a step already goes out this tick
            trial = config_ops.apply_patch(merged, c["body"]["patch"])
            if not config_ops.changed_dims(base, trial) or not config_ops.auto_apply_problems(gov, base, trial):
                taken.append(c)  # fits in one go (or is a no-op, which _propose drops)
                merged = trial
                continue
            stepped = config_ops.step_config(base, trial, limit)
            if stepped is None or config_ops.auto_apply_problems(gov, base, stepped):
                mine.discard(c["change_id"])  # no safe step exists: ask for the whole thing (T4)
            elif not taken:  # a step goes alone, so its "step k of N" is about one change
                n = c["steps_done"] + config_ops.steps_needed(base, trial, limit)
                taken, merged, step = [c], stepped, (c["steps_done"] + 1, n)
        if taken:
            self._propose(wid, now, "auto", cfg, merged, taken, step=step)
        return mine

    def _approval_step(self, wid: str, now: int, queued: list[dict]) -> None:
        """G5-G8: one bundled morning note; urgent changes alone may go earlier (M3)."""
        gov = self.wf[wid]["governance"]
        hours = gov["approver_hours"]
        urgent = [c for c in queued if c["urgent"]]
        out = self._open(wid, "approval")
        if out and (not urgent or any(p["urgent"] for p in out)):
            return  # D13: one request at a time, except urgent may join a normal one
        if in_morning_window(now, hours, self.step) and not out:
            batch, tagged = queued, bool(urgent)   # the morning note carries everything queued
        elif urgent and urgent_can_send_now(now, hours, self.avg_latency(wid)):
            batch, tagged = urgent, True
        elif urgent and in_morning_window(now, hours, self.step):
            batch, tagged = urgent, True
        else:
            return
        used = self._q("SELECT count(*) AS n FROM proposals WHERE run_id=%s AND workflow_id=%s AND path='approval' "
                       "AND sent_minute > %s AND (approval_id IS NOT NULL OR status='sending')",
                       self.run.run_id, wid, now - MINUTES_PER_DAY).fetchone()["n"]  # M4: what the sim counts
        if not slots_free(gov["max_requests_per_24h"], used, tagged):
            with self.conn.transaction():
                self._diary(f"no_slot:{wid}:{now // MINUTES_PER_DAY}:{tagged}", now, wid, "request_deferred",
                            "wait", f"{used} of {gov['max_requests_per_24h']} daily requests used; waiting",
                            {"used": used, "urgent": tagged})
            return
        held = self.held_dims(wid)
        cfg = self.sim.config(wid)
        send, merged, prompt_taken = [], cfg["config"], False
        for c in sorted(batch, key=lambda c: -c["updated_minute"]):  # newest first, for the prompt rule
            problems = self._problems(wid, c, "approval")
            if problems:
                self._drop(c, now, problems)
                continue
            if "prompt_version" in c["body"]["dims"]:
                if prompt_taken:
                    continue  # M6: one prompt per request, its evidence must match; the rest wait
                prompt_taken = True
            send.append(c)
        for c in sorted(send, key=lambda c: c["created_minute"]):
            merged = config_ops.apply_patch(merged, c["body"]["patch"])
        if send:
            overrides = sorted({d for c in send for d in c["body"]["dims"]} & set(held))
            self._propose(wid, now, "approval", cfg, merged, send, tagged=tagged, overrides=overrides)

    def _propose(self, wid: str, now: int, path: str, cfg: dict, merged: dict, changes: list[dict], *,
                 tagged: bool = False, overrides: list[str] = (), step: tuple[int, int] | None = None) -> None:
        dims = config_ops.changed_dims(cfg["config"], merged)
        if not dims:
            for c in changes:
                self._drop(c, now, ["already in the current config"])
            return
        bodies = [c["body"] for c in changes]
        # G7: the URGENT tag marks the urgent items only, never the normal ones riding along
        summary = "; ".join(("[URGENT] " if c["urgent"] else "") + c["body"]["summary"]
                            for c in sorted(changes, key=lambda c: not c["urgent"]))
        if overrides:  # M2: never override a supervisor quietly
            summary += f" (overrides a recent supervisor setting: {', '.join(overrides)})"
        if step:
            summary = f"step {step[0]} of {step[1]}: {summary}"
        elif len(changes) == 1 and changes[0]["steps_done"]:
            summary = f"final step {changes[0]['steps_done'] + 1} of {changes[0]['steps_done'] + 1}: {summary}"
        evidence = {"items": {b["intent_key"]: b["evidence"] for b in bodies}}
        val = next((b["evidence"].get("validation_run_id") for b in bodies if "prompt_version" in b["dims"]), None)
        if val:
            evidence["validation_run_id"] = val
        with self.conn.transaction():  # G10: write it down before it leaves
            n = self._q("SELECT count(*) AS n FROM proposals WHERE run_id=%s AND workflow_id=%s",
                        self.run.run_id, wid).fetchone()["n"]
            pid = f"{self.run.token}-{wid}-{n + 1}"  # L2: token keeps ids unique across DB resets
            request = {"proposal_id": pid, "base_version": cfg["version"], "proposed_config": merged,
                       "rationale": " | ".join(b["rationale"] for b in bodies), "evidence": evidence,
                       "change_summary": summary}
            if path == "approval":
                request |= {"workflow_id": wid, "diff": {b["intent_key"]: b["patch"] for b in bodies},
                            "affected_leads": int(sum(b["affected_leads"] for b in bodies)),
                            "dry_run": {b["intent_key"]: b["dry_run"] for b in bodies},
                            "impact_score": max(b["impact_score"] for b in bodies)}
            self._q("INSERT INTO proposals (run_id, proposal_id, workflow_id, path, urgent, status, base_version, "
                    "dims, sent_minute, change_ids, request, partial) "
                    "VALUES (%s,%s,%s,%s,%s,'sending',%s,%s,%s,%s,%s,%s)",
                    self.run.run_id, pid, wid, path, tagged, cfg["version"], dims, now,
                    [c["change_id"] for c in changes], json.dumps(request), bool(step and step[0] < step[1]))
            self._q("UPDATE changes SET status='in_flight', proposal_id=%s, updated_minute=%s "
                    "WHERE change_id = ANY(%s)", pid, now, [c["change_id"] for c in changes])
        self._transmit(self._proposal(pid), now)

    # -------------------------------------------------------------- the wire (G11)
    def _transmit(self, p: dict, now: int) -> None:
        """Send, or resend after a crash, a recorded request. Safe to repeat."""
        if p["path"] == "approval":
            self._transmit_approval(p, now)
        else:
            self._transmit_auto(p, now)

    def _transmit_approval(self, p: dict, now: int) -> None:
        wid, req = p["workflow_id"], p["request"]
        code, body = self.sim.send("POST", "/approvals", req)
        if code == 409 and body.get("error") == "duplicate_proposal_id":
            self._adopt_existing(p, now)  # it arrived before the crash
        elif code == 200:
            with self.conn.transaction():
                self._set_proposal(p["proposal_id"], status=body["status"], approval_id=body["approval_id"])
                self._diary(f"sent:{p['proposal_id']}", now, wid, "request_sent", "sent",
                            f"asked the client approver: {req['change_summary']}",
                            {"proposal_id": p["proposal_id"], "approval_id": body["approval_id"],
                             "urgent": p["urgent"], "expected_answer_in_min": round(self.avg_latency(wid)),
                             "base_version": p["base_version"], "dims": p["dims"],
                             "affected_leads": req["affected_leads"], "impact_score": req["impact_score"]})
        else:
            self._refused_or_stale(p, now, code, body)

    def _adopt_existing(self, p: dict, now: int) -> None:
        """After a crash: the sim already has this request. Record it as the sim sees it (L5)."""
        a = self._sim_approval(p["workflow_id"], p["proposal_id"])
        cols = {"status": a["status"], "approval_id": a["approval_id"]}
        if a.get("decided_at"):
            cols |= {"decided_minute": store.sim_minute_of(self.run, a["decided_at"]),
                     "reasons": json.dumps(a.get("reasons"))}
        with self.conn.transaction():
            self._set_proposal(p["proposal_id"], **cols)
            self._diary(f"sent:{p['proposal_id']}", now, p["workflow_id"], "request_sent", "sent",
                        f"asked the client approver (confirmed after restart): {p['request']['change_summary']}",
                        {"proposal_id": p["proposal_id"], "approval_id": a["approval_id"], "status": a["status"],
                         "urgent": p["urgent"]})

    def _transmit_auto(self, p: dict, now: int) -> None:
        wid = p["workflow_id"]
        code, body = self.sim.send("PUT", f"/workflows/{wid}/config", p["request"])
        if code == 409 and body.get("error") == "duplicate_proposal_id":
            self._mark_applied(p, now, self._applied_version(wid, p["proposal_id"]))
        elif code == 200:
            self._mark_applied(p, now, body["version"])
        else:
            self._refused_or_stale(p, now, code, body)

    def _refused_or_stale(self, p: dict, now: int, code: int, body: dict) -> None:
        if code == 409 and body.get("error") == "stale_base_version":
            self._close(p, now, "stale", requeue=True, reason="config changed before our request went out; re-queued")
            return
        # 403 / 422: our local checks disagree with the sim. Record it, and mark the changes 'refused' so
        # submit() counts it like a rejection instead of resending every tick (H2).
        self._close(p, now, "refused", requeue=False, change_status="refused",
                    reason=f"sim refused the request: {code} {body}")
        log.error("request %s refused: %s %s", p["proposal_id"], code, body)
