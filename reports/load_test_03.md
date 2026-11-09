# Load test: public_03_load (final code, all features live)

Scenario: 12 workflows (4 tenants x renewal / KYC / cancellation), ~650k dials/day, 7 history days + 5 live
days, 9.74M events. Run on a laptop (Apple Silicon, Docker Desktop Postgres 16), controller started directly
with `/usr/bin/time -l python -m controller.main --url ... --dsn .../formi_load`. Source: the controller's
per-step JSON log (`runs/load03_ctl.jsonl`, not committed: 0.5 MB of run output) and `reports/public_03.md`.

| Measure | Result |
|---|---|
| Whole run (history + 5 days), wall clock | 557 s, exit 0 |
| History ingest | 5,537,531 events in 225 s (~24,600 events/s) |
| Live ingest | 4,200,173 events in 138 s of ingest time (~30,400 events/s) |
| Loop step (ingest + all features + gate, 12 workflows), 481 steps | median 0.73 s, p95 1.6 s, max 8.2 s (the 08:00 daily upload) |
| Ingest per step | median 0.44 s for ~13,500 events |
| Wall clock per simulated day | D0 75.7 s, D1 65.8 s, D2 59.8 s, D3 57.7 s, D4 59.7 s (ingest ~26-31 s of each) |
| Non-ingest work per step | ~0.35 s for 12 workflows (~30 ms per workflow) |
| Peak memory (controller process) | 69,386,240 bytes (66 MiB) |
| Daily prune of raw call/lead events | 840,000 rows in 3.2-4.4 s (3,017,447 after the history load in 5.6 s) |
| Postgres database size at the end | 6.1 GB (3 days of raw events + space freed by deletes, reused but not returned to the OS) |

**Extrapolation to 10x (SRE target: one sim day in under 10 minutes on 4 vCPU / 8 GB):** linearly ~10 min
per sim day (ingest ~4.4 min + features ~6 min) on this laptop: right at the target, so at 10x the work
must be sharded across workers (by tenant) or features made incremental. Not measured on 4 vCPU / 8 GB.

**Grade (`reports/public_03.md`):** ALL conclusion rate +18.7%, pickup +12.7%, engagement +3.2%; 88 changes
applied: 38 beneficial, 50 neutral, **0 harmful**; 25 approval requests, 0 expired, 0 lapsed, 0 conflicts; 0 credits.
- Most of the lift is phone-number spreading on all 12 workflows (never-used spares tried at 10%, then an
  equal split); the 8 engagement workflows also ran greeting/voice experiments with guarded promotion.
- KYC workflows (pickup primary): no greeting/voice experiments, by design.
