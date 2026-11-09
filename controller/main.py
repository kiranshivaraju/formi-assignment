"""Controller entry point.

    python -m controller.main --url http://127.0.0.1:8700

Loop (S12): ingest everything the sim has produced, think, then advance. A restart always begins
at ingest, so the controller never moves the clock while holding unread events.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import httpx
import psycopg

from . import settings as settings_mod
from . import store, think
from .client import SimClient
from .features import Context, run_features
from .governance import Governor

DEFAULT_DSN = f"postgresql://formi:formi@127.0.0.1:{os.environ.get('FORMI_PG_PORT', '55432')}/formi"
log = logging.getLogger("controller")


def emit(event: str, **fields) -> None:
    """One JSON line per loop step (S27)."""
    print(json.dumps({"event": event, "wall_ts": round(time.time(), 3), **fields}), flush=True)


def ingest_pending(sim: SimClient, conn: psycopg.Connection, run: store.Run, kinds: tuple[str, ...]) -> int:
    """Fetch and save every chunk after the bookmark (S11). Returns events saved."""
    pending = sorted((c for k in kinds for c in sim.list_chunks(k, run.last_chunk)),
                     key=lambda c: c["chunk"])
    total = 0
    for chunk in pending:
        with sim.stream_chunk(chunk["chunk"]) as lines:
            total += store.ingest_chunk(conn, run, chunk, lines)
    return total


class FatalConfig(Exception):
    """A problem a restart cannot fix (bad flag, wrong schema, world already driven)."""


def run_controller(sim: SimClient, conn: psycopg.Connection, sim_url: str, step: int | None,
                   settings: settings_mod.Settings | None = None) -> store.Run:
    settings = settings or settings_mod.load()
    state = sim.state()
    tick = state["tick_minutes"]  # read from the API, not assumed
    step = step or tick
    if step <= 0 or step % tick:
        raise FatalConfig(f"--step {step} must be a positive multiple of the sim tick ({tick} min)")
    if not store.lock_sim(conn, sim_url):
        raise FatalConfig(f"another controller is already driving {sim_url}")
    run, created = store.resolve_run(conn, sim_url, state)
    emit("run_resolved", run_id=run.run_id, created=created, scenario=run.scenario,
         resume_from_chunk=run.last_chunk, sim_minute=state["sim_minute"])
    workflows = sim.workflows()
    workflow_ids = [w["workflow_id"] for w in workflows]
    if unknown := settings.unknown_workflows(workflow_ids):
        raise FatalConfig(f"settings name workflows this simulator doesn't have: {unknown}")

    t0 = time.perf_counter()
    n = ingest_pending(sim, conn, run, ("history", "events"))  # S13: history, same path
    emit("history_ingested", events=n, seconds=round(time.perf_counter() - t0, 2))
    gov = Governor(sim, conn, run, workflows, step=step, settings=settings)
    wf_by_id = {w['workflow_id']: w for w in workflows}
    gov.reconcile(state["sim_minute"])  # G13: finish anything a crash left half-done

    summarised_day = 0  # first call re-checks every day; decision_key makes repeats no-ops
    pruned_day = None   # D6: prune once per sim day (and once after the history load)
    keep_days = settings.storage("raw_event_days")
    while True:
        state = sim.state()
        if store.is_new_world(run, state):
            raise RuntimeError("simulator was restarted under us; refusing to mix worlds (S22)")
        t0 = time.perf_counter()
        n = ingest_pending(sim, conn, run, ("events",))
        store.mark_covered(conn, run, state["sim_minute"])
        t_ingest = time.perf_counter() - t0
        written = think.write_daily_summaries(conn, run, workflow_ids, from_day=summarised_day)
        submitted = run_features(Context(conn, run, sim, gov, settings, wf_by_id, state["sim_minute"],
                                         sim_state=state))
        gov.tick(state["sim_minute"])
        summarised_day = run.sim_minute // store.MINUTES_PER_DAY
        if pruned_day != summarised_day:
            pruned_day = summarised_day
            cutoff = (summarised_day - keep_days) * store.MINUTES_PER_DAY
            t0 = time.perf_counter()
            pruned = store.prune_raw_events(conn, run, cutoff, state["sim_minute"])
            if pruned:
                emit("pruned", rows=pruned, before_minute=cutoff, seconds=round(time.perf_counter() - t0, 2))
        emit("step", sim_minute=run.sim_minute, events=n, ingest_s=round(t_ingest, 3),
             decisions_written=written, last_event_id=run.last_event_id,
             **({"changes": submitted} if submitted else {}))
        if state["finished"]:
            if run.last_event_id < state["last_event_id"]:  # would otherwise spin forever
                raise RuntimeError(f"sim finished at event {state['last_event_id']} but only "
                                   f"{run.last_event_id} ingested after a full pass")
            emit("finished", run_id=run.run_id, last_event_id=run.last_event_id)
            return run
        sim.advance_to(state["sim_minute"] + step)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--url", required=True, help="simulator base URL")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    ap.add_argument("--settings", default=str(settings_mod.DEFAULT_PATH), help="checker knobs (YAML)")
    ap.add_argument("--step", type=int, default=None,
                    help="sim minutes per loop step; default one sim tick (S7)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP call is noise
    # Exit codes for run.sh: 0 finished; 2 fatal config (don't restart); 75 lost a connection;
    # anything else is a bug. run.sh restarts on 75 and on bugs, since state resumes either way.
    url = a.url.rstrip("/")
    try:
        settings = settings_mod.load(a.settings)
        with store.connect(a.dsn) as conn:
            run_controller(SimClient(url), conn, url, a.step, settings)
    except (FatalConfig, store.SchemaMismatch, settings_mod.SettingsError) as e:
        log.error("fatal: %s", e)
        return 2
    except (httpx.TransportError, psycopg.OperationalError) as e:
        log.error("lost connection: %s; exiting so run.sh restarts us", e)
        return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
