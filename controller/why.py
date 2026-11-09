"""C12: "Why did workflow X change at 14:00 on D3, who approved it, and what happened next?"

Answered from the controller's own records only (the diary, our requests and our changes), never
from the simulator. Read-only: safe to run while the controller is running.

    python -m controller.why ks_cancel "D3 14:00"        # the config change nearest that time (all
                                                          #   changes at that same minute), within --window
    python -m controller.why ks_cancel --list             # every config change of the workflow
    python -m controller.why ks_cancel "D3 14:00" --dsn postgresql://... --scenario public_02_incidents

For each config change it prints: what changed and who did it; for our own changes, which feature
wanted it, the evidence and numbers it was based on, how it was approved (client approver: when
asked, when decided; or auto-applied within the workflow's policy); and what happened next (the
feature's follow-up lines, and the workflow's KPIs in the 24 h before vs after).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg

from . import store
from .main import DEFAULT_DSN
from .store import MINUTES_PER_DAY, sim_label

FEATURE_KINDS = ("caller_id_health", "script_health", "experiments")
QUIET = ("no_winner_yet", "no_action")   # routine status lines, left out of "what happened next"
NEXT_HOURS = 24


def parse_label(text: str) -> int:
    """'D3 14:00' -> sim minute."""
    m = re.fullmatch(r"\s*D(-?\d+)\s+(\d{1,2}):(\d{2})\s*", text)
    if not m:
        raise ValueError(f"time must look like 'D3 14:00', got {text!r}")
    return int(m[1]) * MINUTES_PER_DAY + int(m[2]) * 60 + int(m[3])


def find_run(conn, scenario: str | None) -> int:
    row = conn.execute("SELECT run_id FROM runs WHERE (%s::text IS NULL OR scenario = %s) ORDER BY run_id DESC "
                       "LIMIT 1", (scenario, scenario)).fetchone()
    if not row:
        raise SystemExit("no controller run found" + (f" for scenario {scenario}" if scenario else ""))
    return row["run_id"]


def config_changes(conn, run_id: int, wid: str) -> list[dict]:
    return conn.execute("SELECT sim_minute, inputs FROM decisions WHERE run_id=%s AND workflow_id=%s AND "
                        "kind='config_observed' ORDER BY sim_minute, decision_id", (run_id, wid)).fetchall()


def kpis(conn, run_id: int, wid: str, since: int, until: int) -> dict:
    r = conn.execute(
        f"""SELECT coalesce(sum(dials),0)::int d, coalesce(sum(connected),0)::int c,
                   coalesce(sum(engaged),0)::int e, coalesce(sum(hallucinations),0)::int h
            FROM call_stats WHERE run_id=%s AND workflow_id=%s
              AND sim_day * {MINUTES_PER_DAY} + hour * 60 >= %s AND sim_day * {MINUTES_PER_DAY} + hour * 60 < %s""",
        (run_id, wid, since, until)).fetchone()
    pct = lambda a, b: f"{a / b:.1%}" if b else "n/a"  # noqa: E731
    return {"calls": r["d"], "pickup": pct(r["c"], r["d"]), "engagement": pct(r["e"], r["c"]),
            "made_up": pct(r["h"], r["c"])}


def explain(conn, run_id: int, wid: str, minute: int, inputs: dict) -> list[str]:
    actor, version = inputs.get("actor", "?"), inputs.get("version", "?")
    dims = ", ".join(inputs.get("changed_dims") or []) or "?"
    out = [f"== {wid} config v{version} at {sim_label(minute)}: {dims} changed by {actor}"]
    end = minute + NEXT_HOURS * 60
    if actor not in ("supervisor", "controller_approved", "controller_auto"):
        out.append(f"WHO: {actor} (not the controller and not a supervisor edit we know of)")
    elif actor == "supervisor":
        out.append("WHO: the client's supervisor, directly (supervisor edits bypass governance; not our change).")
        reactions = conn.execute(
            "SELECT sim_ts, kind, reason FROM decisions WHERE run_id=%s AND workflow_id=%s AND sim_minute >= %s "
            "AND sim_minute < %s AND (kind IN ('supervisor_hold','request_superseded') OR action IN "
            "('supervisor_owned','forget')) ORDER BY decision_id", (run_id, wid, minute, end)).fetchall()
        out.append("OUR REACTION:" if reactions else "OUR REACTION: none recorded")
        out += [f"  {r['sim_ts']}  {r['reason']}" for r in reactions]
    else:
        out += _our_change(conn, run_id, wid, minute, inputs)
    out += _next(conn, run_id, wid, minute, end, skip_holds=actor == "supervisor")
    hour = minute // 60 * 60  # counters are hourly: "before" ends at the change's hour, "after" starts at the next
    before, after = kpis(conn, run_id, wid, hour - MINUTES_PER_DAY, hour), kpis(conn, run_id, wid, hour + 60, hour + 60 + NEXT_HOURS * 60)
    out.append(f"KPIs 24 h before: {before['calls']} calls, pickup {before['pickup']}, engagement "
               f"{before['engagement']}, made-up {before['made_up']}")
    out.append(f"KPIs 24 h after:  {after['calls']} calls, pickup {after['pickup']}, engagement "
               f"{after['engagement']}, made-up {after['made_up']}  (other changes in that window also count)")
    return out


def _our_change(conn, run_id, wid, minute, inputs) -> list[str]:
    pid = inputs.get("proposal_id")
    p = conn.execute("SELECT * FROM proposals WHERE run_id=%s AND proposal_id=%s", (run_id, pid)).fetchone()
    if not p:
        return [f"WHO: controller, request {pid} (not found in our records)"]
    req = p["request"]
    out = [f"WHAT: {req['change_summary']}"]
    if p["path"] == "approval":
        decided = (f"decided {sim_label(p['decided_minute'])} ({p['decided_minute'] - p['sent_minute']} min later)"
                   if p["decided_minute"] is not None else "decision time not recorded")
        out.append(f"WHO APPROVED: the client approver, approval {p['approval_id']}: asked {sim_label(p['sent_minute'])}"
                   f"{' as URGENT' if p['urgent'] else ''}, {decided}; we applied it {sim_label(minute)}")
        out.append(f"  sent with: {req.get('affected_leads')} affected leads, impact score {req.get('impact_score')}"
                   + (f", validation run {req['evidence']['validation_run_id']}"
                      if (req.get("evidence") or {}).get("validation_run_id") else ""))
    else:
        out.append("WHO APPROVED: no human; auto-applied by the controller because the workflow's own policy allows "
                   f"it for {', '.join(p['dims'])} (within max_shift)")
    out.append(f"WHY: {req['rationale']}")
    changes = conn.execute("SELECT change_id, intent_key, created_minute, body FROM changes WHERE change_id = ANY(%s) "
                           "ORDER BY change_id", (list(p["change_ids"]),)).fetchall()
    for c in changes:
        feature = c["intent_key"].split(":")[0]
        out.append(f"  wanted by {feature} since {sim_label(c['created_minute'])} (idea {c['intent_key']})")
        if feature in FEATURE_KINDS:
            lines = conn.execute(
                "SELECT sim_ts, action, reason FROM decisions WHERE run_id=%s AND workflow_id=%s AND kind=%s AND "
                "sim_minute >= %s AND sim_minute <= %s AND action <> ALL(%s) ORDER BY decision_id",
                (run_id, wid, feature, c["created_minute"] - 60, minute, list(QUIET))).fetchall()
            out += [f"    {r['sim_ts']}  [{r['action']}] {r['reason']}" for r in lines[-8:]]
    return out


def _next(conn, run_id, wid, minute, end, skip_holds=False) -> list[str]:
    rows = conn.execute(
        "SELECT sim_ts, kind, action, reason FROM decisions WHERE run_id=%s AND workflow_id=%s AND sim_minute > %s "
        "AND sim_minute < %s AND (kind = ANY(%s) OR kind IN ('config_observed','request_rejected','request_stale',"
        "'request_withdrawn','supervisor_hold')) AND action <> ALL(%s) ORDER BY decision_id",
        (run_id, wid, minute, end, list(FEATURE_KINDS), list(QUIET))).fetchall()
    if skip_holds:  # already shown as our reaction
        rows = [r for r in rows if r["kind"] != "supervisor_hold"]
    out = [f"WHAT HAPPENED NEXT ({NEXT_HOURS} h):" if rows else f"WHAT HAPPENED NEXT ({NEXT_HOURS} h): nothing recorded"]
    out += [f"  {r['sim_ts']}  [{r['kind']}/{r['action']}] {r['reason']}" for r in rows[:15]]
    if len(rows) > 15:
        out.append(f"  ... {len(rows) - 15} more lines in the diary")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Why did a workflow's config change? (from the controller's records)")
    ap.add_argument("workflow")
    ap.add_argument("time", nargs="?", help="sim time like 'D3 14:00'")
    ap.add_argument("--list", action="store_true", help="list every config change of the workflow")
    ap.add_argument("--window", type=int, default=120,
                    help="how far from the time a change may be to count as 'nearest' (minutes, default 120)")
    ap.add_argument("--scenario", help="which run (default: the latest)")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    a = ap.parse_args(argv)
    if not a.list and not a.time:
        ap.error("give a time like 'D3 14:00', or --list")
    with psycopg.connect(a.dsn, row_factory=psycopg.rows.dict_row) as conn:
        run_id = find_run(conn, a.scenario)
        rows = config_changes(conn, run_id, a.workflow)
        if not rows:
            print(f"no config changes recorded for {a.workflow}")
            return 1
        if a.list:
            for r in rows:
                i = r["inputs"]
                print(f"{sim_label(r['sim_minute'])}  v{i.get('version')}  {i.get('actor')}  "
                      f"{', '.join(i.get('changed_dims') or [])}")
            return 0
        t = parse_label(a.time)
        best = min(abs(r["sim_minute"] - t) for r in rows)
        if best <= a.window:  # the nearest change, and any others at that same minute
            near = [r for r in rows if abs(r["sim_minute"] - t) == best]
            near = [r for r in near if r["sim_minute"] == near[0]["sim_minute"]]
        else:
            before = [r for r in rows if r["sim_minute"] <= t]
            near = before[-1:] or rows[:1]
            print(f"(no config change within {a.window} min of {sim_label(t)}; showing "
                  + ("the one in force then)\n" if before else "the first one)\n"))
        for r in near:
            print("\n".join(explain(conn, run_id, a.workflow, r["sim_minute"], r["inputs"])), end="\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
