# Provenance Notes: how this assignment got done

A running journal of every decision, what we tried, what we learned, and why.
Newest entries at the bottom. Each entry: what we did, what we decided, why.

---

## Decision log (quick index)

| # | Date | Decision | Why |
|---|---|---|---|
| D1 | 2026-11-07 | Work in a fresh local git repo, not a fork | README says don't share publicly; fresh history lets commits show our own thinking |
| D2 | 2026-11-07 | Python 3.11 + venv | Simulator and grader are Python; same language means one toolchain to defend |
| D3 | 2026-11-07 | Treat everything below `# ---- hidden ground truth` as unseen | Reading it is disqualifying, and tuning to public truth fails on hidden scenarios |
| D4 | 2026-11-07 | Build order: reliable skeleton first, smart parts after | The mandatory floor (runs end to end, audit trail, crash safety) matters more than lift |
| D5 | 2026-11-07 | ~~SQLite~~ **Postgres** (revised, see journal "Storage choice") | SRE already runs Postgres; survives node recycling; many writers when we partition at 10x |
| D6 | 2026-11-07 | Keep running aggregates for decisions; keep raw events for a bounded window | 2 crore calls/month is about 20 GB/month raw; aggregates are what decisions need, raw is for audits |

---

## Journal

### 2026-11-07: Understanding the problem

**What we did**
- Read the full repo: README, `docs/SIMULATOR.md`, the 6 stakeholder memos, the 3 scenarios
  (policy sections only), the naive example controller.

**The problem in one sentence**
Build a program that safely and automatically tunes outbound voice agents (greeting, voice,
caller ID, script, call hour), always goes through the client's approval process, records
every decision, and shows judgement about what to build and whom to say no to.

**Key facts we pulled out**
- Two KPIs, never blended: pickup rate (connected / dials) and engagement rate
  (engaged / connected). Each workflow has one `primary` and a `guardrail` on the other.
- Config changes only three ways: supervisor edit (bypasses us), approval path
  (propose, human decides, we apply), auto-apply (only allowed dims, within `max_shift`).
- 15 capabilities listed, about 2 to 3x what fits. Choosing is graded.
- Floor: public_01 and public_02 run with no manual steps, audit trail, no constraint broken,
  complete `SUBMISSION.md`.

**Memo claims that are wrong against the actual policy** (checked in the scenario files)
- Sales: "Kestrel turned on auto-apply for everything." False. `ks_cancel` allows only
  schedule, caller_id, welcome_message, voice, with `max_shift: 0.2`. Prompts need approval.
- Sales: "Northwind is fine with auto-apply on KYC." False. `nw_kyc` has auto-apply disabled.
- Sales: wants one blended score. Breaks the hard rule and Product's definitions.
- Sales: wants the "persuasive" prompt (P4) everywhere. Its own summary says "freer";
  hallucination risk, and `nw_kyc` doesn't even offer P4.
- Kestrel supervisor: "max 2 requests/day, never before 11:00." Policy allows 4/day from 10:00.
  Not wrong, just stricter. A preference we can choose to honour.
- Kestrel supervisor: "tell me before anything changes on a running campaign." Conflicts with
  auto-apply on voice/welcome while `mid_flight: allowed`.
- Kestrel supervisor's "don't make it read the script word for word" maps to P5 ("Strict").
- Compliance (Northwind): approver hours 10 to 19 (KYC 10 to 18) match the policy.

### 2026-11-07: Setup and first run

**What we did**
- Copied the repo into `formi-assignment/`, fresh `git init`, initial commit of starter code.
- Created `.venv`, installed `requirements.txt`.
- Ran the baseline (no-op) on public_01: 8 sim days in about 4 seconds, 297k events.
- Ran the provided naive controller on public_01 and graded it: `reports/naive_01.md`.

**Naive controller results (public_01)**

| Workflow | Primary KPI | Result vs baseline |
|---|---|---|
| nw_kyc | pickup | +2.3% pickup (lucky voice change) |
| nw_renewal | engagement | +0.9%, basically nothing |
| ks_cancel | engagement | +1.0% engagement, but pickup -2.3% |

6 changes applied: 2 beneficial, 4 neutral.

**Biggest lesson: 7 of 15 approval requests expired**
The naive controller proposes at 20:00. Approvers work 10:00 to 19:00. On `ks_cancel` the
TTL is 12 hours, so the request dies at 08:00 before anyone is at work.
Takeaway for our design: **time proposals against approver hours and TTL**, and batch them.

**What the grader measures** (from the report layout)
Per-workflow KPI lift, whether each applied change was harmful/neutral/beneficial at the
moment it was applied, governance stats (requests, expiries, rejections, conflicts, lapses),
incident time-to-fix, how fast we move off decaying variants, credits used.

**Next**
Build the skeleton: advance time, ingest events into the store, log every decision,
survive kill and restart.

### 2026-11-07: Storage choice, SQLite vs Postgres

**Question raised:** with 2 crore calls/month, shouldn't we use Postgres instead of SQLite?

**The maths (volume is NOT the deciding factor)**
- 2 crore calls/month is about 6.5 lakh calls/day, about 8.5 lakh events/day.
- That averages about 10 events/sec, peaks about 14/sec.
- SRE's target (one load day in 10 wall-clock minutes) needs about 1,400 events/sec.
- SQLite with batched transactions does tens of thousands/sec on one box. So volume alone
  does not rule it out. Worth knowing if the reviewer claims otherwise.

**Why Postgres anyway (decision D5 revised)**
1. SRE memo: "We already run Postgres and a Redis. I'd prefer no new stateful infrastructure."
   Postgres is already operated, backed up and paged on. SQLite would be new, unowned state.
2. SRE memo: "nodes get recycled". SQLite lives on the node's local disk; a recycled node can
   lose learned state and pending approvals. Postgres is separate, so a controller can die and
   resume on any node. This is the crash-safety requirement.
3. 10x answer: partition work across several controller workers (by tenant/workflow).
   SQLite allows one writer at a time; Postgres handles concurrent writers.

**Cost accepted**
- `run.sh` must start Postgres first (Docker, which the README allows). One extra Python
  dependency (Postgres driver), easy to defend since SRE runs Postgres.

**Related decision D6: don't keep every raw event forever**
- Raw calls at about 1 KB each is about 20 GB/month.
- Decisions run on running aggregates (per workflow x cohort x variant x hour: dials,
  connected, engaged, hallucination flags).
- Raw events are kept for a bounded window to answer audit questions
  ("why did this customer hear this on day 3", Compliance needs it within 24h).

### 2026-11-08: Skeleton design review (in progress)

Drafted 27 skeleton decisions (S1 to S27) and reviewed them one by one with Kiran.

**Decided**
- **S16, running totals ("scoreboard") built now, inside the skeleton.** Every saved call also
  bumps a counter row (workflow x day x hour x cohort x variant: calls, answered, stayed,
  hallucination flags), in the same database transaction as the raw event.
  Why: saving and counting belong together, so they get built and tested as one piece; the
  smart features later read counters instead of scanning millions of raw rows; one transaction
  means the counters can never disagree with the raw events.

- **S19, diary volume: option B.** One summary row per workflow per day ("D2 nw_renewal:
  2,100 calls, pickup 26%, engagement 55%, nothing needed changing") plus a row for every real
  decision or observed config change. Rejected: a row every 15-min check (about 96 identical
  rows/day bury the one that matters) and real decisions only (a quiet day looks identical to
  a crashed controller).

**Final skeleton decisions (approved 2026-11-08)**

| # | Decision | Why |
|---|---|---|
| S1 | Python 3.11 | Same language as sim/grader |
| S2 | Postgres 16 in Docker, started by run.sh | D5; same on grader's Linux box |
| S3 | psycopg 3, no ORM | Standard driver; fast COPY; less to defend |
| S4 | Plain idempotent SQL schema at startup | No migration tool needed |
| S5 | Retry transient HTTP errors with backoff | Network/sim blips shouldn't crash us |
| S6 | Never blindly retry advance: check sim clock first, only advance the remainder | Double advance skips time we never looked at |
| S7 | 15-minute step | React as fast as possible; revisit for load test |
| S8 | Skeleton only reads; never proposes/applies config | Can't break governance before that logic exists |
| S9 | Events keyed by (run, event_id); re-insert ignored | Re-download after crash can't duplicate |
| S10 | Chunk events + bookmark in one transaction | Crash never leaves half-saved state |
| S11 | Always list chunks after bookmark; ignore advance's new_chunks list | One code path for normal running and recovery |
| S12 | Loop order: ingest, think, advance; restart begins with ingest | Never advance with unread events |
| S13 | History uses same ingest path | No special case |
| S14 | Stream chunk lines, bulk COPY into Postgres | Bounded memory, fast at load scale |
| S15 | Raw event stored as JSONB + key columns | Full audit detail, fast common lookups |
| S16 | Scoreboard updated in same transaction, counting only newly inserted events | Fast decisions; counts can't drift from raw |
| S17 | Diary append-only (DB trigger blocks UPDATE/DELETE) | Auditors can trust history |
| S18 | Diary rows carry decision, reason, input numbers | Evidence, not just labels |
| S19 | Option B: daily summary per workflow + every real decision | See above |
| S20 | Observed config changes (incl. supervisor edits) written to diary | Never overwrite silently; audit trail |
| S21 | proposals table exists but unused until governance step | Don't claim untested behaviour |
| S22 | Detect restarted sim (clock/chunks behind our bookmark) and start a new run | Never mix two worlds' data |
| S23 | All rows tagged with run_id | Many runs in one database |
| S24 | Tests against real Postgres | Mocks hide the bugs we care about |
| S25 | Crash test: SIGKILL 5 random times on public_02, verify no gaps/dupes | SRE wants proof; deliverable #4 |
| S26 | Skeleton grade must equal baseline (+0.0%) | It changes nothing; any diff is a bug |
| S27 | One JSON log line per loop step | Throughput numbers + loop health |

### 2026-11-08: Skeleton built

**What we built** (all in `controller/`)
- `client.py`: talks to the sim over HTTP. GETs retry with backoff. `advance_to(target)` re-reads
  the sim clock before every attempt and only advances the remainder, so a timeout can never
  make us skip time (S6).
- `schema.sql`: tables `runs` (incl. bookmark), `events`, `call_stats` (scoreboard),
  `decisions` (append-only diary, DB trigger blocks UPDATE/DELETE), `proposals` (empty for now).
- `store.py`: `ingest_chunk` streams a chunk into a temp `stage` table via COPY, then ONE
  transaction inserts raw events, bumps the scoreboard (counting only rows actually inserted),
  writes a diary row per observed `config_changed`, and moves the bookmark.
- `think.py`: daily summary per workflow (option B). No change logic yet (S8).
- `main.py`: loop = state, ingest, mark covered, think, advance 15 min. Stops when the sim says
  finished and every event is saved.
- `run.sh`: starts Postgres (docker compose), runs the controller, restarts it if it dies.
- `tests/test_controller_store.py`: 11 tests against real Postgres.
- `scripts/crash_test.py`: SIGKILL test.

**Decisions made while building**
- **D7: Postgres host port 55432**, configurable via `FORMI_PG_PORT`. 5433 was already taken on
  this Mac; a high, unusual port avoids clashes on the grader's box too.
- **D8: diary time label is computed by the database** (`sim_ts` generated column, "D3 14:00"),
  from one integer `sim_minute`. One format, one place (DRY); no mixing ISO timestamps and
  day labels.
- **D9: "covered up to" marker (`mark_covered`).** At night there are no calls, so no chunks
  arrive, and the bookmark's clock would lag. Without this the final day's summary would never
  be written. Safe because the sim flushes a chunk at the end of every advance, so after reading
  state at minute M and ingesting every listed chunk, nothing older than M is outstanding.
- **D10: run.sh restarts the controller on failure** (up to 20 times). Crash recovery is then
  part of normal operation, not only a test.

**Bug found by measuring (worth telling in the review)**
- Symptom: per-step save time grew from 0.01s to 0.8s over a run; scenario 01 took 2m13s.
- Investigation: EXPLAIN ANALYZE showed the ingest SQL takes 4 ms, so not Postgres. Profiling the
  loop without the daily-summary step showed a flat 1.1s/day. So the summary step was the trigger.
- Root cause: psycopg's default (non-autocommit) mode. The summary step's plain SELECT opened an
  implicit transaction that never closed. Every later "transaction" block silently became a
  savepoint inside it, so **nothing was committed until exit**, and the temp stage table
  (cleared on commit) kept growing, so each step reprocessed everything since D1.
- Why the crash test still passed: a kill rolled back to the last real commit and the restart
  redid the work correctly. Correct, but crash safety was much coarser than designed.
- Fix: autocommit connection; every `with conn.transaction()` block is now a real commit.
  Regression tests: a second connection must see each chunk right after it is saved, and the
  stage table must be empty after each chunk. Confirmed the test FAILS without the fix.
- Result: scenario 01 went from 133s to 15s; scenario 02 from 201s to 17s.

**Results**
| Check | Result |
|---|---|
| public_01 via run.sh | finished, 15s wall clock, +0.0% vs baseline on every KPI (S26) |
| public_02 via run.sh | finished, 17s, +0.0%; both supervisor edits recorded in the diary (D3 11:30 ks_cancel prompt; D5 15:00 nw_renewal voice) |
| Events saved vs produced | 297,003 = 297,003 (01); 324,282 = 324,282 (02), no gaps, no duplicates |
| Scoreboard vs raw | exact match (dials and connected) |
| Tests | 17 pass (11 ours + 6 sim's) |
| Crash test (public_02) | 5 SIGKILLs (1 mid-history, 4 at random sim times), 1 run across 6 starts, every check PASS (`reports/crash_test.log`) |

**Crash test design notes**
- First version killed on random wall-clock timers. After the speed-up all kills landed during
  history loading (first 5 seconds), so it no longer covered the main loop. Changed to: kill 1
  during history, kills 2 to 5 at random *sim minutes* (polls the sim clock).
- Changed the "resumed" check from counting log lines (a kill before Python finished starting
  logs nothing) to "exactly one run row was created", which is the real property.

**Next**
- Independent code review of the skeleton (in progress).
- Then: first smart capability. Candidates in priority order: governance client (C9),
  caller-ID health (C5), hallucination incident response (C8), experiments (C6/C7).

### 2026-11-08: Independent code review of the skeleton, and fixes

Reviewed by a separate reviewer agent (not the author pass), which also re-ran tests and the
crash test with a different seed (PASS). Verdict: **no critical or high issues**; crash windows,
advance retry, mark_covered ordering, SQL and run.sh all confirmed correct for one sim per DB.

**Fixed**
| Finding | Severity | Fix |
|---|---|---|
| Run identified only by scenario + counters: two sims of the same scenario in one DB could resume each other's run (would lose/mix pending approvals later) | Medium | `runs.sim_url`; `resolve_run` matches URL + scenario. Crash test uses its own DB `formi_crash`. |
| `CREATE IF NOT EXISTS` never alters old tables: a schema change fails mid-run | Medium | `schema_meta` + `SCHEMA_VERSION`; older DB refused at startup with "reset with `docker compose down -v`", exit 2 |
| One malformed event (null actor, missing variant) blocks ingest forever | Low | NULL-safe `format()`, `coalesce(.., '?')`; new test also caught `changed_dims: null` crashing `jsonb_array_elements_text`, now guarded |
| Tick length assumed (15) | Low | read `tick_minutes` from `/sim/state`; `--step` must be a multiple, else exit 2 |
| Two controllers could drive one sim | Low | session advisory lock per sim URL, released automatically when the process dies |
| Exit codes muddled; DB connect outside error handling | Low | 0 done, 2 fatal config (run.sh does not restart), 75 lost connection, other = bug (restart) |
| run.sh restart limit counted over whole run; SIGTERM not forwarded | Low | counter resets after a healthy 60s run; TERM/INT forwarded to the controller (verified: child gone, exit 143) |
| Possible busy loop if sim finished but we're behind | Low | raise a clear error instead |
| Bookmark clock could move backwards (history chunk) | Low | `GREATEST(sim_minute, ..)` |

**Decision D11: exit-code contract between controller and run.sh** (above). Why: a restart fixes
crashes and lost connections (state resumes), but can't fix a wrong flag or schema, so those
stop immediately instead of looping 20 times.

**Deferred, on purpose**
- Fault-injection hooks to kill at the narrowest windows (after COPY, before mark_covered).
  Current crash test kills at random sim minutes; the atomic-transaction design covers the
  narrow windows, and a store test covers death mid-stream.
- Batching daily-summary writes (negligible cost today).
- Decision tags (S5, S10...) in code comments point to this journal, which is in the repo; kept.
- Known gap: if the sim is restarted AND advanced past our old bookmark by someone else before
  our controller restarts, it looks like the same world. Needs a world id from the API, which
  it does not provide.

**Verified after fixes**: 22 tests pass; public_01 and public_02 via run.sh finish at +0.0%;
crash test PASS (5 kills, 1 run, all counts exact); second controller refused (exit 2);
bad `--step` stops run.sh without restarts (exit 2); SIGTERM to run.sh stops the controller.

### 2026-11-08: Approval handling (governance client, C9): design review

**Facts confirmed by reading the sim's governance API code** (rules engine only, no hidden truth)
- The daily request limit counts every request created in the last 24h, including rejected
  and expired ones. A wasted request still burns a slot.
- There is no cancel. A sent request can only end approved, rejected or expired.
- "Lapsed" is only discovered when you try to apply too late (410). No event warns you.
- Refused auto-applies (403) and stale applies (409) are both counted by the grader.
- `GET /approvals?workflow_id=` returns `proposal_id` alongside `approval_id`, so we can always
  reconcile by our own id.

**Final decisions (approved 2026-11-08)**

| # | Decision | Why |
|---|---|---|
| G1 | One common change format for every feature: workflow, what to change, urgent/normal, reason, numbers | Every feature hands changes to one gate (DRY) |
| G2 | The request manager (not the feature) decides auto-apply vs approval, from the workflow's own policy | One place knows the client's rules |
| G3 | Check auto-apply rules locally (allowed dims, max_shift as total-variation distance) before trying | Refused attempts are counted against us |
| G4 | Never send a note that is certain to be rejected: required fields, valid numbers, prompt changes carry a passing validation | Rejections still burn a daily slot |
| G5 | Normal changes bundled into one note per workflow at the start of that manager's day (`approver_hours`) | Kiran's idea: a full day to answer, a full day of data, one note a day |
| G6 | Urgent changes may go out immediately, subject to G7 | Harm happens every hour we wait |
| G7 | **Kiran's timing rule, urgent changes only:** expected answer = now + that manager's average answer time. Send now only if the expected answer lands at least 30 min before the manager's day ends. Otherwise send at the very start of their next day, tagged URGENT. Average = midpoint of the policy's latency range until we have real answers from that manager, then their measured average (same-day answers only). Normal changes never use this rule and are never tagged urgent. | Uses each manager's own numbers; the 30 min buffer is ours; learns real manager speed |
| G8 | Keep at least one daily slot free for urgent changes (if the policy allows 2 or more) | Routine notes must never block an emergency |
| G9 | **No per-client settings file.** Follow the policy from the API only. Kestrel's "max 2/day" is already met by one morning note plus rare emergencies; for "not before 11:00" our reply points her to her signed policy. | A file in our repo needs an engineer for every client change and rots at scale; the policy is the contract (CTO memo) |
| G10 | Save the request in our tracker before sending, with our own id `{run token}-{workflow}-{n}` (token: see D19) | A crash mid-send can't lose it |
| G11 | After a crash, resend with the same id; "duplicate id" means it arrived, so look it up by id | Never two copies of one request |
| G12 | Mark "applying" before applying; after a crash, ask the sim whether it went through first | Never apply twice |
| G13 | At startup, reconcile our tracker with the sim's approvals list by proposal id | Nothing lost after a crash |
| G14 | Manager replies are read from chunks and recorded in the same transaction as the events | A reply can't be seen but not recorded |
| G15 | Approved: apply at the very next step | No lapses |
| G16 | Before applying, check the config version is unchanged; if not, don't apply, re-queue fresh | Stale applies are refused and counted as conflicts |
| G17 | Rejected: record the reason, don't resend the same thing; at most one retry after 24h if fresh numbers still support it | Don't spam the manager |
| G18 | Expired: treat as rejection, re-queue so the feature rebuilds it with fresh numbers | Product memo |
| G19 | A diary line for every step: queued, sent, approved, rejected, expired, applied, stale, superseded | Answers "why did X change, who approved it, what happened next" |
| G20 | A supervisor edit drops our queued/in-flight change to the same setting | Never silently overwrite a supervisor (hard rule) |
| G21 | After a supervisor edit, leave that setting alone until we have enough calls on it to judge it, and never before the next morning note. Urgent harm is the exception. | Respect their choice, judge it on data, but still fix real harm |
| G22 | Tests run against a real, fresh simulator in a throwaway folder | Tests the real rules, not our guess of them |
| G23 | Crash test extended to requests in flight and mid-apply | Proves G10 to G13 |

**Changed during review**
- G7 originally used the *slowest* answer time. Then I proposed "50% chance". Kiran's version
  (average + a 30-minute buffer, else next morning tagged urgent, urgent only) is simpler to
  explain to a supervisor and to defend; adopted.
- G9 originally proposed a per-client preferences file. Kiran asked who maintains it: nobody
  good. Dropped in favour of the policy as the single source of truth.
- G21 originally 24h fixed. Replaced by "enough calls to judge it, not before next morning".

**Scope note:** no detection rules exist yet, so in real scenario runs nothing is sent and the
grade stays +0.0%. The tests exercise the request manager with hand-made changes. The first
detection rule (caller-ID health) comes next.

### 2026-11-08: Governance client built

**What we built**
- `controller/config_ops.py`: pure functions written from the docs: merge a patch into a config,
  which settings changed, the biggest total-variation shift, why the sim would refuse an
  auto-apply, unknown options. Lets us check *before* calling the sim (G3).
- `controller/governance.py`: `Change` (G1), `Governor.submit` (queue/refresh per intent),
  `Governor.tick` (holds, apply approved, close decided, send due), `Governor.reconcile` (G13).
- Schema v3: `changes` (one active row per intent, via a partial unique index), `proposals`
  (current status of every request; the diary keeps history), `holds` (supervisor edits).
- Ingest now also records the approver's decision on our tracker plus a diary line, and places
  holds for supervisor edits, in the same transaction as the events (G14).

**Design details decided while building**
- **D12: "intent key" per change** (e.g. `caller_id_health:ks_cancel:D2`). A feature that keeps
  detecting the same thing every 15 minutes refreshes one queued row with fresher numbers
  instead of piling up duplicates. When a request expires, the intent is free again, so the
  feature's next detection automatically builds a fresh request with today's numbers (G18).
- **D13: one approval request in flight per workflow**, except an urgent one may go out while
  a normal one is pending. Two requests on the same base config would make the second stale.
- **D14: non-urgent auto-applies wait while an approval is in flight** for that workflow,
  because applying anything bumps the config version and makes the pending request stale.
  Urgent auto-applies (e.g. a dead phone number) go anyway.
- **D15: a supervisor edit drops ALL our changes to that setting**, urgent ones too. If ours was
  still needed, the detecting feature re-submits it with fresh data after the edit.
- **D16: morning note window = first hour of the approver's day.** Wide enough to survive a
  crash at 10:00, narrow enough that a change found at 15:00 waits for tomorrow's note.
- **D17: manager's real average (G7) uses same-day answers only**, since an overnight answer
  measures the approver's office hours, not their speed. Needs 3 samples before trusted.
- Prompt changes must carry `validation_run_id` and `validation_pass_rate` in the change's
  evidence; the sim has no endpoint to look a validation run up later.

**Tests (11 new, against a fresh real sim per test)**
- Timing rule with Kestrel and KYC numbers; slot reservation; config ops.
- Auto-apply within policy is applied by the sim as `controller_auto`.
- ks_cancel voice change with shift 0.5 > max_shift 0.2 goes to the approver (no 403).
- Normal change found at 01:00 is not sent at 09:45, is sent at 10:00, then applied (or the
  rejection is recorded, since the approver may say no).
- Urgent: nw_kyc at 11:00 sent immediately (expected answer 15:30); nw_renewal at 17:00 held
  (19:15 too late) and sent next day 10:00 tagged URGENT.
- Prompt change without validation is never sent.
- Crash before POST and after POST (response lost): restart reconciles, exactly one approval each.
- Crash right after apply: restart sees it applied; config history shows exactly one apply.
- public_02 supervisor voice edit at D5 15:00: our pending request becomes `superseded`, the
  supervisor's config stays, the setting is held, a new non-urgent change is refused as `held`,
  the hold releases after enough calls, and the superseded request is never applied.

**Test-writing lessons (my mistakes, fixed)**
- Test helper applied an approval in the same tick it arrived, so a "crash before apply" test
  never saw the approved state. Added `stop_before_tick`.
- A test assumed the approver would approve; it rejected (base_reject_rate). Rewrote so the
  scenario is deterministic: send at 14:45 so it is still pending at the 15:00 edit.

**Results**: 33 tests pass; public_01/02 via run.sh still +0.0% (nothing submits changes yet,
as expected); crash test PASS.

**Honest gap (G23):** the SIGKILL crash script can't yet exercise requests in flight, because
no feature submits changes in the scenario runs. Covered for now by the governance crash tests
above (injected kill before/after send and after apply). Extend the SIGKILL script once the
first detection rule exists.

**Next**: independent review of this step (running), then the first detection rule:
caller-ID health (C5).

### 2026-11-08: Governance review (round 1) and fixes

Independent reviewer verdict: **request changes**. 0 critical, 2 high, 6 medium, 6 low.
Confirmed correct: never applies twice, never applies stale, no lost pending approval on traced
paths, daily-limit counting matches the sim, SQL correct.

| Finding | Sev | What was wrong | Fix |
|---|---|---|---|
| H1 | High | Crash right after the sim answered 410 (too late to apply): restart marked the request lapsed but left its change "in flight" forever, so that idea could never be proposed again | reconcile routes lapsed through the normal close (re-queue); any unexplained state stops the controller loudly |
| H2 | High | Auto-apply path skipped the form checks (e.g. empty rationale gets 403); a refused change was re-submitted and refused every tick, each counted by the grader | same checks on both paths; refusals count against the idea like rejections (cool-down, give up after 2) |
| M1 | Med | Diary written after the state commit: a crash in between loses a diary line forever | diary row now commits in the same transaction as the state change |
| M2 | Med | Urgent change could auto-apply over a supervisor's held setting with no human involved | never auto-apply on a held setting; goes to the approver and the summary says "overrides a recent supervisor setting" |
| M3 | Med | Mid-day urgent note carried that day's normal changes, tagged URGENT, using the emergency slot | mid-day urgent notes carry only urgent changes; `[URGENT]` tags items, not the whole note |
| M4 | Med | Our daily count included requests the sim never created (stale/refused at send) | count only requests the sim has (approval id) or may have (sending) |
| M5 | Med | SIGKILL crash script has no requests in flight | **deferred** to the first real feature (needs real changes flowing); pytest covers injected crashes |
| M6 | Med | Two prompt changes in one note: evidence of one, prompt of the other, so a certain rejection | one prompt change per request (newest), the rest wait |
| L1 | Low | applied version read from current config | looked up by our proposal id in config history (one code path) |
| L2 | Low | proposal ids could repeat after a DB reset against the same sim world | random per-run token in the id (schema v4) |
| L3 | Low | schedule hour keys compared un-normalised (int vs str) | normalised like the sim does |
| L4 | Low | a large `--step` could jump over the morning window | window is at least one step wide |
| L5 | Low | decisions found on resend lost their decided time and diary link; an unreachable reconcile loop | adopt sets decided time/reasons and writes a linking diary line; dead loop removed |
| L6 | Low | hold release counts all answered calls, not just the held setting's | **kept, documented** (exact for workflow-wide settings, generous for cohort ones) |
| Maint. | | statuses scattered; one 50-line function; repeated SQL; two copies of 1440 | status machine in the module docstring; `_auto_step` / `_approval_step`; `_set_changes` helper; one shared `MINUTES_PER_DAY` |

**Interpretation decided (D18):** "tagged urgent, only for the urgent ones" (Kiran). A morning note
that bundles urgent and normal items tags each urgent item `[URGENT] ...` in the summary; normal
items are never tagged. Mid-day notes carry only urgent items.

**Lesson:** H1 hid in a status nobody listed. Writing the status machine down as a table (now in
`governance.py`) is what makes holes like that visible; the reviewer said as much.

**Verified:** 38 tests pass (5 new, one per high/medium fix that is testable), public_01/02 via
run.sh still +0.0%, crash test PASS. Reviewer is re-checking the fixes.

### 2026-11-08: Governance review (round 2): my fixes introduced two problems

Reviewer re-checked every round-1 fix: all fixed. But two fixes introduced new problems:

- **R1 (high), my L2 fix:** I made proposal ids unique with a *random* token. The sim's approver
  draws approve/reject and answer time from the proposal id, so random ids made every run
  unrepeatable and made two tests fail about 1 in 3 runs. My earlier "38 passed" was luck.
  **D19:** the token is now derived from the sim state when the run is created
  (scenario, clock, event count). Same world gives the same ids every time; a database reset
  mid-world sees a different clock/event count, so ids still never repeat.
  Tests that needed an approval now keep proposing until one is approved (as the real
  controller would), instead of assuming the first one is. Verified 8 of 8 repeated runs.
- **R2 (medium), my L3 fix:** normalising schedule keys with `int()` meant a malformed schedule
  patch raised inside the loop: crash, restart, crash again on the same queued change.
  Now `schedule_problems` checks the shape (whole hours inside the dialling window, weights
  >= 0) and the change is dropped with a diary line; normalising leaves bad values alone.
- **R3 (low):** dropped-change diary lines keyed by idea + problems, so a feature resubmitting
  the same bad change writes one line, not one per tick.
- Doc drift fixed: G10 id format; status machine notes that a restart can also set
  rejected/expired; G8 now says a morning note with an urgent item may use the reserve slot.

**Lessons**
- A fix can break a property nobody wrote down. "The approver's decisions are reproducible" was
  implicit; it is now a test (`test_same_world_gives_same_proposal_ids`).
- One green run is not evidence for anything random. Repeat the tests that depend on chance.

**Verified:** 40 tests pass; approval-dependent tests 8/8 repeated runs; public_01/02 via run.sh
+0.0%; crash test PASS.

### 2026-11-08: Governance review (round 3): approved

Reviewer re-ran the previously flaky tests 7/7 and the full suite (40 pass): R1, R2, R3, L1 and
doc drift all fixed. **Verdict: approve.** Two tiny leftovers fixed straight away: schedule hours
must be an int or a digit string ("9", not "9.0"), as the sim accepts. Still deferred, agreed:
M5 (SIGKILL crash test with requests in flight, waits for the first real feature) and L6.

### 2026-11-08: Caller-ID health (C5), the first feature that changes config

**Kiran's answers that shaped it**
- F1 shown as a picture: a feature is a machine with two inputs (the scoreboard, the workflow's
  current **config** read live from the sim) and one output (a list of Changes) that goes to the
  gate. Only the middle differs between features.
- "False alarms": each check is a lottery ticket that wins 1 time in 30,000 at 4 luck units;
  ~3,500 checks a day means about one false alarm every 8 to 10 days, before the second lock
  ("30% worse") makes it far rarer. Scenario 01 (no broken numbers) must raise zero alarms.
- Thresholds: "have these as defaults, but also make them configurable so business can define
  these numbers." Probe share: "lets do 5%, but again keep it configurable." Spares: my
  suggestion (none unless removing the bad number leaves zero healthy numbers).
- Asked: does the scoreboard have time on call? No. Raw events have `duration_s` (in Postgres);
  the scoreboard only counts. Caller-ID health only needs "answered or not". A column can be
  added when a feature needs duration.
- Naming fixed: **config** = the client's workflow setup (input 2). **checker knobs** = our own
  thresholds in `settings.yaml`.

**Decisions**

| # | Decision | Why |
|---|---|---|
| F1 | Every feature is `check(ctx) -> list[Change]`; reads scoreboard + current config, never writes to the sim | One shape; adding a feature never touches the gate |
| F2 | `FEATURES` list in `controller/features/__init__.py`, run every loop step before the gate; a crashing feature is logged + diaried and skipped for that step (connection errors still exit 75) | One bug in one rule must not stop approvals being applied, nor crash-loop on restart |
| F3 | `Change.ttl_min`: a queued change not re-submitted for 60 min is dropped ("no longer wanted") | If the problem goes away, the change goes away by itself |
| T1 | `stats.z_score` (two-proportion z, "luck units") | One test for every rate comparison; checked against Kestrel day 1 (0.48) and day 2 (13.7) |
| T2 | `config_ops.step_toward(p, q, limit)` moves straight toward the target by at most `limit` TV | Works for any setting, any options, any client limit |
| T3 | The gate steps self-changes itself: biggest allowed step now, the next `step_gap_minutes` (60) later; diary "step k of N" / "final step N of N" | Option A, chosen earlier. Changes are never refused (403) for being too big |
| T4 | If we may change every setting a change touches ourselves, it stays on the self-change path (applied, stepped, waiting, or dropped), never the approver; otherwise the approver gets the full target | One clear rule for which path; replaced the old "too big a self-change goes to the manager" behaviour (its test was rewritten) |
| K1 | **Checker knobs in `controller/settings.yaml`**: defaults + optional per-workflow overrides, every knob declared with type and range in `settings.py`; any typo or silly value stops the controller at startup (exit 2) | "Business can define these numbers" without it silently making us reckless. Not G9's rejected per-client file: these are Formi's own detection knobs, owned by the platform team; client policy is still read live. Production: a Postgres table + admin screen |
| C-1 | **One change per workflow** (`caller_id_health:<wf>`) carrying the whole wanted caller-ID split, not one per number | Two changes to the same split would overwrite each other in the gate (allocation splits are replaced whole). Cost: rejections count per workflow, not per number |
| C-2 | Number life kept in `feature_state` (new table): flagged/removing → removed → probing → restoring → forgotten; every move is a diary line committed with the state | Survives restarts; the audit trail explains each move |
| C-3 | Decided from the config, not from change statuses: "removed" = it gets 0% in every cohort now | Robust to a change being merged, dropped as already done, or a supervisor doing it for us. A supervisor putting a removed number back is respected (we forget ours) |
| C-4 | A number on trial is judged only on calls since the trial went live | Old bad data would otherwise fail every trial |
| C-5 | Single-number workflow (C4/C5): its own past 14 days at the same hours; "shared drop" if the other workflows dropped at least half as much | Without it, a one-number workflow could never be fixed; with it, a holiday isn't blamed on the number |

**Schema v5**: `changes.not_before_minute`, `changes.steps_done`, `proposals.partial`, new
`feature_state`. Existing databases must be reset (the version check says so).

**Tests (12 new, 52 total, all pass)**
- `tests/test_caller_id.py` (fast, scripted scoreboard): z-score on Kestrel numbers, step_toward /
  step_config (incl. a cohort that inherits default), knobs reject typos/silly values and apply
  per-workflow overrides; dead number flagged with a measured note; luck-sized gap ignored; quiet
  workflow widens to 12 h; single number vs its past switches to a spare; shared drop ignored;
  full life (removed, verified, trial at 5%, passes, back to 50/50, forgotten); failed trial
  removes again and waits 48 h; no trial where the client must approve caller IDs.
- `tests/test_governance.py`: a 0.5 voice move on ks_cancel goes in 3 hourly steps (0.8, 0.6,
  0.5), never to the approver; a change nobody re-submits is dropped after its ttl.

**Small lesson:** 0.8 − 0.6 is 0.20000000000000007 in floating point, which is *over* a 0.2
limit. `step_toward` checks its own rounded output and shaves a millionth off if needed, so a step
is never refused for a rounding hair.

**First real run (scenario 02, via run.sh) and what it taught us**

| | Run 1 | Run 2 (after the fix below) |
|---|---|---|
| Kestrel conclusion rate vs baseline | +25.6% | +14.4% |
| Northwind KYC | +11.4% | +14.9% |
| All workflows | +10.3% | +9.0% |
| Changes graded beneficial / neutral / harmful | 17 / 2 / 2 | 7 / 0 / 2 |
| Scenario 01 (no incidents): alarms | 0 | |

- **X1 met:** Kestrel D2 flagged D2 12:00 (15.4% vs 25.0%, 38% worse, 4.1 luck units), switched
  off in 3 hourly steps (12:00, 13:00, 14:00), verified, all three steps graded beneficial.
- **X2 met:** scenario 01 raised zero alarms and made zero changes.
- **Bug found by the run: a trial "passed" on too little evidence.** Day 4: D2 at 10.9% vs 20.0%
  on 202 trial calls is 45% worse but only 3.2 luck units, so "not proven bad" was read as "good"
  and D2 was restored, then removed again 30 minutes later (both graded harmful). **Fix (C10):**
  passing needs evidence it is fine (under the 30% line). Worse but unproven: keep the trial
  going; still unclear after 4 x 200 calls counts as failed. Test added.
- **Second fix: the note shows the evidence that met our bar.** KYC was flagged at D0 17:00 but the
  morning note at 10:00 quoted that morning's weaker numbers. Now the summary quotes the
  flag-time evidence ("flagged D0 17:00") and the rationale adds the latest numbers alongside.
- The two harmful changes left are the 5% trials of the still-dead D2: the known, small price of
  C10 (didΔ −0.028 and −0.022).
- **Run-to-run spread is large** (+25.6% vs +14.4% on Kestrel). The sim's random draws follow our
  actions, so two runs diverge after the first different decision. Judge on several runs, not one.
- **Observation, not acted on:** single-number checks (C4) fired on numbers that had been carrying
  all the calls (KYC D1, then KYC D2; Kestrel D1 in run 1) and the grader called those switches
  beneficial. It looks like a number wears out when it carries everything. That argues for
  spreading calls over spares, the opposite of the "no spare by default" rule. Raised with Kiran
  as a decision; not changed, to avoid tuning to one public scenario.

**X4 (deferred M5) done:** the SIGKILL crash test now checks changes in flight: 6 kills
(mid-history and at random sim minutes), 9 real config changes, every one applied exactly once,
the sim's list equals ours, nothing half-sent, nothing stuck, nothing refused. PASS.

**Verified:** 53 tests pass; public_01 and public_02 end to end via run.sh; crash test PASS.

### 2026-11-08: Caller-ID review (round 1) and fixes

**Spare numbers decision (Kiran): keep the rule** (no spare unless nothing healthy is left). The
"a number wears out when it carries everything" pattern goes into SUBMISSION.md as a next step,
not into code, to avoid tuning to one public scenario. (Explained as shoes: one pair worn every
day wears out; rotating two lasts longer.)

Independent reviewer verdict: **request changes**. 0 critical, 3 high, 5 medium, 6 low.

| Finding | Sev | What was wrong | Fix |
|---|---|---|---|
| H1 | High | `step_toward` checked its rounded output before normalising; the sim normalises, so 1/3 splits came out a hair over the limit, stepping gave up and the urgent fix went to the approver | check after rounding **and** normalising; test on 1/3 splits at four limits |
| H2 | High | one intent per workflow: two "no"s to removing D2 would block every later caller-ID fix on that workflow forever, and the removal stayed stuck | rejections counted per **target** (`Change.attempt_key`, e.g. "remove D2"); a stuck removal is re-judged and cancelled once the number is clearly fine |
| H3 | High | a trial passed whenever the point estimate dipped under the 30% line; looked at every 15 min, dead numbers passed 15-55% of the time (reviewer's simulation) | pass needs `pass_z` (2.5) luck units **clear of the 30% line** (`stats.clearly_not_worse`); still unclear after 4 x 200 calls counts as failed |
| M1 | Med | comparison pools cohorts and hours (Simpson's paradox when a cohort owns its own number split) | **deferred, documented**: stratify by cohort x hour when cohorts get their own splits (no feature creates them yet) |
| M2 | Med | a supervisor removing a number during our trial/restore would see it re-added next day | any supervisor caller-ID edit during a trial/restore: we forget ours, diary line, test |
| M3 | Med | numbers being removed / on trial stayed in the "healthy" reference group | excluded |
| M4 | Med | stepping on `next_campaign_only` workflows can lock a half-step into a campaign | **documented**: only matters with auto-apply there; none of the public ones allow it |
| M5 | Med | missing tests | added: 3-number split, two rejections then a new target, supervisor during trial, crash after a partial step (restart re-queues it, every step applied once) |
| L1 | Low | urgent steps make a pending approval stale | **kept** (pre-existing rule D14 says urgent goes anyway) |
| L2 | Low | one workflow's error skipped the checker for all; one error line per day | per-workflow guard, keyed per hour |
| L3 | Low | a typo'd workflow id in settings silently ignored; a non-mapping section crashed | startup check against the sim's workflows (exit 2); type checks |
| L4/L5 | Low | step labels wrong after the target changed; a new urgent target still waited the step gap | new target resets step count; an urgent new target skips the wait |
| L6 | Low | distinct-lead count on raw events every tick | cached per number per sim hour |

**Rerun found one more of my bugs (not the reviewer's):** on day 6, Kestrel D1 was removed while the
dead D2 was on its 5% trial, and the checker moved D1's calls *onto D2* (5 harmful steps). A number
on trial now keeps only its trial share and never takes other numbers' calls; the rest goes to a
spare. Also the verify line ran at night with 0 calls; it now waits for 200 calls. Tests for both.

**Results after the fixes (scenario 02):** Kestrel +25.4%, ALL +8.4%; 18 changes: 14 beneficial,
2 neutral, 2 harmful (the two 5% trials). KYC −1.7%: its borderline single-number alarm (exactly
30%, 4.1 luck units) was cancelled 2.5 h later as clearly fine, so no switch this run. Scenario 01:
0 changes. Crash test PASS (18 changes in flight). 64 tests pass.

**Lessons**
- "Not proven bad" is not "good". Every time we re-look at a growing sample, luck gets another
  ticket; passing needs its own margin, not the absence of an alarm.
- Each rerun found something the tests didn't (D2 restored, D2 given D1's calls). Real end-to-end
  runs are part of testing, not just the score.
- My `pkill` pattern for the scenario sims also matched a test's sim and killed it mid-test.
  Infrastructure, not code; the test passes on rerun.

### 2026-11-08: Caller-ID review (round 2) and fixes

Reviewer re-checked every round-1 fix: **all fixed** (H3 re-simulated: a still-dead number at 11%
now passes 0.05% of trials, was 15%; a healthy one passes 98.5%). Verdict still **request
changes**, for regressions my fixes introduced:

| Finding | Sev | What was wrong | Fix |
|---|---|---|---|
| N1 | High | when the checker cancelled a removal, the gate's copy lived on: a queued one could still be sent, an approved one still applied, leaving a healthy number dark with nothing tracking it | **withdraw at once**: after each feature runs, the gate withdraws every intent it didn't return. Queued: dropped. Request out: marked; if approved, closed as `withdrawn` and **never applied** (the apply window lapses). Test through a real approval |
| N2 | Med | "new target?" compared exact weights, which drift by a millionth from rounding, so urgent steps skipped the hourly gap and step labels restarted | compared by meaning: the list of targets (`attempts`) |
| N3 | Med | the rejection key was the whole bundle, so "remove D2" (given up) came back inside "remove D2, remove D3" | `Change.attempts` lists targets; rejections count **per target**; the checker leaves blocked targets out (the number stays in use, still watched, one diary line) and the gate refuses any bundle containing one, as a safety net |
| N4 | Low | a trial number whose only peer is itself being removed/restored waits unjudged | **documented**: slow, not harmful |
| N5 | Low | a supervisor edit forgetting our trial leaves that number at 0 forever | kept (their split stands); the diary now says it won't be trialled again |
| N6 | Low | verification could be skipped if the trial started first | verify line always written before leaving 'removed' |

Correction: I wrote "64 tests" last round; it was 62. Now **65 tests** (pytest --collect-only).

**Results (scenario 02):** unchanged from round 1 (same decisions): Kestrel +25.4%, ALL +8.4%;
14 beneficial, 2 neutral, 2 harmful (the trials). Scenario 01: 0 changes. Crash test PASS.

**Lesson:** "drop it after 60 minutes if not refreshed" (F3 as first designed) is too slow once a
request can already be out. Wanting must be withdrawn the moment it ends, all the way to "don't
apply", or the audit trail says one thing and the config another.

### 2026-11-08: Caller-ID review (round 3): approved

Reviewer re-checked N1-N6: all fixed. Answered our questions: withdraw never fires after a feature
error (the intent is kept), a "split already matches" withdraw is correct, a withdrawn approval is
never applied and its approval just lapses, and withdraw doesn't interact with stepping or
reconcile. **Verdict: approve.** Leftovers, fixed straight away:
- **W1:** withdrawn while pending, then wanted again before the answer: the approval would have
  been thrown away (a lost slot and a day during an incident). Now un-withdrawn when the same
  targets are asked for again. Test through a real approval.
- **L-a:** a withdrawn change re-queued by another path kept its flag and dodged withdraw for up to
  60 min. Withdraw now always drops flagged queued rows.
- **L-b, kept and documented:** if the approver rejects a request we had already withdrawn, it still
  counts against that target. They did say no.

**Verified:** 66 tests pass. (Scenario/crash runs from round 2 still stand: these two fixes only
touch the withdraw-then-rewant path, which those runs never hit.)

### 2026-11-08: Script health (C8), bad-script incident response

**Why C8 next (Kiran chose it):** scenario 02's day-3 bad script (supervisor switched Kestrel to P2)
was graded "fixed by: nobody". Engagement is Kestrel's main KPI and P2 cost about 5 points of it.

**Explained to Kiran as:** the sim sells two services for credits (a prepaid wallet, 200 in
scenario 02). "Suggest fixes" = asking an AI consultant for script ideas (3 credits); it returned
P2 itself among its ideas on a throwaway sim, so its ideas are never trusted alone. "Validation
run" = a test drive on pretend conversations (1 credit / 100); the manager rejects any script
change without one >= 97% clean. A test drive says nothing about engagement.

**Decisions (Kiran's answers in bold)**

| # | Decision | Why |
|---|---|---|
| H1 | A newly switched script: its made-up rate since it went live vs the previous script's. An unchanged script: the last **day** vs its own previous 14 days (drift) | One script per workflow, so before/after is the only comparison. Drift on a whole day: see the scenario-01 finding below |
| H2 | Alarm: >= 100 answered calls, >= 2x the reference, >= 4 luck units (**Kiran: yes, defaults, configurable**) | Kestrel: 12/114 = 10.5% vs 1.5% at D3 12:45, 7.7 luck units, 75 min after the switch |
| H4 | **Kiran: immediately go back to the previous script proven clean (>= 97%) on real calls**, and **in parallel ask the AI and test-drive its ideas** | Real calls beat promises; and we still look for better options |
| H5 | **Kiran: number of AI ideas to test is a knob, default 2** (so the AI part costs 9 credits, 12 with the fix's own test drive). Ideas that are the bad script or the fix are skipped. The ideas are **reported** (diary + the manager's note), not switched to; only with no proven script does the cleanest well-passing idea become the fix | Option A (live trial) rejected after doing the numbers: a 1-day trial can catch a big drop (P5, -7 points) but can't prove a small gain (P3, +1.5 points needs ~20,000 calls). Script experiments belong to C6/C7 |
| H6 | Test drive 300 conversations; must clear 97% by 2 luck units | A borderline 97.5% on 300 could be luck |
| H7 | <= 15 credits per incident; optional spending (AI) never below 20% of the starting wallet; the fix's own test drive may use the reserve (**Kiran: yes**) | "Running out mid-incident is a failure"; the reserve exists for exactly the essential call |
| H8 | Urgent request; the gate adds "overrides a recent supervisor setting"; evidence carries the validation id, rates, engagement, other options checked | The supervisor is never silently overruled |
| H9 | Verify vs **normal** (the rate before the alarm), not just vs the spike, on >= 500 answered calls: verified / not_back_to_normal / not_improved | See finding 2 below |
| H10 | Rejections: existing gate rules (per target, G17) | Already built |

Each paid call's result is saved with its diary line immediately, so a restart doesn't pay twice
(a crash in the instant between the sim's answer and our commit could; no endpoint exists to look a
test drive up again; documented).

**First scenario runs found three problems (fixed):**
1. **False alarm on scenario 01:** the drift check looked at 3-hour windows: 13 made-up in 257 calls
   (normal predicts ~4) tripped it. Looking every 15 min for days, luck does that. Scripts don't
   drift in 3 hours; drift is now judged on a whole day (>= 500 calls). A *switch* keeps the fast check.
2. **The AI idea it then chose (P4) made things worse** (2-3% made up vs 1.6-1.9%; that workflow's
   made-up calls 54 -> 125), yet the diary said "verified" because it compared with the 5.1% spike.
   The grader called it "beneficial" (engagement +0.05), which is exactly why we don't trust one
   number. Verify now compares with normal.
3. **Tested 3 ideas, not Kiran's 2:** once P4 became the fix, the "first 2 untested ideas" list
   shifted. The list is now fixed when the AI answers.

**Results**

| | Before C8 | After |
|---|---|---|
| Kestrel conclusion rate vs baseline | +25.4% | **+37.1%** |
| Kestrel calls with made-up claims (baseline 348) | ~431 | **94** |
| Bad-script incident | fixed by nobody | **fixed in 195 min, approved** |
| Credits used | 0 | 12 |
| Scenario 01 | 0 changes | 0 changes, 0 credits |

ALL workflows +11.5%; 19 changes graded: 16 beneficial, 1 neutral, 2 harmful (the caller-ID trials).
75 tests pass; crash test PASS.

**Lesson:** a test that finds no alarm on a clean scenario is worth as much as one that finds the
incident. Scenario 01 caught both the false alarm and a wrong "verified".

### 2026-11-08: Script health review (round 1) and fixes

Reviewer verdict: **request changes**. 0 critical, 2 high, 6 medium, 6 low.

| Finding | Sev | What was wrong | Fix |
|---|---|---|---|
| R-H1 | High | the 2-luck-unit margin was applied to the **proven** script too: with 300 test conversations a script like P1 (truly ~99%) fails it ~20% of the time, gets skipped, and an AI idea of unknown engagement is proposed. Scenario 02 depended on an 80% chance | a proven script only needs the approver's bar (real calls already proved it); the margin stays for AI ideas |
| R-H2 | High | after our fix went live, a later switch (e.g. supervisor back to P2) went unjudged for hours and the verify line then blamed our fix for their script | if the fix is no longer live: close as "superseded", judge the new script at once |
| R-M | Med | for 14 days after a switch only the cumulative since-switch check ran, diluting a later drift | both checks run every step; alarm on either |
| R-M | Med | a script we flagged could later be the clean reference or even "proven" | scripts ever flagged are remembered per workflow (`bad_scripts`) and never used as either |
| R-M | Med | with no proven script, the AI question could use the reserve but its test drives couldn't (paying for names we can't test); `ai_ideas_to_test: 0` still asked | same "essential" rule for both; no question when 0 ideas would be tested |
| R-M | Med | one rejection froze the incident for a day while other validated options sat ready | a turned-down fix moves to the next option (gate's `attempt_blocked`), diary line |
| R-M | Med | cost recorded only after a paid call; an error after the sim charged made us pay again every 15 min; the HTTP client retried paid POSTs | cost + "pending" marker saved **before** the call; a lost answer stays counted (diary `paid_lost`); paid calls never auto-retried (`send(retry=False)`) |
| R-M | Med | verify ignored engagement | verify also tests engagement vs normal; a clear drop writes `engagement_drop` |
| R-L | Low | refusals counted as spent and permanently skipped the proven script | refusals not charged; retried (up to 2); a waiting proven fix beats any AI idea |
| R-L | Low | 3 flagged calls against a tiny reference could alarm | `min_made_up: 10` |
| R-L | Low | verify thresholds hard-coded / borrowed | `verify_min_calls`, `verify_z` knobs |
| R-L | Low | duplicated sum helpers | one `store.sum_rows` used by both features |
| R-L | Low | missing tests; one test checked time going backwards | 6 new tests (near-margin proven script, fix superseded, turned-down fix, refused call, lost answer, no-ask at 0 ideas); test fixed |
| Open Q | | after a hold is released the note no longer says the script was a supervisor's | rationale says "the script being replaced was set by a supervisor at ..." whenever it was |
| R-L | Low | essential spending has no cap across workflows | **documented**: per-incident budget bounds it; a global cap belongs with C15 (fair allocation), which we cut |

**Results:** unchanged on both scenarios (same decisions): Kestrel +37.1%, incident fixed in 195
min, 12 credits; scenario 01 zero changes. 81 tests pass; crash test PASS.

**Lesson:** a safety margin is right for evidence we don't otherwise have (an AI idea) and wrong
for evidence we already hold (real calls). Applying one rule to both quietly made the good path a
coin flip.

### 2026-11-08: Script health review (round 2) and fixes

Reviewer re-checked round 1: both highs and all mediums fixed; pending-before-pay traced through
every exit and found correct. Verdict **comment** (mergeable after two mediums), both in how the fix
is chosen, both introduced by my "retry a refused proven fix first" rule:

| Finding | Sev | What was wrong | Fix |
|---|---|---|---|
| M1 | Med | "refused, retry later" and "can't pay now" looked the same, so a proven fix that could never be retried (no credits) blocked an AI idea that had already passed, forever | `_test_drive` says *why* there is no result (`refused` / `no_credits` / `exhausted` / `turned_down`); only a retry that can really happen waits |
| M2 | Med | a proven script refused twice stopped the loop, so the next proven script was never tried and an AI idea won | refused-twice (or turned down) scripts are skipped and the loop moves on |
| L | Low | a script the approver already turned down was paid for before being skipped | checked before paying |
| L | Low | closing an incident (someone else's switch) dropped a lost payment without a diary line | recovered first |
| L | Low | one false alarm removed a script from "proven" forever | flagged scripts expire after `reference_days` |
| L | Low | AI ideas could use the reserve while a proven fix was about to be retried | optional then |
| L | Low | "no fix" note missing with 0 ideas, not repeated after a turned-down fix | both fixed |
| L | Low | old state lacking `ref_engaged` broke verify | tolerated |
| L | Low | an engagement drop is only a diary line | **kept, documented**: changing scripts needs a person anyway; the line says so |

**Verified:** 84 tests pass (3 new); scenarios unchanged (Kestrel +37.1%, fixed in 195 min, 12
credits; scenario 01 zero changes); crash test PASS.

### 2026-11-08: Script health review (round 3): mergeable

Reviewer re-checked round 2: both mediums and all lows fixed; the new "reason instead of None"
return values are handled safely at both call sites. Verdict **comment / mergeable**, no critical
or high. Leftovers, fixed straight away:
- **M:** an AI question skipped to protect the reserve (optional at the time) was remembered as
  final, so when it later became the only way to a fix it was never asked. A skip is now re-checked
  every step; test added (wallet on the reserve, proven fix refused twice, the AI is then asked).
- **L:** skipping an exhausted / turned-down proven script now writes a diary line.
- **L:** a flagged script's mark is refreshed while its incident is open, so it can't expire while
  still live; an unexpected test-drive outcome raises loudly.

**Verified:** 85 tests pass. Scenario runs not repeated: these paths (refusals near the reserve)
don't occur in scenarios 01/02, which the round-2 runs already cover.

### 2026-11-08: Load test (scenario 03) and raw-event retention (D6)

**First attempt died: the Mac's disk filled up** (460 GB, ~419 GB used by other things). The sim
writes ~7 GB of event files for scenario 03 (run + baseline) and our Postgres kept every raw event.
At 100% the sim returned 500s, the controller died, Docker Desktop crashed. Cleaned up only this
run's own files; Kiran freed space and restarted Docker.

**What it exposed: D6 was only half built.** Day 1 said "keep running totals forever, raw events
for a bounded window". The totals existed; the window didn't. **Built now:** `storage.raw_event_days`
(default 3). Once per sim day, raw `call` and `lead` events older than that are deleted, with a
diary line; config changes, approval decisions and campaign events (small) are kept for good.
Only two things read raw events: the 24 h lead count (needs 1 day) and "when did the script last
change" (config events, kept). The crash test keeps everything (3650 days), since it checks
exactly-once ingestion against the raw events.

**Results (scenario 03: 12 workflows, ~650k dials/day, 7 history days + 5 days, 9.7M events)**

| Measure | Result |
|---|---|
| Whole run, wall clock | **326 s** (5.4 min), exit 0, no errors |
| History ingest | 5.54M events in **136 s** (~41,000 events/s) |
| One 15-min loop step (ingest + features + gate) | median **0.48 s**, p95 0.62 s, max 5.2 s (the day's batch upload) |
| Ingest per step | median 0.28 s for ~13,500 events |
| A full sim day | ~46 s of wall time, i.e. ~1,900x faster than real time |
| Peak memory (controller) | **69 MB** (chunks are streamed, never loaded whole) |
| Pruning | ~840k raw events/day deleted in 2.5-4.7 s |
| Postgres after the run | 6.2 GB (2.5M raw events kept + space freed by deletes, reused by Postgres, not returned to the OS); scoreboard 0.7 MB, diary 0.1 MB |
| Grade | 0 changes, 0 credits, +0.0% on all 12 workflows |

**Reading the grade honestly:** scenario 03's header says its hidden truth "mirrors scenario 1 per
workflow type", which has no incidents, so zero changes is the right answer (same as scenario 01:
no false alarms at 20x the volume). It proves we keep up and stay quiet, not that we find lift.

**For production (SUBMISSION, scale section):** deleting rows leaves space Postgres reuses but
doesn't hand back; at Formi's volume partition `events` by day and drop whole partitions (instant,
space returned). The scoreboard grows by ~0.1 MB per workflow-day, so it never needs pruning.

**Verified:** 86 tests pass (retention test added); crash test PASS with keep-all settings.

### 2026-11-09: C6 + C7 experiments (greetings and voices): decisions

**The idea, as a story.** A restaurant wants to know if a new opening line keeps more diners. It
can't ask the chef's opinion (the LLM); it tries both lines on real diners at the same time and
counts. If the new one clearly wins it becomes the main line, but a few tables keep the old line so
we notice if diners get bored of the new one (fading). If it fades, go back.

**What the data allows.** The 14 history days used only W1 and V1, so we start knowing nothing about
the others. Answered calls/day: ks_cancel ~1,180, nw_renewal ~950, nw_kyc ~510. Days to be sure of a
winner on ks_cancel (equal split, z 3, 80% power):

| Real difference | 3 greetings (~390 calls/day each) | 4 voices (~295/day each) |
|---|---|---|
| 5 points | 7.5 days | 10 days |
| 8 points | 2.9 days | 3.9 days |
| 10 points | 1.9 days | 2.5 days |
| 15 points | 0.8 days | 1.1 days |

Why: luck makes two equal greetings differ by ~±14 stayers a day. A 5-point edge is ~20 extra
stayers a day, hidden in that wobble until ~7 days; a 15-point edge is ~59 a day, obvious in one.
Runs last 8-10 days: we will find big winners, not small ones (Product's "speed vs false winners").

| # | Decision | Why |
|---|---|---|
| E1 | Winner only by statistics: primary KPI beats the current setting by z >= 3 | 5 challengers x ~8 daily looks = 40 chances to get lucky; 5% overall needs ~1 in 800 each, which is z 3 |
| E2 | Winners judged once a day (09:00, ready for 10:00 approvers) | Looking every 15 min is "peeking" and invents winners |
| E3 | Greeting and voice tested at the same time | The sim draws them independently per call, so one test answers both |
| E4 | Compare arms hour by hour (and per cohort), then add up | Ramps and harm stops give arms different hours; pooling would favour an arm that ran in good hours (Simpson, our deferred M1) |
| E5 | Harm stop checked every step once an arm has 200 answered calls: clearly worse (z <= -3) means removed at once, urgent | Compliance: "worse for a cohort, even briefly, is a board conversation" |
| E6 | Guardrails before promoting: pickup within the policy's margin, made-up calls not up | KPIs stay separate |
| E7 | No experiments where the primary KPI is pickup (nw_kyc) | People decide to pick up before they hear the greeting or voice; testing can only hurt the guardrail. A general rule, so it holds on hidden scenarios |
| E8 | Evidence = the variant on each call event | Product: an arm that never ran must not count as learning |
| E9 | No winner after enough calls to find an 8-point lift: end the test, back to the old setting, retest after 3 days | Don't leave customers on a coin flip forever |
| E10 | 0 credits | CTO: the best compute is no compute |
| E11 | **Kiran: the split is a per-business knob.** `control_share: equal` (default, fastest) or a number (e.g. 0.5: the old setting keeps half) | A cautious client can be set to 0.5 without code |
| E12 | **Kiran: harm guard for groups**, not per-group winners. Winner chosen on everyone; before promoting, each group (language, age band, city tier, value band, one at a time) is checked; a group where the winner is clearly worse keeps the old setting through a named cohort | Per-group winners need ~7 days even for big effects (Tamil is ~48 calls/day per voice) |
| E13 | **Kiran: keep 20% on the old setting after promoting**, compare at the same time to spot fading (~2 days for a vanished 10-point lift). On fading: back to the old setting, the faded option "worn out" for 3 days, test the rest again | Same-time comparison can't be fooled by a bad day that hits everyone (like the bad script); costs 20% of the lift |
| E14 | **Kiran: auto-apply where policy allows** (ks_cancel, hourly 0.2 steps), every step in the diary. A supervisor edit stops that dimension's test at once; their choice becomes the new "old setting"; data so far kept; 24 h hands off, then only via human approval | Policy permits auto; the supervisor's own edit is the signal they want a say |

### 2026-11-09: C6 + C7 built, first runs

**Built:** `controller/features/experiments.py` (one change per workflow carrying both dimensions and
any group carve-outs), `stats.stratified_lift` (hour-by-hour comparison) and `stats.calls_needed`,
schema v6 (`leads` table with each lead's features, `segment_stats` scoreboard per customer group,
filled in the same ingest statement as call_stats), `Change.approval_only` (the gate never auto-applies
it), `experiments` knobs in settings.yaml. 17 new tests (Simpson example, harm stop, daily winner,
guardrail block, group carve-out, no-winner end, fading rollback, supervisor hands-off then
approval-only, approval-only at the real gate, per-group ingest).

**Scenario 01** (reports/c6_01.md): ALL conclusion -0.5%, 25 changes applied: 5 beneficial, 20
neutral, **0 harmful**, 0 credits.
- nw_renewal: tests live D0 12:15. **Greeting W3 promoted D3 09:00** (62.9% vs 54.8%, beneficial
  +0.049). **Fading caught D6 10:45** (lead went from +7.8 to -3.1 points over 48 h). The grader
  confirms W3 is a decaying variant. The rollback request was turned down once by the approver
  (10% judgement rejections), so we moved off W3 on D7 instead of D6.
- ks_cancel: no option clearly better (W3 stopped as harmful D2 12:30); tests ended without a winner
  and restarted 3 days later.
- nw_kyc: skipped by E7, yet moved -2.3%: **that is the noise level between two runs** (each test
  changes the sim's random draws for everything after it).

**Scenario 02** (reports/c6_02.md): ALL +6.3% (was +11.5% before experiments), 31 applied: 8
beneficial, 20 neutral, 3 harmful. Incident MTTR 120 min (was 195), 12 credits.
- The drop is **not** from the experiments themselves: the earlier run's big Kestrel pickup gain
  came from caller-ID health flagging D1 as worn out (30% below its past) and switching to a spare.
  This time D1 wore down to ~25% below its past, under our 30% line, so it never fired. This is the
  known wear-out weak spot (C5), shown up by run-to-run chance.
- nw_renewal: the test start was turned down once (judgement), live D2. Greeting W3 led +5 points
  (2.5 luck units) but the test ended at D6 without a winner (we need 3): a real but small win missed,
  the price of strict winners. The supervisor's voice edit (D5 15:00) stopped our voice test as
  designed (E14). **After 24 h we retested voices through the approver; approved; graded harmful
  (-0.044)**, because a 4-way split took 75% of calls off the supervisor's good voice (V2).

### 2026-11-09: C6 + C7, two rules changed after the first runs (Kiran)

| # | Was | Now | Why (from the runs) |
|---|---|---|---|
| E14 | After a supervisor edit: 24 h hands off, then re-test through the approver | **Their choice stands for good**: we never propose a test on that setting again (diary says so). The gate's `approval_only` flag was then unused and is removed | Scenario 02: the re-test moved 75% of calls off the supervisor's good voice V2: graded harmful |
| E9 | No winner: back to today's option, re-test after 3 days | **Keep the leader, no re-test**: options behind are dropped; one still ahead (2+ luck units, `lead_z`) runs 50/50 against today's option until it proves itself (z 3) or falls back below 2; then the test is done for good. Options return only when a promoted winner fades (E13), and a faded option never returns | Re-tests found nothing in either run and once put calls back on a faded greeting (harmful); and W3 was dropped while +5 points ahead |

`retest_after_days`, `worn_out_days`, `hands_off_hours` knobs removed; `lead_z: 2.0` added.

**Final runs**

| | Scenario 01 | Scenario 02 |
|---|---|---|
| ALL conclusion rate | **+2.0%** (was -0.5%) | **+7.3%** (was +6.3%) |
| Changes applied | 18: 6 beneficial, 12 neutral, **0 harmful** | 24: 8 beneficial, 14 neutral, 2 harmful (the two caller-ID trials, known) |
| Experiment wins | nw_renewal greeting W3 (promoted D3, fade caught D6); ks_cancel voice V4 via narrowing (promoted D6) | ks_cancel voice V3 via narrowing (promoted D6); supervisor's voice left alone |
| Credits | 0 | 12 (the bad-script incident, MTTR 120 min) |

The narrowing rule found both Kestrel voice wins: neither proved itself in the 4-way test,
both did at 50/50. 104 tests pass.

**Known weak spots (for SUBMISSION):** a rollback needing approval can be turned down by the
approver's random judgement (one day lost on W3 in scenario 01); small wins (~5 points) still
mostly go unproven inside a 10-day run; group carve-outs overlap (a lead in two hurt groups follows
the first cohort listed); scenario 02's Kestrel total is lower than the C8-only run because the
caller-ID wear-out of D1 stayed just under our 30% line this time (C5 weak spot, run-to-run chance).

### 2026-11-09: C6 + C7 review round 1 (independent reviewer, read the code cold)

| # | Sev | Finding | Fix |
|---|---|---|---|
| R-H1 | High | The "narrowed" flag survived into the test that follows a fade, so that test ended on its first daily check (and asked for 4x the calls) | `start_test` resets it; test |
| R-H2 | High | One intent per workflow: a harm stop or fade rollback waited behind a routine request still out with the approver (hours) | Safety moves (stop / move off) go under their own intent `experiments:<wid>:urgent`, default split only, so they never queue behind anything and stay auto-appliable |
| R-M1 | Med | Two "no"s put a dimension in `declined`, which also stopped harm and fade checks: a harmful option could stay live for good | No `declined` phase. A safety move is asked again a day later; a turned-down promotion or narrowing ends the test; a test start/end is left as it is |
| R-M2 | Med | A rollback test could bring back an option already stopped as harmful | `harmed` list, excluded like `faded` |
| R-M3 | Med | Routine bundles (greeting + voice) count a "no" against both targets | **kept, documented**: the approver gives no per-item reason; safety moves are separate now (R-H2) |
| R-M4 | Med | Removing carve-outs on rollback put `cohorts` in the change, so it could never be auto-applied and needed the approver | Carve-outs stay after a rollback or the end of a test (they pin a group to the old option: harmless) |
| R-M5 | Med | A supervisor edit to one cohort's greeting left our test split live in `default`, unwatched | If our split is still live, it goes back to today's option once, then hands off for good |
| R-L1 | Low | Room for carve-outs ignored the other dimension's carve-outs (sim max 20 cohorts) | counted |
| R-L2 | Low | A winner blocked by a guardrail kept the test open forever | the "enough calls" end now applies to it too |
| R-L3 | Low | A lead with a null feature (`{"lang": null}`) would break ingest of its chunk for good | `coalesce(value, '?')`; test |
| R-L4 | Low | `strata_counts` scanned the whole history per call | `sim_day >=` bound so the primary key narrows it |

Also: the "end test" target names its options, so a "no" to one test's end never counts against
another's. Rules 2-5 held (statistics only, only what ran counts, atomic state + diary, idempotent
ingest incl. per-group counts, no sim imports). 23 experiment tests.

**After round 1:** 109 tests pass. Scenario 01 ALL +1.9%, 0 harmful; the W3 rollback request now goes
out at once under its own urgent intent (D6 13:15, was 16:30), so the share still on the faded W3 on
day 7 fell from 68% to 46% (the approver still turned the first ask down). Scenario 02 ALL +7.3%,
2 harmful (the known caller-ID trials), MTTR 120 min, 12 credits.

### 2026-11-09: C6 + C7 review round 2

Round 1's fixes held (R-H1, R-M2, R-M4, R-L3, R-L4 confirmed; two intents can't undo a safety move,
because a request built on an older config version fails as stale). New findings, mostly in the R-M5
"supervisor edited one cohort" path I added:

| # | Sev | Finding | Fix |
|---|---|---|---|
| R2-H1 | High | Our clean-up of the leftover test split stopped after its first auto step (the split no longer "looked like ours"), leaving a half-way split nobody watched | The clean-up stays ours until it reaches its target; only a newer supervisor edit cancels it |
| R2-H2 | High | That clean-up also deleted carve-out cohorts, including a supervisor's own pin on one (breaks rule 1) and forced approval | A supervisor-owned dimension never touches cohorts: only the default split |
| R2-M1 | Med | An "end the test" turned down twice left the test split live with no harm checks | The test goes on, and so do the checks |
| R2-M2 | Med | Narrowing could pick a winner that had just failed a guardrail, doubling its exposure | Failed winners are never the leader |
| R2-M3 | Med (plausible) | A stale request re-queued by the gate could be re-sent in the same step with its old body | Gate: a stale/lapsed re-queue waits one step so its feature refreshes it first (all features) |
| R2-L1 | Low | A day's wait before asking again could carry over to a different safety move | The wait belongs to one attempt |
| R2-L2 | Low | Room for carve-outs missed cohorts kept for a supervisor-owned dimension | counted |
| R2-L3 | Low | A clean-up turned down twice was retried silently forever | diary says the split stays live and unwatched, we stop |

Test gap closed: the tests now run the rejection paths through a fake gate (asked again a day
later, declined end keeps checking, supervisor's cohort pin untouched, half-way clean-up finishes).
27 experiment tests.

**After round 2:** 113 tests pass; scenario results unchanged (01: +1.9%, 0 harmful; 02: +7.3%, 2
harmful caller-ID trials), since the fixed paths don't occur in them. Crash test (5 kills, scenario
02 with experiments now live): PASS, 24 of our changes in flight across kills, none applied twice.

### 2026-11-09: C6 + C7 review round 3: no High findings

The reviewer confirmed every R2 fix works and that no path can override a supervisor (only the
controller and supervisors write config; every supervisor write is a hold, ingested before the
features run; a clean-up already out goes stale on the version check).

| # | Sev | Finding | Fix |
|---|---|---|---|
| R3-M1 | Med (plausible) | A promotion turned down twice left its carve-outs in the state; they could be applied mid-test later | The carve-outs kept are only those that actually went live (`live_carve`); test |
| R3-L1 | Low | A narrowed leader that then fails a guardrail kept 50% of calls until ~4x the calls | Test ends at once; test |
| R3-L2 | Low | A request that goes stale late in the approver's morning window now waits one step, so it can miss the window and wait a day (R2-M3's price) | **kept, documented**: one stale request a day late beats re-sending an outdated body |

C6 + C7 closed after three review rounds: 29 experiment tests, 115 in all, crash test PASS.

### 2026-11-09: C12 "why did it change" tool; finishing plan

**Kiran: build a small why-tool (C12), skip the bigger supervisor view (C11) and loop dashboard
(C14), then SUBMISSION.** Why: C12 is part of the mandatory floor ("anyone can answer why did X
change at 14:00 on D3, who approved it, what happened next"); before, answering meant writing SQL
on the diary by hand.

`python -m controller.why ks_cancel "D3 13:30"` (or `--list`). Read-only, from our own records only
(diary, requests, changes; never the sim). For each config change near that time it prints:
- what changed and who did it (supervisor, client approver, or auto within policy);
- for ours: the request's summary and rationale, the approval id with when it was asked and decided,
  affected leads, impact score and validation run, and the feature lines that led to it;
- what happened next (24 h of follow-up lines) and the KPIs 24 h before vs after.

Examples on scenario 02 saved in `reports/why_examples.md` (the bad-script incident: supervisor
edit D3 11:30, our fix approved in 30 min and applied D3 13:30, verified D3 21:00). 2 tests.
Final grade reports copied to `reports/public_01.md` / `public_02.md` (the c6_* files are the same runs).

### 2026-11-09: Final load test (all features live) and SUBMISSION.md draft

**Load test rerun with the final code** (the first one predates experiments and per-group counting):

| Measure | First run (D6) | Final run |
|---|---|---|
| Whole run | 326 s | 500 s |
| History ingest | 41k events/s | 24.7k events/s (per-group counting adds ~4 rows per call) |
| Live ingest | | 31.8k events/s |
| Loop step, median / p95 / max | 0.48 / 0.62 / 5.2 s | 0.67 / 0.98 / 7.3 s |
| One sim day | ~46 s | ~54 s (SRE target 600 s) |
| Peak memory | 69 MB | 66 MB |
| Grade | +0.0%, 0 changes | **+4.7% ALL, 48 changes: 24 beneficial, 24 neutral, 0 harmful** |

The 12 load workflows mirror public_01's truth (no incidents), so the lift is the experiments alone;
the 4 KYC workflows ran none (pickup primary). `reports/public_03.md` is now this grade.

**SUBMISSION.md drafted** from this journal: all 15 template sections, the six stakeholder replies
(<=120 words each, ready to paste), scope table C1-C15, architecture diagram, the learning loop with
the false-winner maths, governance state machine, the public_02 incident timeline, measured scale,
crash test, the why-tool output, results with interpretation, trade-offs, 11 known weaknesses, and a
two-week production plan. Waiting on Kiran: review, and his hours.

### 2026-11-09: Consistency review of SUBMISSION.md and the docs

Journal renamed `prin_notes.md` -> `notes.md` (Kiran); code comments and tests now point to `notes.md`.

An independent reviewer checked every number and claim in SUBMISSION.md against the reports, crash log,
load log, code and this journal. Numbers in the results tables, crash test and load test all matched.
Fixed:
- **Wrong memo claim missed:** Meera says caller-ID rotation and timing are covered by her policy; on KYC
  auto-apply is off. Now listed, and her reply corrected.
- **"Supervisor edits are final" was too broad:** true for greeting/voice (E14); other settings are held
  until 400 calls and the next morning (G21), and an urgent script fix may override, labelled (H8).
  Assumption 10, Anita's reply and section 6 now say exactly that.
- **"A failed winner is never the narrowed leader"** was wrong: only not on the day it fails (V3 failed D3,
  led later, promoted D6). Fixed text.
- **why-tool:** printed every change within +/-120 min, not "the nearest" as documented. Now prints the
  nearest change (and others at that same minute); "after" KPIs start at the next hour; unknown actors
  labelled. `reports/why_examples.md` regenerated with the plain documented command.
- nw_kyc noise figure (-1.9%, not the superseded run's -2.3%); "zero changes on the clean scenario" ->
  "zero false alarms from the incident detectors"; the SQL example range; 402 handling (a refused paid call
  is diaried, not charged, retried once); expired is not a strike; luck units defined once.
- **Added:** end-to-end decision lag (caller ID 1 h to flag, script 75 min to flag + 45 min to apply), the
  10x extrapolation against SRE's target (~9 min per sim day, so sharding is needed, not optional), the
  peeking trade-off for harm/fade checks, fading results for public_02/03, `reports/load_test_03.md`, the
  crash test's raw-retention setting, LEAD_KEEP_DAYS, a 5-line TL;DR, Anita's reply trimmed to 115 words.

**Sources for weakness 1 (slow wear-out), so it isn't unsourced:** Kestrel D1 answer rate per day in the
final public_02 run (`call_stats`, caller_id D1): D0 24.7%, D1 23.2%, D2 22.2%, D3 20.9%, D4 19.9%, D5 18.7%,
D6 19.1%, D7 17.4%, D8 18.0%, D9 18.0%. In the C5 run (database `formi_c5`), D1 was flagged D5 17:00 (15.7% vs
22.4% on its own past, 30% worse) and after switching to spare D3 the workflow answered 29.0% (D6 09:30).

### 2026-11-09: Caller-ID load spreading (C5 extension): decisions

**Why now.** Comparing results with other public submissions showed most of their extra lift came
from moving calls across phone numbers. We had seen the same pattern ourselves on Day 2 (C5 run,
"a number wears out when it carries everything") and chose then not to act, to avoid tuning to one
public scenario. Our own evidence since:
- Kestrel D1, carrying calls daily, slid from 24.7% answered on D0 to ~18% by D7-D9 (final public_02 run).
- When worn D1 was switched off, the never-used spare D3 answered 29.0% vs D1's 15.7% (C5 run).
- After ~2 days' rest, D1 passed its trial at 24.9% (C5 run): rest restores a number.
- KYC used only D1 for all 14 history days; D2 and D3 were never used.
Built from our own observation and design; nothing taken from other submissions' code.

**The idea (shoes):** wear one pair daily and it wears out; rotate pairs and each lasts longer.

| # | Decision | Why |
|---|---|---|
| L1 | Spread calls **evenly across all healthy numbers**, including never-used spares (**Kiran: equal split**) | Each number carries less, wears slower. Replaces "spares only when nothing healthy is left" |
| L2 | A never-used spare first gets a **trial: 20% of calls, shared between the spares (Kiran; lowered to 10% after the load test, see below)**, judged with the existing trial rules (2.5 luck units clear of the 30% line vs the numbers in use); passes: equal share; fails: excluded for good | A spare may be bad from the start; the trial limits exposure |
| L3 | **From D0 (Kiran)** | Numbers already carried everything through 14 history days; prevention helps most early |
| L4 | Lives inside caller-ID health (one split per workflow) | Two features on one split would overwrite each other |
| L5 | Follows each client's policy: auto in steps where allowed; KYC via the morning note (new campaigns only) | Nothing new in governance |
| L6 | A "spare" is a number with **no calls in our records at all**. A number that had calls and is now unused was removed by someone; if not by us, it was a supervisor, and we leave it out. If a supervisor ever edited caller IDs on a workflow, no spreading there | Never undo a supervisor's choice (hard rule) |
| L7 | Dead-number removal, second-chance trials and the supervisor rules stay as they are | Spreading adds to the safety rules |
| L8 | Knobs: `spread_spares: true`, `spare_trial_share: 0.2` | Business can switch it off per client |

### 2026-11-09: Load spreading built; first runs; harmful changes fixed; review round 1

**First runs with spreading:** public_01 ALL +1.9% -> **+14.7%** (0 harmful); public_02 +7.3% -> **+27.3%**
(KYC +24.8%, Kestrel +53.1%). The grader marked the KYC spare trials and spreads beneficial one after
another (+0.08 to +0.14 pickup each).

**Kiran: the 3 harmful changes in public_02 must be fixed too.**

| # | Harmful change | Cause | Fix |
|---|---|---|---|
| L9 | Two 5% second-chance trials of dead Kestrel D2 | A dead number gets re-tried even though healthy numbers carry the calls | A removed number gets a second chance only when fewer than `probe_min_healthy` (2) healthy numbers are left |
| E15 | nw_renewal greeting W3 promoted on D4 | Won on the whole test (D0-D3: +5.9 points, 3.1 luck units) but was already behind over the last 24 h (-2.3 points): a fading variant | A winner must also be ahead over the last `recent_hours` (24); otherwise not promoted that day. For a true 10-point winner this delays promotion <1% of the time, a 5-point one ~9% (a day's delay, never a loss) |

The incident MTTR in that run was 240 min (was 120): luck, not the change. P2's made-up rate crossed our
bar later (16/279 calls by 14:00 vs 10/120 by 12:45 in the earlier run) and the approver took 66 min.

**Review round 1 of spreading (independent reviewer): 3 High, 4 Medium, all fixed except noted:**

| # | Finding | Fix |
|---|---|---|
| R-H1 | A failed spare's removal turned down twice made the gate refuse every later caller-ID change on that workflow ("given up" on the bundle) | A turned-down removal of a failed spare is left out of new bundles like any removal; it keeps its share |
| R-H2 | Every later change carried "trial spare N" even for live trials, so a rejected bundle could push unproven spares to an equal share | A spare is a target only until its trial is live |
| R-H3 | With all used numbers dead and a spare on trial, the "switch to a spare" rule found nothing, so dead numbers kept 80% | Spares on trial take over the cohort |
| R-M1 | With spreading, a recovered number was forgotten before its share grew; if spreading was then blocked it stayed at 5% | Done only once its share has grown past the trial share in config |
| R-M2 | A number whose removal was turned down got an equal (bigger) share | It keeps its current share |
| R-M3 | Failed/declined spares counted as healthy references | Added to the not-healthy set |
| R-M4 | A cohort with its own caller-ID split spreads only over its own numbers plus trial spares | **Kept, documented** (cohort splits are usually a supervisor's, and a supervisor edit switches spreading off) |
| Low | "No calls in our records" can't see numbers a supervisor removed before the 14-day history, and is per workflow | **Documented** |
| Low | supervisor-edit query twice; summary share ignored scaling; scenario-specific text in the rationale; redundant test fixture; a test that passed for the wrong reason | fixed |

Tests now drive spreading with a fake approval gate (failed-spare removal turned down, all numbers dead
with a spare on trial, live trial not a target).

### 2026-11-09: Harmful changes, round 2, and spreading review round 2

**Run after L9 + the first E15:** public_02 harmful 3 -> 1 (both caller-ID trials gone). The remaining one
was greeting W3 again, through other doors:
1. **A waiting promotion.** W3 won on D2 (really ahead then), the approver said no, and the retry on D4
   carried D2's decision after W3 had started fading. Fix: a promotion not yet live is re-judged every
   morning (`recheck_pending`) and withdrawn if it may be fading.
2. **A weak bar.** On D4 W3 was still ahead over the last 24 h, but by +3.4 points against +10.3 claimed.
   Fix: E15 uses E13's own fading bar, so a recent lead under half the claimed lead counts as fading (run 1:
   -2.3 vs +5.9; run 2: +3.4 vs +10.3; both caught). A true steady winner is delayed a day ~9% of the time.
3. **Narrowing.** W3 sat at exactly 3.0 luck units (not a "winner"), so as the leader of a no-winner test it
   was given 50% of calls. Fix: the fading guard applies to every move that gives an option more calls
   (promote, narrow, waiting promotion), and a fading leader means no decision that day (a day's wait).

**Spreading review round 2 (independent reviewer):** all round-1 fixes confirmed.

| # | Sev | Finding | Fix |
|---|---|---|---|
| R2-H | High | A request out with the approver carrying "promote W2" + a voice change: after the promotion was withdrawn, the gate kept the request (same intent, still listed) and would apply it, fading greeting included | **Gate:** a request still out whose targets are no longer all wanted is marked withdrawn and never applied; a fresh request follows. Test against the real sim. Caller-ID bundles that only *grow* (a second dead number) are unaffected |
| R2-M1 | Med | When spares took over (all used numbers dead), nothing judged them (no healthy reference), and a removed number's trial had no reference either | Spares carrying calls alone become ordinary numbers, judged like any other |
| R2-M2 | Med | E15 had no minimum calls: a quiet day (no shared hours) read as "fading" | Needs `decay_min_calls` a side; otherwise no verdict |
| R2-M3 | Med | A fading winner ended the test the same morning (counted as "failed") | A fading winner/leader means no decision that day |
| L | Low | A withdrawn promotion partly applied in auto steps froze the half-step | Back to the pre-promotion test split |
| L | Low | "no second chance" diary line on approval-only clients, where trials never happen | Only where caller IDs can be changed by us |
| L | Low | For a test younger than 24 h the recent window is the whole test; restoring with spreading blocked and a before-share under the trial share never completes | **Documented** (the pending recheck and E13 cover the first; the second needs an odd split) |

### 2026-11-09: Spreading review round 3; load test; trial share 20% -> 10%

**Review round 3:** the round-2 fixes held, but my gate fix (withdraw a request whose targets shrink) had
a High regression: it also withdrew **urgent** safety requests. "Stop W2" waiting for the approver, then W3
turns bad too: the new urgent request says "stop W3", the old one was withdrawn, and proven-bad W2 kept its
calls for another approval round. Fixes:

| # | Sev | Finding | Fix |
|---|---|---|---|
| R3-H | High | Shrink-withdraw hit urgent safety requests | Applies to routine requests only (promotions are routine). Test against the real sim: an approved urgent stop is still applied |
| R3-M | Med | A rejection of a request we had already withdrawn counted as a "no" against targets we still want | Withdrawn requests close as dropped, never rejected/expired |
| R3-M | Med | "A fresh request follows" only after the approver answers the old one | Documented; the test now checks the fresh request is queued once the old one is closed |
| R3-M | Med | A narrowed test with a fading leader stayed 50/50 indefinitely | Once the narrowed test had all its calls it ends ("may be fading") |
| L | Low | Fading bar written twice | One helper, `fading_bar` |
| L | Low | Test gaps (quiet day, narrowed fading end, urgent never withdrawn) | Added |

**Load test (all features):** +18.4% ALL (was +4.7%), 80 changes, 41 beneficial, but **2 harmful**: on two
renewal workflows the first spare trial gave never-used D4 10% of calls and D4 is a bad number (pickup
-0.020, graded harmful; the same move in public_01 was -0.018, graded neutral: right on the line).

**Kiran: lower the spare trial share from 20% to 10% total** (~5% per spare when there are two). Halves the
cost of trying a bad spare; a trial takes about a day instead of half a day.

### 2026-11-09: Getting public_02 to zero harmful (Kiran: "keep trying for 0")

The last harmful change was always greeting W3 on nw_renewal: a planted fading variant. Three attempts:

| # | Attempt | Result | Lesson |
|---|---|---|---|
| E16a | **Two-step promotion**, step 1 = 50% | W3's step-1 move cost -0.017 engagement (vs -0.048), step 2 never happened; still graded harmful | Smaller steps cut the damage but not the count |
| E16 | Step 1 = a **canary**: current share + 10 points (33% -> 43%); the main share the next morning only if the winner keeps half its lead over the last 24 h on the step-1 split | -0.010, still harmful. The grader's line for engagement is tighter than for pickup (-0.006 neutral, -0.010 harmful) | Kept: it limits damage from any fade right after promotion, at the cost of a day |
| E17 | **Fresh evidence after a "no"**: every harmful W3 promotion went live on D4 only because the approver randomly turned it down on D2 and we re-sent D2's evidence. Now a turned-down promotion sends the test back to testing, and the winner must clear the winner bar again on calls made after the decision (also for narrowing) | **public_02: 0 harmful**, +28.0%. W3 had only +5.4 (1.4 luck units) and +6.9 (2.5) on new calls on D3/D4, so it was never re-proposed | The real flaw was re-using evidence the approver had already said no to; standard practice is not to re-submit the same data after a rejection |

**Kiran-facing honesty:** with each change the numbers move a little by luck (each run is a different shuffle),
so "0 harmful" is one run's outcome, not a guarantee. What the rules guarantee: a promotion is never sent on
stale evidence, never jumps straight to 80%, and a winner losing half its lead is never given more calls.

**Also found:** in one public_01 run, Kestrel's made-up rate doubled from D5 (1.4% -> 2.3-3.4%) evenly across
every variant (not in the baseline run). The drift detector rightly flagged it; with no other proven script,
the rules took the AI's cleanest idea, P4 ("persuasive"). Graded beneficial (engagement +5 points) but P4 is
no safer (3.1% vs 2.8% for P1 over those days) and Compliance would question it. **Weak spot for
SUBMISSION:** when the main script drifts and nothing proven is left, the fallback is an AI idea with its own risks.

**Final review of E16/E17 (independent reviewer): 1 High, 3 Medium, fixed:**

| # | Sev | Finding | Fix |
|---|---|---|---|
| R4-H1 | High | A turned-down promotion's group carve-outs stayed in state and could go out later under a "test" attempt, getting around the approver's no | Reset to the carve-outs actually live; test |
| R4-M2 | Med | While step 2 waited for approval, the step-1 winner was not watched for harm/fading (live cleared) | Step 1 stays live and watched; "step 2 live" is detected from the config |
| R4-M3 | Med | After a "no", the test usually ended the next morning on too few new calls | No end/narrow decision until each option has the calls needed on new data; test |
| R4-M4 | Med | A narrowed 50/50 test did not skip step 1 as documented | It goes straight to the main share; test |
| R4-L | Low | `evidence_since` never cleared; re-promotion while still cooling down; step 2 refused twice ended the whole test | Cleared on a new test or promotion; skipped while cooling down; a refused step 2 keeps the winner at its step-1 share, still watched |

### 2026-11-09: Final results (all features, final code)

| | public_01 | public_02 | public_03 (load) |
|---|---|---|---|
| Conclusion rate (ALL) | **+11.0%** | **+26.8%** | **+18.7%** |
| Changes / harmful | 26 / **0** | 26 / **0** | 88 / **0** |
| Credits | 0 | 12 (MTTR 255 min) | 0 |

138 tests pass; crash test PASS (5 kills, 324,315 events once, 26 changes each applied once); load: 557 s
whole run, 58-76 s per sim day, 66 MiB, ~25-30k events/s. A new public submission (PR 6) scores higher on
lift (+15.7 / +32.6 / +21.8%) with per-person call hours (C4, which we cut); Kiran chose to finalize.
SUBMISSION.md, reports, why-tool examples and the prep guide updated to these numbers.
