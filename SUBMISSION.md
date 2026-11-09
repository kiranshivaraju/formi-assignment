# Outcome-First Agent Loop: Submission

**Name:** Kiran · **Date:** 2026-11-09 · **Hours actually spent (be honest):** about 18 (Sat 7, Sun 4, Mon 7), a little over the suggested 12-16, mostly on three review rounds per feature ·
**Language / storage:** Python 3.11 · Postgres 16 (Docker, started by `run.sh`)

How to run: start the simulator, then `./run.sh http://127.0.0.1:8700`. The full decision journal (every
decision, why, what we tried and what broke) is `notes.md`; decision ids like G7 or E13 below point there.

---

## 0. TL;DR

1. **What it does:** an always-on, crash-safe controller that spreads calls across healthy phone numbers (switching off dead ones), fixes bad scripts with approved, validated changes, and runs greeting/voice experiments with guarded, two-step promotion and fading rollback; every change passes one governance gate, and `python -m controller.why` explains any change from our own records.
2. **public_01** (normal days, 3 workflows): **+11.0%** conclusion rate, 26 changes (8 beneficial, 18 neutral, **0 harmful**), 0 credits.
3. **public_02** (incidents): **+26.8%** (Kestrel +57.5%); 26 changes (10 beneficial, 16 neutral, **0 harmful**); the supervisor's bad script fixed by an approved change in 255 min (12 credits); the dead caller ID off in 2 auto steps.
4. **public_03** (load, 12 workflows, 9.7M events): one sim day in **58-76 s** (SRE target 10 min), 66 MiB; **+18.7%** with 88 changes, **0 harmful**; a 5-kill crash test passes with every change applied exactly once.
5. **Cut on purpose:** call-time optimisation (C4), the supervisor screen (C11), dashboards/alerting (C14), cross-tenant fairness (C15); reasons in section 2, weak spots in section 13.

## 1. Assumptions

0. **Vocabulary.** "Luck units" below means a **z-score**: how many standard errors a gap between two rates
   is from zero. With ~900 calls a side, two equal options typically differ by ~2.4 points by chance; a 7.6-point
   gap is ~3 luck units, something luck alone does about 1 time in 740.
1. The simulator contract (`docs/SIMULATOR.md`) and the live `/workflows` policy are the truth. Where a
   memo disagrees, the policy wins (CTO memo).
2. We never read anything under the scenario's hidden-truth section, and the controller never imports `sim/`
   or reads `scenarios/`. Policies are read live from the API.
3. **Pickup is decided before the customer hears the greeting or voice.** So greeting/voice tests can't
   help a workflow whose primary KPI is pickup (nw_kyc). They can only risk its engagement guardrail, and we
   don't run them there (E7).
4. A guardrail's `*_max_drop` is an absolute drop in the rate (0.02 = 2 points).
5. `business outcome` is per lead, not per call, so it can't be split by variant. Experiments judge on the
   primary and guardrail KPIs only.
6. `hallucination_flag` is noisy both ways. We act only on populations, never on one flagged call.
7. A validation run measures safety only, never engagement (as the contract says). So a script is never
   chosen for engagement on a test drive alone.
8. The LLM's fix ideas can be wrong or harmful. On a throwaway sim it suggested the bad script itself.
9. Approver decisions are random but reproducible from the proposal id, so proposal ids must be stable for a
   given world (D19).
10. Supervisor edits are deliberate and never silently overwritten (hard rule). A supervisor's greeting/voice
    choice is final: we never test that setting again (E14). Other settings they touch are left alone until
    400 answered calls **and** the next morning (G21). The one exception is an urgent safety fix (a script that
    makes things up), which goes to the client approver labelled "overrides a recent supervisor setting" (H8).
11. One controller drives one simulator at a time. A Postgres advisory lock enforces it.
12. The controller's own thresholds (our "checker knobs") belong to Formi's platform team, not the client.
    They live in `controller/settings.yaml`, typed and range-checked at startup. Client policy is never
    copied into a file.
13. Credits are cheap here and expensive in production: spend only where a decision depends on it.
14. A run's numbers vary by about ±2% from luck alone. A test changes the sim's random draws for everything
    after it. Before spreading, nw_kyc (which we then never touched) moved -1.9% in one run and -0.5% in another.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at scale | **Built** | Floor; everything else reads it. Streamed COPY, one transaction per chunk with its counters and bookmark | Partition `events` by day (section 8) |
| C2 | Lead joins + one shared cohort object | **Partial** | Lead features are joined at ingest (`leads`, `segment_stats`). Cohorts are the config's own cohort objects (carve-outs are config cohorts, comparisons stratify by `cohort_id`). No separate cohort registry | A cohort table shared with a supervisor view |
| C3 | Bootstrapping from history | **Built** | Same ingest path. Feeds the caller-ID "own past" (14 d), the script reference and "proven scripts". History holds only W1/V1, so it gives experiments no prior | Nothing for current scope |
| C4 | Call-time optimisation | **Cut** | Product's P0 headline, but per-person timing needs its own learning loop and experiment budget. Five features done properly beat six sketched | Per-cohort hour weights as another experiment dimension, then per-person preferred hour from answer history |
| C5 | Caller-ID health + load spreading | **Built** | Dead numbers hurt pickup for everyone, and a number that carries every call wears out; spreading calls over healthy numbers (spares tried first) was the single biggest lift. 3 review rounds each | Per-cohort spreading for cohorts with their own split |
| C6 | Greeting/voice experiments | **Built** | Most of the graded "engagement gap", and the core of the "learning loop" | Per-group winners (needs more traffic) |
| C7 | Promotion, decay detection, rollback | **Built** | Graded on hidden scenarios ("decay rollback"). Guarded two-step promotion (section 5). 3+ review rounds | Faster fade detection with sequential tests |
| C8 | Hallucination incident response | **Built** | public_02 incident; regulated clients. 3 review rounds | Script experiments for engagement |
| C9 | Governance client | **Built** | Every feature depends on it; most failure modes live here. 3 review rounds | Per-tenant approver-fatigue budgeting |
| C10 | Honest impact, affected leads, dry runs | **Partial** | Every request carries measured numbers. Dry runs replay the feature's own evidence (the test's calls, the flag-time window), not a counterfactual model | Replay against historical cohorts with variance |
| C11 | Supervisor view | **Cut** | The diary + why-tool answer "what changed and why"; a screen is UI work, not loop work | A read API over the diary, changes and feature_state |
| C12 | Audit store "why did X change" | **Built (minimal)** | Mandatory floor. Append-only diary + `python -m controller.why` | Web front end |
| C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable. `scripts/crash_test.py` + `reports/crash_test.log` | Fault injection at the narrowest windows |
| C14 | Observability of the loop | **Partial** | One JSON log line per step (events, ingest seconds, decisions); pruning lines; diary errors | Metrics export + the alerts in section 10 |
| C15 | Fair credits/attention across tenants | **Cut** | Per-incident credit cap and per-workflow request limits bound the damage today | Global credit and approver-attention budget per tenant |

**Principle.** Floor first (runs end to end, audit trail, crash safety), because nothing else counts without
it. Then the capabilities that change outcomes on hidden scenarios and that a compliance head can sign off:
incidents (C5, C8), then the learning loop (C6, C7). Every feature had to be reviewed cold three times and run
end to end on both public scenarios, with zero false alarms from the incident detectors on the clean scenario,
before the next one started.
We'd rather defend five capabilities in depth than fifteen sketches.

## 3. Stakeholders

### 01 Sales (Nikhil Rao)
- **Accepted:** speed where the policy allows it. Kestrel greeting/voice/caller-ID changes are auto-applied
  in hourly steps; urgent fixes go out the same hour.
- **Declined:**
  - One blended score: Product's definitions and the hard rule keep pickup and engagement separate.
  - "Winning by lunch, live by evening": a lunchtime lead is usually luck (section 5 has the numbers), and
    false winners cost conversions too.
  - The "persuasive" prompt everywhere: its own summary says "freer", which is hallucination risk; nw_kyc
    doesn't even offer P4; any script change needs a validation run.
- **Wrong against policy:**
  - "Kestrel turned on auto-apply for everything": ks_cancel auto-applies only schedule, caller_id, greeting
    and voice, at most 0.2 per step; scripts need approval.
  - "Northwind is fine with auto-apply on KYC": nw_kyc has auto-apply disabled.
- **Reply:**
  ```
  Nikhil, happy to make the summit story real. Per client we'll show lift on each KPI separately (pickup and
  engagement), because Product and the hard rules keep them apart and a blended score hides trade-offs. Two
  corrections: Kestrel's auto-apply covers caller ID, greeting, voice and timing, not scripts, and Northwind
  KYC has auto-apply off. We push winners as fast as the evidence allows: a "winner by lunch" is usually
  luck, and reversing a false winner costs more than a day. The persuasive script stays out: it raises
  made-up claims and KYC doesn't offer it. Real numbers from our runs: KYC pickup up 16% and Kestrel's
  completed calls up 57% with no harmful change; a bad script fixed in hours with approval.
  ```

### 02 Client Compliance, Northwind (Meera Iyer)
- **Accepted:**
  - Customer-facing changes (greeting, voice, script) on Northwind always go through the named approver.
    Their policy already allows auto-apply only for schedule and caller ID, and the gate follows the live policy.
  - Script changes carry a validation run ≥ 97%, with margin for AI ideas (H6).
  - "Slow and right": winners need 3 luck units, and every winner is checked group by group (language, age
    band, city tier, value band). A group it hurts keeps the old greeting (E12).
  - Requests are timed to approver hours (G5, G7); none were sent at night.
- **Pushed back:**
  - "Why a customer heard what they heard, within 24 h": the why-tool answers it for any config change from
    our permanent records, and each call event records its variant. But per-call raw events are kept 3 days
    by default (D6, storage). For IRDAI audits, `raw_event_days` must be set to their retention period
    (about 20 GB a month at full volume), or raw chunks archived.
- **Wrong:** "call timing and caller-ID rotation are covered by the policy we signed" holds for renewal only.
  nw_kyc has auto-apply **off**, so every KYC change, caller ID included, goes to her approver. Her approver
  hours match the policy.
- **Weekly summary:** accepted in substance. Every self-applied change is in the diary with its reason
  (`why --list`), but a formatted weekly email isn't built (part of the cut supervisor view, C11).
- **Reply:**
  ```
  Meera, thank you, these conditions are now enforced by the system, not by habit. On Northwind every
  greeting, voice and script change goes to your named approver. Only on renewal do call timing and
  caller-ID rotation use your policy's latitude; on KYC everything goes to your approver. Every change is
  logged with its reason. Script changes carry a validation run of at least 97%. Before a winner goes live
  we check it group by group; a group it would hurt keeps the old version. Requests go out only in your
  approvers' hours. Tell us your IRDAI retention period and we'll keep per-call records that long.
  ```

### 03 Platform & SRE (Kabir Menon)
- **Accepted:**
  - The kill/restart test (section 9).
  - Idempotency everywhere (event ids, our own proposal ids, apply-once).
  - No new stateful infrastructure: Postgres only.
  - The one-load-day-under-10-minutes target, measured (section 8).
  - Exit codes: 0 done, 2 fatal, 75 lost connection; `run.sh` restarts.
- **Partly:**
  - Loop health is one JSON line per step (events, ingest seconds, decisions written), not a dashboard.
  - Section 10 lists the alerts we'd page on; they aren't wired to a pager.
- **Wrong:** nothing.
- **Reply:**
  ```
  Kabir, your non-negotiables are covered and tested. scripts/crash_test.py SIGKILLs the controller 5 times
  mid-run (history load and live loop, with changes in flight) and checks every event is stored once, every
  change applied exactly once and no request is left half-sent: PASS. State is in Postgres only; no Redis or
  new stores. One sim day of public_03 takes about a minute on a laptop with 66 MB of memory; at 10x that is ~10 minutes, so we shard before then.
  What breaks first at 10x is the single loop and delete-based pruning; the fix is partitioning events by day
  and sharding workflows across workers. Loop health is a JSON line per step today; the page list is drafted.
  ```

### 04 Kestrel Operations Supervisor (Anita Desai)
- **Accepted:**
  - "Don't undo my changes": a supervisor edit is final. Our pending requests on that setting are dropped
    or superseded, we never re-test a greeting/voice she set (E14), and scripts she ships are only replaced
    by an urgent, approved fix whose note says "overrides a recent supervisor setting" (G20, H8).
  - "Don't flood me": one bundled morning note per workflow, urgent items separately, and one slot always
    kept free for emergencies (G5, G8). Peak in our runs: 1 request a day on ks_cancel.
  - "Nothing word for word": we never switch to the strict script (P5) on a test drive alone. Test drives
    measure safety, not whether people stay.
- **Pushed back:**
  - "Never before 11:00": her signed policy says 10:00. The morning note goes at the start of approver
    hours; moving it means changing the policy, not a side file (G9).
  - "Tell me before anything changes on a running campaign": the policy allows auto-apply on greeting and
    voice mid-flight. We apply in small hourly steps and log each with its reason. Notice in advance means
    asking for a policy change.
  - "One screen": not built; the why-tool answers "what changed and why" today.
- **Wrong:** "max 2 requests/day" is stricter than the policy (4). Not wrong, a preference we stay within.
- **Reply:**
  ```
  Anita, we never overwrite your edits behind your back: your change withdraws our pending requests for that
  setting, and we never re-test a greeting or voice you've set. One exception: a script that starts making
  things up goes to your approver, clearly labelled as overriding you. You'll get at most one bundled note a
  morning, plus one only for something urgent; in our tests that was one a day. Your policy starts approvals
  at 10:00; if 11:00 suits you better, we can change the policy. Greeting and voice changes on running
  campaigns are allowed by your policy; we make them in small, logged hourly steps. Want them to need your
  approval? That's one policy switch.
  ```

### 05 Product (Tara Kulkarni)
- **Accepted:**
  - KPIs kept separate everywhere.
  - Cohorts are the config's cohort objects, used by detection, experiments and carve-outs alike.
  - Every proposal carries impact score, diff, affected leads and dry run (section 6).
  - An expired proposal counts as a rejection and is rebuilt with fresh data (G18).
  - "Only what ran counts": analysis uses the variant recorded on each call event, so a test that was never
    approved contributes nothing.
  - Winners change only that workflow.
- **Answered:** minimum cohort size and window (section 5). 400 answered calls per option, a 3-luck-unit bar,
  judged daily; a test with no winner ends once it could have found an 8-point lift (about 1,150 calls per
  option).
  - **Cost:** about 2-4 days for an 8-10 point win.
  - **What we give up:** 5-point wins mostly go unproven within a 10-day run.
- **Declined:** two of her three P0s.
  - Per-person call-time (C4): cut.
  - The consolidated supervisor view (C11): cut, though the why-tool covers the "what changed and why" part.
  - Caller-ID health, her third, is built.
- **Wrong:** nothing.
- **Reply:**
  ```
  Tara, your definitions hold everywhere: separate KPIs, one cohort object (the config's cohorts, also used
  for group carve-outs), every proposal with impact, diff, affected leads and dry run, expired treated as
  rejected, and only calls that really ran count as evidence. Minimum size: 400 answered calls per option,
  3 luck units, judged once a day; it finds 8-10 point wins in 2-4 days and mostly can't prove 5-point wins
  within a 10-day run, by design, to keep false winners near 5%. Of your three P0s I built caller-ID health
  and cut per-person call time and the supervisor screen, to do fewer things properly. Happy to walk through it.
  ```

### 06 CTO (Aryamann)
- **Accepted, and how it shows:**
  - Explainable over clever: every decision has a diary line with its numbers, and winners are chosen by
    stated statistics.
  - A change that hurts a group is worse than nothing: the group harm guard, and harmful options are stopped
    within hours.
  - Credits only where they change a decision: 0 credits on experiments and caller ID; 12 on the bad-script
    incident (the fix's test drive, then the AI's ideas within a 15-credit cap and a 20% reserve).
  - Choosing is the job: section 2.
  - Docs for the on-call engineer: section 14, the journal, the why-tool.
- **Wrong claims found in other memos:** Sales (auto-apply scope on Kestrel and KYC) and Compliance (KYC
  has no auto-apply latitude). Ops' "max 2 a day, never before 11:00" isn't wrong, just stricter than her policy.
- **Reply:**
  ```
  Aryamann, I chose depth: crash-safe ingestion and a governance gate under every change, then caller-ID
  health, bad-script response and greeting/voice experiments with fading rollback, each reviewed cold three
  times and run end to end; I cut per-person call
  time, the supervisor screen, dashboards and cross-tenant fairness, and wrote down why. Every change is
  explainable from our own records with one command. Credits went only to the incident (12). The honest
  weak spots are in section 13: a variant can fade after our last look, small wins we can't prove in
  10 days, and a single loop that needs sharding at 10x.
  ```

## 4. Architecture

```
            simulator (HTTP)                                      Postgres (all state)
  ┌──────────────────────────────┐                  ┌──────────────────────────────────────────┐
  │ /events/chunks  /sim/advance │                  │ runs (bookmark)   events (raw, 3 d)      │
  │ /workflows (policy, config)  │                  │ leads (features)  call_stats  segment_stats│
  │ /approvals /apply PUT config │                  │ decisions (append-only diary)            │
  │ /llm/suggest-fixes /validation│                 │ changes  proposals  holds  feature_state │
  └──────┬───────────────▲───────┘                  └──────▲──────────────▲──────────────▲──────┘
         │ chunks (NDJSON)│ requests                       │ 1 txn/chunk  │ state+diary  │ 1 txn per
         ▼               │                                 │              │ in 1 txn     │ state change
  ┌─────────────┐   ┌────┴──────────────────────┐   ┌──────┴──────┐  ┌────┴─────────────────┴─────┐
  │ main loop   │──▶│ 1 ingest (stream → COPY)  │──▶│ store.py    │  │ features/ (check → Change) │
  │ every 15 min│   │ 2 features                │   │ raw + counts│  │  caller_id  script         │
  │ (sim time)  │   │ 3 gate.tick               │   │ + bookmark  │  │  experiments               │
  │             │   │ 4 advance                 │   └─────────────┘  └────────────┬───────────────┘
  └─────────────┘   └───────────────────────────┘                                 │ Change(intent, patch,
                                                                                   ▼ evidence, attempts)
                                                    ┌──────────────────────────────────────────────┐
                                                    │ governance.py (the gate)                      │
                                                    │ auto path: policy allows every dim → step by  │
                                                    │   max_shift, hourly                           │
                                                    │ approval path: morning note / urgent now,     │
                                                    │   bundling, slots, TTL, apply, stale/lapse    │
                                                    └──────────────────────────────────────────────┘
```

- **Loop order** (S12): ingest everything listed after the bookmark → features → gate → advance by one tick.
  We never advance with unread events. The advance re-reads the sim clock first, so it never double-advances.
- **Where state lives:** only Postgres.
  - Raw events: kept 3 days, then pruned daily.
  - Counters ("scoreboard"): per workflow × day × hour × cohort × variant, kept forever.
  - Per-group counters: kept forever.
  - The diary: append-only, enforced by a database trigger.
  - Requests, intended changes, supervisor holds and features' small memory (`feature_state`).
- **On restart** (`run.sh` restarts on any non-fatal exit):
  - Resume from the bookmark. Re-listing chunks after it is idempotent: event ids are a primary key, and
    counters only count newly inserted rows.
  - `reconcile()` resolves any request left `sending` (resend with the same id; "duplicate" means it
    arrived) or `applying` (ask the sim whether it applied).
  - Features re-read their state.
  - A restarted simulator is detected (its clock or event count is behind our bookmark), and a new run starts
    instead of mixing worlds.

## 5. The learning loop

**Cohorts.**
- The unit is the config's cohort, plus the hour of day.
- Every comparison is made **inside each (day, hour, cohort) stratum, then added up** (stratified
  two-proportion test, `stats.stratified_lift`). Arms that ran in different hours, during ramps and harm
  stops, can't win by running in "good hours" (Simpson's paradox; there's a test for it).
- We never fragment traffic into new cohorts to learn. The only cohorts we create are **carve-outs**:
  a customer group (one feature at a time, e.g. `lang=ta`) where a winner was clearly worse keeps the old
  option. At most 4 per dimension, within the sim's 20-cohort limit.
- Group minimum: 100 answered calls per option in the group, and harm at 2.5 luck units. That's more
  sensitive than the winner bar (3), because missing harm to a group is worse than a false carve-out.
  Across ~15 groups that's ~9% chance of one false carve-out per promotion. If more than 4 groups would need a
  carve-out, the "winner" is too uneven and isn't promoted.

**Experiment design.**
- **Allocation:** equal split across the options by default (W1/W2/W3 a third each). A per-client knob
  `control_share` (e.g. 0.5) keeps more calls on today's option (E11).
- **Two dimensions at once:** the sim draws greeting and voice independently for every call, so one
  factorial test answers both (E3), each judged on its margin over the other. Interactions aren't
  modelled; they're assumed small at this traffic.
- **Exploration vs exploitation:**
  - Explore with a fixed split.
  - Stop a clearly worse option at once.
  - When a test ends without a proven winner but one option is ahead by ≥ 2 luck units, drop the others and
    run that leader 50/50 against today's option until it proves itself or falls back (E9).
  - **No automatic re-tests.** In our runs re-tests found nothing and once put calls back on a faded
    greeting. Options come back only when a promoted winner fades.
- **Where experiments don't run:** pickup-primary workflows (assumption 3), and any setting a supervisor has
  edited (E14).

**Deciding a winner.**
- Frequentist, stratified two-proportion z-test. The stat is an absolute lift in the primary KPI, with its
  standard error.
- **Judged once a day at 09:00**, ready for the approver's morning (E2), never every 15 minutes (peeking).
- **Bar:** z ≥ 3, with 400+ answered calls on each side.
- **Multiple comparisons:**
  - About 5 challengers × ~8 daily looks gives ~40 chances.
  - A Bonferroni-style budget of 5% across those needs about 1 in 800 per look, which is z ≈ 3.
  - **Expected false-winner rate: about 5% per workflow-run.**
- **Stopping rule:** a test without a winner ends once each option has the calls needed to find an 8-point
  lift 80% of the time (`stats.calls_needed`, ~1,150 at a 50% base). A narrowed test (leader vs today's
  option) runs on until it could find a 4-point lift.
- **Harm stops and fade checks *are* looked at every step** (that is peeking, on purpose). A false harm stop
  only costs one option's exploration, and a false rollback returns calls to the option we already trust, so
  we accept a few percent false-stop rate per equal option over a test, at the same z of 3, for stopping real
  harm within hours.

| Real lift | 3-way greeting test (Kestrel, ~390 answered/option/day) | 4-way voice test (~295/day) |
|---|---|---|
| 5 points | 7.5 days | 10 days |
| 8 points | 2.9 days | 3.9 days |
| 10 points | 1.9 days | 2.5 days |
| 15 points | 0.8 days | 1.1 days |

**Promoting a winner safely (E15-E17).** A variant can win on a test's early days and already be fading (a
novelty effect), so a winner has to pass three guards, all on the same bar our fading check uses:
- **Still holding its lead (E15):** over the last 24 h it must keep at least half its claimed lead (with
  enough calls; a quiet day proves nothing). A leader that may be fading gets no more calls that day: it is
  not promoted, and a no-winner test is not narrowed onto it.
- **Two steps (E16):** step 1 gives the winner only its current share + 10 points (a canary, e.g. 33% -> 43%);
  the next morning it goes to 80% only if it still holds half its lead on the step-1 split, otherwise it is
  rolled back having reached few extra calls. A narrowed 50/50 test already was step 1.
- **Fresh evidence after a "no" (E17):** if the approver turns a promotion down, the test goes on and the
  winner must clear the winner bar again on calls made after that decision. Re-sending evidence the approver
  already refused, days later, is exactly how a fading greeting went live in earlier runs.
- **Waiting promotions are re-checked every morning** and withdrawn if the winner may be fading; the gate
  never applies a routine request that no longer carries only wanted targets.

In our runs these took public_02 from 3 harmful greeting changes to 0; the price is that a real winner
reaches 80% about a day later.

**Decay and rollback.**
- A promoted winner gets 80%; **20% stays on the previous option** (E13). That share is the comparison group.
- Every step we compare the winner against it over the last 48 h, stratified.
- **Rollback fires** if the winner's lead has fallen ≥ 3 luck units below its lead at promotion **and** to
  under half of it, or if the winner is now clearly worse (z ≤ -3).
- Rollback goes out as an **urgent** change under its own intent, so it never queues behind a routine
  request. The faded option is retired for good, and the remaining options are tested again.
- Comparing against a group running at the same time means a bad day for everyone (a bad script) can't
  look like fading.
- **The graded decaying variant is greeting W3 on renewal** in every scenario. public_01: promoted, fade
  caught, moved off (its share falls from 80% to 33% on day 6). public_02: W3 won on D2, the approver said no,
  and on the new calls it never cleared the bar again (+5.4 then +6.9 points, 1.4 and 2.5 luck units), so it
  stayed at its test share and was never promoted (E17).
- Detection after promotion takes about a day of calls on the 20% holdout.

**Call time and caller ID.**
- **Call time:** cut (C4).
- **Caller ID:**
  - **History:** each number's own past 14 days is the reference when a workflow has only one number.
  - **Alarm:** a number answered ≥ 30% less than the others over 3/6/12/24 h windows (smallest with
    ≥ 200 calls each side) **and** 4 luck units.
  - **Removal:** stepped by `max_shift` where auto-apply is allowed, otherwise urgent to the approver.
  - **Second chance:** a 5% trial after 24 h (doubling on each failure), **only when fewer than 2 healthy
    numbers are left** (L9): with healthy numbers carrying the calls, re-trying a dead one only risks harm. It
    passes only when 2.5 luck units clear of the 30% line, because "not proven bad" isn't "good".
  - **Load spreading (L1-L8):** a number that carries every call answers less and less over days (Kestrel D1
    went from 24.7% to ~18% over 9 days in an early run) and a rested or fresh one answers better. So calls are
    spread **evenly over all healthy numbers**. A never-used spare first gets a **10% trial** (shared between
    spares), judged with the same trial rules, then an equal share; a failed spare is left out for good. A
    number someone else removed (it has calls in our records) is never treated as a spare, and a supervisor
    caller-ID edit switches spreading off for that workflow.
  - **Scope:** per workflow and per cohort split, not per person.

**Guardrail KPI.** Before any promotion, the winner's guardrail KPI is compared with today's option the same
way (stratified). It's blocked if the drop is larger than the policy's `max_drop` **and** clearly real
(z ≤ -2). Made-up claims must not be clearly up either.
- **Real example:** on public_02, Kestrel voice V3 beat V1 on engagement on D3, but its pickup was 21.8% vs
  23.8%. It was **not promoted** that day (diary: "fails a guardrail"). By assumption 3 a voice can't really
  change pickup, so this was most likely noise, and later data cleared it: V3 was promoted on D6.
- A winner that fails a guardrail isn't made the leader of a narrowed test on the day it fails. It can lead
  later if the guardrail clears, as V3 did. A narrowed leader that then fails a guardrail ends the test.

## 6. Governance

**State machine** (in `governance.py`'s docstring; every arrow is a diary line committed with the state):

```
changes:   queued ─▶ in_flight ─▶ applied
              │          ├──────▶ rejected | refused   (count against that target; 1 retry after 24 h,
              │          │                                2 strikes = given up, G17)
              │          ├──────▶ expired   (not a strike: rebuilt with fresh numbers, Product's rule)
              │          └──────▶ queued   (stale base / lapsed / superseded-but-unrelated: rebuilt fresh,
              │                             waits one step for the feature to refresh it; or one step of a
              │                             stepped change applied, next step after step_gap)
              └─▶ dropped (no longer wanted, supervisor took the setting, unsendable, no-op)

proposals: sending ─▶ pending ─▶ approved ─▶ applying ─▶ applied
           sending ─▶ applied                              (auto path)
           any open ─▶ stale | lapsed | superseded | refused
           approved ─▶ withdrawn  (no longer wanted before we applied: never applied, N1)
           pending  ─▶ rejected | expired
```

- **Expired = rejected** (Product): the intent is freed and the feature rebuilds it with fresh numbers.
- **Lapsed** (applied too late): re-queued.
- **Stale** (config moved, often a supervisor edit): never applied, re-queued and rebuilt on the new base.
- **Withdrawn:** a feature that stops asking for a change withdraws it at once, all the way to "approved but
  not applied".

**Batching and timing.**
- **Normal changes:** bundled into **one morning note per workflow** in the first hour of approver hours
  (G5).
- **Urgent changes:** go out at once only if `now + that approver's average answer time` lands ≥ 30 min
  before their day ends. Otherwise they go first thing next morning, tagged `[URGENT]` (G7). The average
  starts at the policy's midpoint and switches to the approver's measured same-day average after 3 answers.
- **Request limits:** one daily slot is always kept for emergencies (G8). Only one request is in flight per
  workflow, except an urgent one may join (D13). Every request is checked locally against what the approver
  would reject (empty fields, prompt without a passing validation), because a rejected request still burns
  a slot (G4).
- **Results:** public_01 had 5 requests, 0 expired, 0 lapsed, 0 conflicts; public_02 had 4 requests, 0
  expired, 0 lapsed, 0 conflicts. (The naive example controller let 7 of 15 expire at night.)

**Auto-apply.**
- Used whenever every setting a change touches is in the policy's `allowed_dims`. The gate steps it to
  `max_shift` per hour, so it's never refused for being too big (T3). Never on a setting a supervisor just
  edited: an urgent change to such a setting goes to the approver instead.
- **Voluntarily not used:**
  - Any change that adds or removes our group cohorts (`cohorts` isn't auto-allowed anyway).
  - Re-testing a setting a supervisor owns (we don't do that at all).
  - Non-urgent changes while a request is out, since applying would make that request stale (D14).

**Impact score, affected leads, dry run (honesty).**
- **Affected leads:** distinct leads dialled in the last 24 h (on the affected number for caller ID), times
  the share of traffic moved.
- **Impact score:** expected gain as a share of the workflow's answered calls per day.
  - For a caller-ID removal: extra answers per day at the healthy numbers' rate.
  - For a promotion: lift × share moved.
  - For a harm stop or rollback: the loss avoided.
  - **A test start scores 0.0**, because its expected effect is unknown.
- **Dry run:** a replay on evidence we actually hold (the test's own calls, the flag-time window, test drives
  for scripts), with counts. It's not a model of the future, and the text says so.

**Supervisor edits mid-experiment.**
- At ingest, a supervisor `config_changed` event places a hold on each setting it changed.
- The gate drops queued changes and supersedes requests on that setting. The hold lasts until 400 answered
  calls **and** the next morning (G21); after that, other features may propose changes to that setting again
  (never silently: the request names the supervisor's setting it overrides), except greeting/voice, below.
- The experiment for that setting stops for good (E14): data so far is kept (it happened), and no test of
  that setting is ever proposed again.
- If their edit only touched one cohort and left our test split live in `default`, we end our split once and
  touch nothing of theirs (R-M5, R2-H2).
- In public_02 the supervisor's voice edit on nw_renewal (D5 15:00) stopped our voice test.
- Our first version re-tested after 24 h. It moved 75% of calls off the supervisor's good voice and was
  **graded harmful**, which is why the rule is now "never".

## 7. Incidents

**Detection thresholds and false positives.**

| Detector | Fires when | False alarms (expected / seen) |
|---|---|---|
| Caller ID | ≥ 30% fewer answers than the other numbers **and** ≥ 4 luck units, ≥ 200 calls each side | One check is a 1-in-30,000 lottery ticket; ~3,500 checks/day gives about one 4-unit fluke per 8-10 days, and the 30% lock makes it far rarer. **Seen: 0** on public_01 and on 12 load workflows |
| Script (just switched) | ≥ 100 answered calls since the switch, ≥ 2× the previous script's made-up rate, ≥ 4 luck units, ≥ 10 flagged calls | **Seen: 0** on clean scenarios. A 3-hour drift window gave one false alarm in development, so drift is now judged on a whole day |
| Script (unchanged, drift) | Same tests on the last 24 h (≥ 500 calls) vs its own previous 14 days | **0** |

**Picking a fix (C8).**
1. Go back to the most recent script **proven on real calls** (≥ 500 answered, ≤ 3% made up, never flagged)
   and test-drive it (300 conversations, must clear the approver's 97%).
2. In parallel, ask the AI once (3 credits) and test-drive its first 2 new ideas (a knob). An AI idea must
   clear 97% by 2 luck units. Ideas are **reported** in the request, not switched to, because a test drive
   says nothing about engagement.
3. Only with no proven script does the cleanest idea become the fix.

- **Budget:** ≤ 15 credits per incident; optional spending never dips into the last 20% of the starting
  credits.
- **Paying safely:** the cost is recorded before every paid call, and paid calls are never auto-retried.

**Verify.** After 180 min **and** ≥ 500 answered calls, the made-up rate is compared with normal (before
the alarm), not just with the spike. The verdict is verified, not back to normal, or not improved, plus an
engagement check.

**public_02 timeline** (final run, from `python -m controller.why ks_cancel "D3 15:45"`, `reports/why_examples.md`):

| Sim time | What |
|---|---|
| D0 00:00 | Spreading: never-used spare D3 starts a trial; it passes at D0 14:00 (33.7% answered vs 26.0%) and joins an equal split |
| D2 11:00 | Caller ID D2 answers 13.2% vs 25.8% on D1, D3 (273 calls, 4.1 luck units): flagged, removed in auto steps, off at 12:15, verified 15:15 (pickup 25.9% vs 21.5% before) |
| D3 11:30 | **Supervisor ships script P2** (bypasses governance). Hold placed |
| D3 14:15 | P2 makes things up on 4.1% of calls (14/343) vs 1.4% for P1 (2.9x, 4.0 luck units). Test drive of P1: 98.7% clean. AI asked; P5 and P3 test-driven (99.3% each), reported only. **Urgent request sent**, "overrides a recent supervisor setting" |
| D3 15:21 | Client approver approves (66 min) |
| D3 15:45 | Applied |
| D4 10:30 | Verified: made-up 1.2% on 517 calls (incident 4.1%, normal 1.4%) |

The grader reports **MTTR 255 min, fixed by controller_approved, 12 credits**; Kestrel's made-up calls fell
from 348 (baseline) to 76. Time to fix varies by run (120-255 min across our runs): the alarm needs 4 luck
units, and how fast a bad script's made-up calls pile up is luck (8.3% in its first hour in one run, 4.1% in
this one), as is the approver's answer time.

## 8. Scale

Measured on `public_03_load` (12 workflows, ~650k dials/day, 7 history days + 5 days, 9.7M events), on a
laptop (Apple Silicon, Docker Postgres), with the **final code** (all features live):

| Measure | Result |
|---|---|
| Whole run (history + 5 days), wall clock | **557 s**, exit 0, no errors |
| History ingest (5.54M events) | 225 s, **~24,600 events/s** (including per-group counting) |
| Live ingest | 4.2M events in 138 s of ingest time, **~30,400 events/s** |
| One 15-min loop step (ingest ~13,500 events + all features + gate, 12 workflows) | median **0.73 s**, p95 1.6 s, max 8.2 s (the 08:00 daily batch upload) |
| One simulated day | **58-76 s** of wall clock (SRE target: under 10 minutes), about 1,300x faster than real time |
| Compute lag | one tick (15 sim min) + under 1 s of compute: events are acted on at the next step |
| Peak memory (controller process) | **66 MB** (chunks are streamed, never loaded whole) |
| Daily prune of raw events | ~840k rows in 3.2-4.4 s |
| Postgres size at the end | 6.1 GB (raw events for 3 days + space freed by deletes, reused but not returned) |
| Full numbers | `reports/load_test_03.md` |
| Grade (`reports/public_03.md`) | **+18.7% conclusion rate (ALL), +12.7% pickup, +3.2% engagement; 88 changes: 38 beneficial, 50 neutral, 0 harmful**; 25 approval requests, 0 expired, 0 lapsed, 0 conflicts; 0 credits |

The 12 load workflows have no incidents (their header says they mirror public_01's truth), so the lift is
phone-number spreading (all 12) and greeting/voice experiments (the 8 engagement workflows); the 4 KYC
workflows correctly ran no experiments (pickup primary).

**What breaks first at 10× (6.5M dials/day, ~8.5M events/day, 120 workflows):**
1. **Delete-based pruning and table bloat.** Deleting ~8M raw rows a day leaves space Postgres reuses but
   doesn't return. Fix: partition `events` by day and drop whole partitions (instant, space returned).
2. **One loop does every workflow in turn.** Features cost ~30 ms per workflow per step, so 120 workflows
   still fit easily in a 15-minute real-time step. What doesn't scale is one process owning every tenant (one
   failure domain, one ingest stream). Fix: shard workflows across workers by tenant (hash), each holding its
   own advisory lock, all writing to the same Postgres. Counters are keyed by workflow, so there's no
   cross-shard contention.
3. **Per-group counters** add ~4 rows per call before aggregation. Fine at 10×; at 100× they'd move to
   a columnar store or be sampled.
4. Raw chunk downloads are streamed with bounded memory, so memory doesn't grow with volume.

**End-to-end decision lag** (from the problem starting to the fix being live, public_02):
- Dead caller ID: the grader dates it D2 10:00; flagged D2 11:00 (needs ≥ 200 calls a side), off by D2 12:15.
- Bad script: shipped D3 11:30; flagged D3 14:15 (needs 4 luck units, ≥ 100 answered calls on it); approved
  15:21; live 15:45.
- Experiments: a winner is judged once a day at 09:00; then auto-applied in hourly steps, or approved
  (30-240 min) on approval workflows.

**The SRE target at 10×:** each sim day here spends ~26 s ingesting and ~35 s on features/gate (~30 ms per
workflow per step). Linearly, 10× is **~10 minutes per sim day**: right at the 10-minute target, and measured
on a laptop, not 4 vCPU / 8 GB. So at 10× the loop must be sharded (point 2 below) or features made
incremental; one process won't hold the target with margin.

Real time is not the constraint: 8.5M events/day is ~100 events/s on average, and ingest runs at tens of
thousands per second.

## 9. Reliability

**Crash test** (`scripts/crash_test.py`, log in `reports/crash_test.log`):
- **Method:** public_02 with every feature live, the controller making changes throughout the run. SIGKILL it
  5 times: once during history load, then at random sim minutes in the live loop. Restart each time. Raw-event
  retention is set to 3,650 days for this test only, because it checks exactly-once storage against the raw events.
- **Checks:**
  - Every event saved exactly once (324,315 = 324,315, no gaps, no duplicates).
  - Counters equal the raw events.
  - One diary row per config change.
  - Every one of our 26 config changes reached the sim and was **applied exactly once**.
  - The sim's applied list equals ours.
  - Nothing left half-sent or half-applied; nothing stuck; nothing refused.
- **Result:** **PASS**.
- **State lost:** none by design. Two documented windows:
  - A crash between the sim charging a paid call and our commit means paying again on restart. We record
    the cost first, so the total stays counted.
  - A diary line about something not yet committed never exists, by design.

**Idempotency keys:**
- `(run_id, event_id)` for events, and counters count only rows actually inserted.
- Our own `proposal_id = {run token}-{workflow}-{n}`. The token comes from the world's state, so the same
  world gives the same ids and the approver's random draws are reproducible.
- `decision_key` for every diary line.
- One active change per intent, enforced by a partial unique index.
- Mark `applying` before apply, and reconcile by our id at startup.

**When the simulator errors:**

| Response | Behaviour |
|---|---|
| GET 5xx / timeout | Retry with backoff |
| `advance` | Never blindly retried: re-read the clock and advance only the remainder |
| Paid POST (LLM, validation) | Never retried by the HTTP client. If the answer is lost, the cost stays counted. If the sim refuses (402 or another error), it's diaried, not charged, and retried once on the next step, then given up for that incident |
| 409 `duplicate_proposal_id` | It arrived before a crash: adopt it |
| 409 `stale_base_version` | Re-queue and rebuild on the new config |
| 410 lapsed | Re-queue |
| 403 / 422 refused | Recorded as `refused`, counted against that target like a rejection (no resend loop) |
| Our own budget check (before paying) | "no credits" left within the incident cap/reserve: nothing is sent, the incident note says so |
| Lost connection | Exit 75; `run.sh` restarts and resumes |
| Bad flag / old schema | Exit 2, no restart loop |

## 10. Audit & observability

**"Why did ks_cancel change at 15:45 on D3, who approved it, what happened next?"** (final public_02 run)

```
$ python -m controller.why ks_cancel "D3 15:45"
== ks_cancel config v16 at D3 15:45: prompt_version changed by controller_approved
WHAT: [URGENT] Switch script back from P2 to proven P1: P2 makes things up on 4.1% of calls vs 1.4% for P1
      over 16398 real calls (overrides a recent supervisor setting: prompt_version)
WHO APPROVED: the client approver, approval apr_00004: asked D3 14:15 as URGENT, decided D3 15:21 (66 min
      later); we applied it D3 15:45
  sent with: 3494 affected leads, impact score 0.65, validation run val_00001
WHY: The script being replaced was set by a supervisor at D3 11:30. P2: 14/343 answered calls flagged ...
  wanted by script_health since D3 14:15 (idea script_health:ks_cancel)
    D3 14:15  [incident] script P2 makes things up on 4.1% of answered calls (14/343, since P2 went live) ...
    D3 14:15  [test_drive] test drive of P1: 98.7% clean on 300 conversations (3 credits) ...
    ... (AI ideas P5, P3 and their test drives, the chosen target)
WHAT HAPPENED NEXT (24 h):
  D3 16:00  [script_health/fixed] script P1 is live in place of P2 ...
  D4 10:00  [supervisor_hold/released] prompt_version: 1011 answered calls since the supervisor's change ...
  D4 10:30  [script_health/verified] after switching to P1: made-up claims 1.2% on 517 answered calls; 4.1%
            during the incident, 1.4% normally ...
  ... (experiment lines and config versions)
KPIs 24 h before: 4800 calls, pickup 26.5%, engagement 48.0%, made-up 2.4%
KPIs 24 h after:  4800 calls, pickup 25.6%, engagement 49.3%, made-up 1.6%  (other changes in that window also count)
```

More examples, including a supervisor edit and an auto-applied caller-ID step, are in
`reports/why_examples.md` (exact output); `--list` shows every config change of a workflow. Everything comes from our
own append-only diary (`decisions`, a database trigger blocks UPDATE/DELETE), requests and changes, never from
the simulator. Without the tool, the diary alone gives the timeline (from the supervisor's edit to 24 h after our fix):
`SELECT sim_ts, kind, action, reason FROM decisions WHERE workflow_id = 'ks_cancel' AND sim_minute BETWEEN
5010 AND 6825 ORDER BY decision_id;` (the tool also reads our `proposals` and `changes` tables).

**Would page (actionable, with what to look at):**
- Controller exited non-zero more than 3 times in 30 min (`run.sh` log; the last JSON line says where).
- Ingest lag > 2 ticks (the `step` line's `sim_minute` vs the sim clock: sim down or database slow).
- A request stuck in `sending`/`applying` past one step (reconcile failed: compare `proposals` with
  `GET /approvals`).
- Credits below the 20% reserve (an incident may be unable to test a fix).
- A safety feature (caller ID, script) failing on the same workflow for an hour (`feature_error` diary
  lines; the controller log has the stack trace). The loop carries on, but that workflow is unprotected.

**Would not page** (morning report): an urgent change waiting for a client approver (not on-call's to fix);
a one-off feature error (logged and diaried, the loop continues);
rejections; tests ending without a winner; pruning; daily summaries.

## 11. Results

**public_01** (`reports/public_01.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.369 | +8.0% | 0.257 | +5.0% | 0.503 | +2.2% | 2% | 53 (57) |
| nw_kyc | 0.430 | +17.4% | 0.281 | +16.2% | 0.596 | -0.2% | 0% | 32 (21) |
| nw_renewal | 0.461 | +10.8% | 0.282 | +6.8% | 0.557 | +2.8% | 19% | 43 (54) |
| ALL | 0.413 | +11.0% | 0.270 | +7.8% | 0.541 | +2.1% | 8% | 128 (132) |

26 applied: 8 beneficial, 18 neutral, **0 harmful**. 0 credits. 8 approval requests, all approved, 0 expired.

**public_02** (`reports/public_02.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.371 | +57.5% | 0.253 | +39.6% | 0.503 | +7.5% | 34% | 76 (348) |
| nw_kyc | 0.431 | +15.3% | 0.273 | +16.4% | 0.598 | -1.8% | 0% | 34 (29) |
| nw_renewal | 0.478 | +10.3% | 0.288 | +9.8% | 0.552 | -0.4% | 11% | 47 (54) |
| ALL | 0.419 | +26.8% | 0.269 | +22.5% | 0.540 | +1.3% | 23% | 157 (431) |

26 applied: 10 beneficial, 16 neutral, **0 harmful**. Incident MTTR 255 min. 12 credits. 7 approval
requests (6 approved, 1 judgement rejection), 0 expired, 0 lapsed, 0 conflicts.

**Interpretation.**
- **Where the lift comes from:** mostly **phone-number load spreading**. KYC used one number for all 14
  history days; trying the never-used spares and spreading calls lifted its pickup +16% in both scenarios.
  On Kestrel, spreading plus switching off the dead number lifted pickup +40% in public_02. Greeting/voice
  experiments add engagement (renewal +2.8%, Kestrel +2.2% to +7.5%).
- **How we got to 0 harmful** (earlier versions were not): no second chance for a dead number while healthy
  ones carry the calls (L9), a 10% spare trial (a bad spare at 20% was graded harmful on the load test), and
  the promotion guards in section 5 (a fading greeting promoted on stale evidence was the last harmful change).
  0 harmful is these runs' outcome, not a guarantee: each run is a different shuffle of the sim's luck.
- **nw_kyc's engagement** moved -1.8% and -0.2% while we never touch its greeting/voice: run-to-run noise
  (about ±2%), so we don't read anything that small as a result.
- **Made-up calls:** 348 → 76 on Kestrel in public_02 (the bad script). KYC's count rises with its call
  volume (more answered calls, same rate).

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| SQLite | Simpler; enough throughput | SRE runs Postgres already; node recycling loses local disk; one writer blocks sharding at 10× (D5) |
| Keep every raw event | Full per-call audit | ~20 GB/month at volume; decisions need counters. Raw kept 3 days (configurable), counters forever (D6) |
| Multi-armed bandit (Thompson) | Less regret during tests | Harder to explain and audit, and Product asked for minimum sizes and multiple-comparison control. Fixed split + daily z-test + harm stop + narrowing to the leader |
| Judge winners every 15 min | Faster wins | Peeking inflates false winners; daily at 09:00 instead |
| Per-group winners | More lift where groups differ | Groups are small (Tamil ~48 calls/day per voice): ~7 days even for big effects. Group **harm guard** instead (E12) |
| Compare a winner with its own past to detect fading | Free, no holdout | A bad day for everyone (bad script) looks like fading. 20% holdout instead (E13) |
| Re-test a supervisor's setting after 24 h | Might find something better | Graded harmful in public_02: moved 75% of calls off a good voice. Never (E14) |
| Re-test after "no winner" | Effects change over time | Found nothing in our runs; once put calls back on a faded option. Narrow to the leader instead (E9) |
| Live trial of AI script ideas | Measures engagement, not just safety | A 1-day trial can't prove a 1.5-point gain (~20k calls) and exposes customers to unknown scripts. Report ideas; switch only to proven scripts (H5) |
| Random proposal ids | Uniqueness across DB resets | The approver's decisions are drawn from the id: runs became unrepeatable and tests flaky. Ids derived from world state (D19) |
| Per-client preference file (e.g. "no requests before 11:00") | Honour supervisor wishes | Rots, needs an engineer per change. The policy from the API is the contract (G9) |
| One change per workflow for everything | Simple; carve-outs share the cohort list | A safety move queued behind a routine request for hours. Safety moves get their own intent (R-H2) |
| Spares only when nothing healthy is left | Fewer changes, no risk from untried numbers | Numbers wear out under full load (seen on Day 2, ignored at first to avoid tuning): spreading with a small spare trial was the biggest single gain (L1-L8) |
| Promote straight to 80% on the whole test's evidence | Fastest gains | A fading variant got promoted on stale evidence three different ways; guarded two-step promotion instead (E15-E17), a day slower |
| Smaller and smaller promotion steps to avoid harm | Limits damage | Any move toward an already-fading variant counts as harmful; the fix had to be about evidence (E17), not step size |

## 13. Known weaknesses

1. **A variant can fade after our last look.** If a winner is still clearly ahead the morning its promotion
   goes live and fades right after, step 1 (a 10-point canary) still counts as a harmful change until the
   day-2 check rolls it back. Data can't show a fade before it starts.
2. **Small wins (~5 points) mostly go unproven within a 10-day run.** That's the deliberate price of a ~5%
   false-winner rate.
3. **A rollback that needs approval can be turned down** (10% judgement rejections). We re-ask a day later.
   One refusal cost a day on W3 in public_01.
4. **Two dimensions in one routine request share rejection counts.** The approver gives no per-item reason,
   so a "no" counts against both targets (safety moves are separate).
5. **Overlapping groups:** a lead in two carved-out groups follows the first cohort listed, so it's
   protected only for that cohort's dimension.
6. **Greeting × voice interactions aren't modelled.** Each dimension is judged on its margin over the other.
7. **Raw per-call events are kept 3 days by default** (`raw_event_days`), and lead features 14 days (a
   constant, `LEAD_KEEP_DAYS` in `store.py`). Compliance-grade per-customer answers older than that need both
   raised, or raw chunks archived.
8. **Credits have a per-incident cap but no cross-workflow cap** (C15 cut).
9. **A request that goes stale late in the morning window** now waits one step, so it can slip a day
   (R3-L2), traded for never re-sending outdated numbers.
10. **Impact scores are measured, not modelled.** A test start scores 0 because its effect is unknown, so it
    ranks below fixes in a supervisor's queue (intended, but worth knowing).
11. **The single loop is one failure domain** for all tenants until it's sharded (section 8).
12. **When the main script drifts and no other script is proven, the fallback is an AI idea.** In one
    public_01 run Kestrel's made-up rate doubled from D5 across every variant; the detector rightly flagged
    it, and the cleanest idea was P4 ("persuasive"). Graded beneficial (engagement +5 points) but no safer
    (3.1% vs 2.8%), and Compliance would question it. A human should choose in that case.
13. **Bad-script time to fix varies (120-255 min across runs):** the alarm waits for 4 luck units, which
    takes longer when the bad script's made-up calls pile up slowly.
14. **Spreading is per cohort split.** A cohort with its own caller-ID split spreads over its own numbers plus
    trial spares, and "never used" can't see numbers a supervisor removed before the 14-day history.

## 14. If this went to production at Formi next month

In order, first two weeks:
1. **Shadow mode on one real tenant** (features run and write requests to the diary, nothing is sent) for
   3-5 days. Compare our alarms and proposed changes with what supervisors actually did.
2. **Partition `events` by day, set `raw_event_days` per client's retention** (IRDAI for Northwind), and add
   the backup/restore runbook for the controller schema.
3. **Wire the five pages in section 10** to the on-call system, each with its runbook line ("look at X").
   Export the per-step JSON line as metrics: ingest lag, decisions/hour, credit burn.
4. **Turn on caller-ID health and bad-script response for real** (highest value, lowest risk, already
   approval-gated where the policy says so), with supervisors told what the urgent notes look like.
5. **Validate load spreading in shadow mode** against real number data (how fast numbers actually wear
   and recover), and tune the trial share per client.
6. **Experiments on one workflow per tenant**, with `control_share: 0.5` at first. Review every promotion
   and rollback with the client for the first two weeks.
7. **Shard by tenant** once a second tenant is live: one worker per shard, the advisory lock per shard.
8. **A thin read API** over the diary/changes/feature_state for the supervisor view (C11), starting with
   "what's being tested, what's winning, what we want from you, what changed today".
