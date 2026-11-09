"""Crash-recovery test (S25): SIGKILL the controller at random moments, restart, verify the result.

    .venv/bin/python scripts/crash_test.py --scenario scenarios/public_02_incidents.yaml --kills 5

Starts its own simulator on --port and uses its own database (formi_crash, recreated each time), so it
never touches a sim or controller state you have running. Afterwards it checks
the controller's database against what the simulator says it produced:
  - exactly one run was created (every restart resumed, none started over)
  - events saved == sim's last_event_id, ids 1..N with no gaps and no duplicates
  - scoreboard totals == raw call events
  - one daily summary per workflow per day, one diary row per config change
  - changes in flight (X4): every config change we caused was applied exactly once, matches a
    request we recorded as applied (and vice versa), and nothing is left half-sent or stuck
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def wait_healthy(url: str, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise SystemExit("simulator never became healthy")


KEEP_ALL = ROOT / "runs" / "crash_test_settings.yaml"  # raw events kept, so exactly-once can be checked


def controller(url: str, dsn: str, log) -> subprocess.Popen:
    return subprocess.Popen([PY, "-m", "controller.main", "--url", url, "--settings", str(KEEP_ALL)], cwd=ROOT,
                            env={**os.environ, "DATABASE_URL": dsn}, stdout=log, stderr=subprocess.STDOUT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--kills", type=int, default=5)
    ap.add_argument("--port", type=int, default=8711)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--run-dir", default="runs/crash_test")
    ap.add_argument("--log", default="reports/crash_test.log")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    KEEP_ALL.parent.mkdir(exist_ok=True)
    KEEP_ALL.write_text((ROOT / "controller" / "settings.yaml").read_text().replace(
        "raw_event_days: 3", "raw_event_days: 3650"))
    url = f"http://127.0.0.1:{a.port}"
    base = f"postgresql://formi:formi@127.0.0.1:{os.environ.get('FORMI_PG_PORT', '55432')}"
    with psycopg.connect(f"{base}/formi", autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS formi_crash WITH (FORCE)")
        c.execute("CREATE DATABASE formi_crash")
    dsn = f"{base}/formi_crash"
    (ROOT / a.log).parent.mkdir(parents=True, exist_ok=True)
    log = open(ROOT / a.log, "w")

    def say(msg: str) -> None:
        line = f"[crash_test {time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    sim = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", a.run_dir,
                            "--port", str(a.port), "--force"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_healthy(url)
        end = httpx.get(f"{url}/sim/state").json()["end_minute"]
        # Kill 1 lands mid-history (one big chunk); the rest at random points of sim time.
        targets = [None] + sorted(rng.randrange(15, end, 15) for _ in range(a.kills - 1))
        say(f"sim up: {a.scenario} on {url}; kill targets (sim minute): {targets[1:]}")
        for i, target in enumerate(targets, 1):
            p = controller(url, dsn, log)
            if target is None:
                time.sleep(2.0)
            else:
                while p.poll() is None and httpx.get(f"{url}/sim/state").json()["sim_minute"] < target:
                    time.sleep(0.02)
            if p.poll() is not None:
                say(f"FAIL: controller exited ({p.returncode}) before kill {i}")
                return 1
            p.send_signal(signal.SIGKILL)
            p.wait()
            st = httpx.get(f"{url}/sim/state").json()
            say(f"kill {i}: SIGKILL at sim minute {st['sim_minute']} ({st['ts']})")
        say("final restart, running to completion")
        p = controller(url, dsn, log)
        if p.wait() != 0:
            say(f"FAIL: final controller exited {p.returncode}")
            return 1
        st = httpx.get(f"{url}/sim/state").json()
        workflows = [w["workflow_id"] for w in httpx.get(f"{url}/workflows").json()]
        ours_in_sim = [h["proposal_id"] for wid in workflows
                       for h in httpx.get(f"{url}/workflows/{wid}/config/history").json()
                       if h.get("proposal_id")]
    finally:
        sim.terminate()
        sim.wait()

    with psycopg.connect(dsn) as c:
        q = lambda sql, *p: c.execute(sql, p).fetchone()  # noqa: E731
        run_id, last_chunk = q("SELECT run_id, last_chunk FROM runs WHERE scenario=%s ORDER BY run_id DESC LIMIT 1",
                               st["scenario"])
        n, lo, hi, distinct = q("SELECT count(*), min(event_id), max(event_id), count(DISTINCT event_id) "
                                "FROM events WHERE run_id=%s", run_id)
        raw_calls, raw_conn = q("SELECT count(*), count(*) FILTER (WHERE (body->>'connected')::bool) "
                                "FROM events WHERE run_id=%s AND type='call'", run_id)
        sb_dials, sb_conn = q("SELECT sum(dials), sum(connected) FROM call_stats WHERE run_id=%s", run_id)
        summaries, = q("SELECT count(*) FROM decisions WHERE run_id=%s AND kind='daily_summary'", run_id)
        cfg_events, = q("SELECT count(*) FROM events WHERE run_id=%s AND type='config_changed'", run_id)
        cfg_diary, = q("SELECT count(*) FROM decisions WHERE run_id=%s AND kind='config_observed'", run_id)
        runs_created = q("SELECT count(*) FROM runs")[0]
        applied = [r[0] for r in c.execute("SELECT proposal_id FROM proposals WHERE run_id=%s AND status='applied'",
                                           (run_id,)).fetchall()]
        half_done, = q("SELECT count(*) FROM proposals WHERE run_id=%s AND status IN ('sending','applying')", run_id)
        stuck, = q("SELECT count(*) FROM changes c WHERE run_id=%s AND status='in_flight' AND NOT EXISTS "
                   "(SELECT 1 FROM proposals p WHERE p.run_id=c.run_id AND p.proposal_id=c.proposal_id "
                   "AND p.status IN ('pending','approved'))", run_id)
        refused, = q("SELECT count(*) FROM proposals WHERE run_id=%s AND status='refused'", run_id)
    days = st["sim_minute"] // 1440

    checks = [
        ("every restart resumed (one run created)", runs_created == 1,
         f"{runs_created} run(s) created across {a.kills + 1} starts"),
        ("all chunks ingested", last_chunk == st["chunks"] - 1, f"bookmark {last_chunk}, sim chunks {st['chunks']}"),
        ("events saved == sim produced", n == st["last_event_id"], f"{n} vs {st['last_event_id']}"),
        ("no gaps", (lo, hi) == (1, st["last_event_id"]), f"ids {lo}..{hi}"),
        ("no duplicates", distinct == n, f"{distinct} distinct of {n}"),
        ("scoreboard dials == raw calls", sb_dials == raw_calls, f"{sb_dials} vs {raw_calls}"),
        ("scoreboard connected == raw", sb_conn == raw_conn, f"{sb_conn} vs {raw_conn}"),
        ("one summary per workflow per day", summaries == days * len(workflows),
         f"{summaries} vs {days} days x {len(workflows)} workflows"),
        ("one diary row per config change", cfg_diary == cfg_events, f"{cfg_diary} vs {cfg_events}"),
        ("our changes reached the sim (something was in flight)", len(ours_in_sim) > 0,
         f"{len(ours_in_sim)} config change(s) by the controller"),
        ("no change applied twice", len(ours_in_sim) == len(set(ours_in_sim)), f"{ours_in_sim}"),
        ("sim's applied changes == ours recorded applied", sorted(ours_in_sim) == sorted(applied),
         f"sim {len(ours_in_sim)}, ours {len(applied)}"),
        ("nothing left half-sent or half-applied", half_done == 0, f"{half_done} open"),
        ("no change stuck in flight", stuck == 0, f"{stuck} stuck"),
        ("no request refused by the sim", refused == 0, f"{refused} refused"),
    ]
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        say(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
    say("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
