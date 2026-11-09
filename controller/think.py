"""Decision step. In the skeleton it only observes (S8): it never proposes or applies a change.

Diary policy (S19, option B): one daily summary per workflow, plus every real decision.
Real decisions get added here as capabilities are built.
"""
from __future__ import annotations

import psycopg

from . import store
from .store import MINUTES_PER_DAY


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def write_daily_summaries(conn: psycopg.Connection, run: store.Run, workflow_ids: list[str],
                          from_day: int = 0) -> int:
    """Write one summary row per workflow for each fully ingested sim day in [from_day, today).

    Idempotent: a day already summarised is skipped by its decision_key.
    """
    written = 0
    for day in range(from_day, run.sim_minute // MINUTES_PER_DAY):
        totals = store.day_totals(conn, run, day)
        end_minute = (day + 1) * MINUTES_PER_DAY
        for wid in workflow_ids:
            t = totals.get(wid) or {"dials": 0, "connected": 0, "engaged": 0, "hallucinations": 0}
            inputs = {
                "sim_day": day,
                "dials": t["dials"], "connected": t["connected"], "engaged": t["engaged"],
                "hallucinations": t["hallucinations"],
                "pickup_rate": _rate(t["connected"], t["dials"]),
                "engagement_rate": _rate(t["engaged"], t["connected"]),
                "hallucination_rate": _rate(t["hallucinations"], t["connected"]),
            }
            written += store.record_decision(
                conn, run, key=f"daily_summary:{wid}:D{day}", sim_minute=end_minute,
                workflow_id=wid, kind="daily_summary", action="no_action",
                reason=(f"D{day}: {t['dials']} dials, pickup {inputs['pickup_rate']}, "
                        f"engagement {inputs['engagement_rate']}. Observe-only skeleton: no change considered."),
                inputs=inputs)
    return written
