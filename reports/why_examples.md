# "Why did it change?" examples (scenario 02, from the controller's own records)

Command: `python -m controller.why <workflow> "<Dn HH:MM>"` prints the config change nearest that time (and any others at the same minute); `--list` prints every change of a workflow. Each block below is the exact output of the command in its heading (plus `--dsn` for this run's database).

## python -m controller.why ks_cancel "D3 11:30"

```
== ks_cancel config v14 at D3 11:30: prompt_version changed by supervisor
WHO: the client's supervisor, directly (supervisor edits bypass governance; not our change).
OUR REACTION:
  D3 11:45  supervisor changed prompt_version: leaving it alone until 400 answered calls and the next morning; dropped 0 queued change(s), superseded 0 request(s)
  D4 10:00  prompt_version: 1011 answered calls since the supervisor's change; open to proposals again
WHAT HAPPENED NEXT (24 h):
  D3 12:00  [config_observed/recorded] config v15 applied by controller_auto (dims: welcome_message)
  D3 14:15  [script_health/incident] script P2 makes things up on 4.1% of answered calls (14/343, since P2 went live) vs 1.4% on script P1 before the switch at D3 11:30: 2.9x, 4.0 luck units (we act at 2.0x and 4.0). Engagement 42.3% vs 50.3%.
  D3 14:15  [script_health/test_drive] test drive of P1: 98.7% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
  D3 14:15  [script_health/ai_ideas] AI suggested: P5 (Strict: near-verbatim compliance script), P1 (Current production prompt), P3 (Grounded: benefit claims restricted to policy-schedule fields; tighter objection handling) (3 credits)
  D3 14:15  [script_health/test_drive] test drive of P5: 99.3% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
  D3 14:15  [script_health/test_drive] test drive of P3: 99.3% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
  D3 14:15  [script_health/target] fix for P2: switch to P1 (proven on real calls)
  D3 15:45  [config_observed/recorded] config v16 applied by controller_approved (dims: prompt_version)
  D3 16:00  [script_health/fixed] script P1 is live in place of P2; checking the effect once 180 min and 500 answered calls have passed
  D4 09:00  [experiments/end_test] End the voice test; back to V1 for everyone: no clear winner after 1152+ answered calls per option, enough to find a 8% lift (V2: 50.3% vs 48.6% on 1202 calls (+1.0 luck units); V3: 50.4% vs 48.6% on 1240 calls (+0.9 luck units); V4: 50.4% vs 48.6% on 1289 calls (+0.9 luck units))
  D4 09:00  [config_observed/recorded] config v17 applied by controller_auto (dims: voice)
  D4 10:00  [config_observed/recorded] config v18 applied by controller_auto (dims: voice)
  D4 10:30  [script_health/verified] after switching to P1: made-up claims 1.2% on 517 answered calls; 4.1% during the incident, 1.4% normally. Engagement 48.0% (incident 42.3%, normally 50.3%)
  D4 11:00  [config_observed/recorded] config v19 applied by controller_auto (dims: voice)
KPIs 24 h before: 4800 calls, pickup 25.4%, engagement 49.3%, made-up 1.4%
KPIs 24 h after:  4800 calls, pickup 25.2%, engagement 47.8%, made-up 2.6%  (other changes in that window also count)

```

## python -m controller.why ks_cancel "D3 15:45"

```
== ks_cancel config v16 at D3 15:45: prompt_version changed by controller_approved
WHAT: [URGENT] Switch script back from P2 to proven P1: P2 makes things up on 4.1% of calls vs 1.4% for P1 over 16398 real calls (overrides a recent supervisor setting: prompt_version)
WHO APPROVED: the client approver, approval apr_00004: asked D3 14:15 as URGENT, decided D3 15:21 (66 min later); we applied it D3 15:45
  sent with: 3494 affected leads, impact score 0.65, validation run val_00001
WHY: The script being replaced was set by a supervisor at D3 11:30. P2: 14/343 answered calls flagged (since P2 went live) vs 234/16398 on script P1 before the switch at D3 11:30: 2.9x, 4.0 luck units. Engagement 42.3% vs 50.3%. Test drive of P1: 98.7% clean on 300 (val_00001). Other options checked (test drives measure made-up claims, not engagement, so not switched to): P5 99.3% clean; P3 99.3% clean.
  wanted by script_health since D3 14:15 (idea script_health:ks_cancel)
    D3 14:15  [incident] script P2 makes things up on 4.1% of answered calls (14/343, since P2 went live) vs 1.4% on script P1 before the switch at D3 11:30: 2.9x, 4.0 luck units (we act at 2.0x and 4.0). Engagement 42.3% vs 50.3%.
    D3 14:15  [test_drive] test drive of P1: 98.7% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
    D3 14:15  [ai_ideas] AI suggested: P5 (Strict: near-verbatim compliance script), P1 (Current production prompt), P3 (Grounded: benefit claims restricted to policy-schedule fields; tighter objection handling) (3 credits)
    D3 14:15  [test_drive] test drive of P5: 99.3% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
    D3 14:15  [test_drive] test drive of P3: 99.3% clean on 300 conversations (3 credits); approver's bar 97%, cleared with margin
    D3 14:15  [target] fix for P2: switch to P1 (proven on real calls)
WHAT HAPPENED NEXT (24 h):
  D3 16:00  [script_health/fixed] script P1 is live in place of P2; checking the effect once 180 min and 500 answered calls have passed
  D4 09:00  [experiments/end_test] End the voice test; back to V1 for everyone: no clear winner after 1152+ answered calls per option, enough to find a 8% lift (V2: 50.3% vs 48.6% on 1202 calls (+1.0 luck units); V3: 50.4% vs 48.6% on 1240 calls (+0.9 luck units); V4: 50.4% vs 48.6% on 1289 calls (+0.9 luck units))
  D4 09:00  [config_observed/recorded] config v17 applied by controller_auto (dims: voice)
  D4 10:00  [supervisor_hold/released] prompt_version: 1011 answered calls since the supervisor's change; open to proposals again
  D4 10:00  [config_observed/recorded] config v18 applied by controller_auto (dims: voice)
  D4 10:30  [script_health/verified] after switching to P1: made-up claims 1.2% on 517 answered calls; 4.1% during the incident, 1.4% normally. Engagement 48.0% (incident 42.3%, normally 50.3%)
  D4 11:00  [config_observed/recorded] config v19 applied by controller_auto (dims: voice)
  D4 12:00  [config_observed/recorded] config v20 applied by controller_auto (dims: voice)
KPIs 24 h before: 4800 calls, pickup 26.5%, engagement 48.0%, made-up 2.4%
KPIs 24 h after:  4800 calls, pickup 25.6%, engagement 49.3%, made-up 1.6%  (other changes in that window also count)

```

## python -m controller.why ks_cancel "D2 11:00"

```
== ks_cancel config v9 at D2 11:00: caller_id changed by controller_auto
WHAT: step 1 of 2: [URGENT] Stop calling from caller ID D2 (answered 13.2% vs 25.8% on D1, D3, flagged D2 11:00); calls go to D1, D3
WHO APPROVED: no human; auto-applied by the controller because the workflow's own policy allows it for caller_id (within max_shift)
WHY: D2 flagged D2 11:00: 36/273 answered over 3 h vs 136/527 on D1, D3: 49% worse, 4.1 luck units (we act at 30% and 4.0)
  wanted by caller_id_health since D2 11:00 (idea caller_id_health:ks_cancel)
    D2 11:00  [remove] caller ID D2 answers 13.2% vs 25.8% on D1, D3 over 273 calls (49% worse, 4.1 luck units; thresholds 30% and 4.0). Removing it.
WHAT HAPPENED NEXT (24 h):
  D2 12:00  [config_observed/recorded] config v10 applied by controller_auto (dims: caller_id)
  D2 12:15  [caller_id_health/removed] caller ID D2 now gets no calls; second chance after D3 12:15
  D2 15:15  [caller_id_health/verified] after removing caller ID D2: workflow pickup 25.9% on 900 calls, vs 21.5% in the 3 h before it was flagged (healthy numbers then: 25.8%)
  D3 09:00  [experiments/end_test] End the greeting test; back to W1 for everyone: no clear winner after 1152+ answered calls per option, enough to find a 8% lift (W2: 53.0% vs 49.0% on 1235 calls (+1.9 luck units); W3: 50.4% vs 49.0% on 1257 calls (+0.7 luck units))
  D3 09:00  [config_observed/recorded] config v11 applied by controller_auto (dims: welcome_message)
  D3 10:00  [config_observed/recorded] config v12 applied by controller_auto (dims: welcome_message)
KPIs 24 h before: 4800 calls, pickup 25.5%, engagement 51.6%, made-up 1.1%
KPIs 24 h after:  4800 calls, pickup 25.8%, engagement 49.0%, made-up 1.6%  (other changes in that window also count)

```

## python -m controller.why nw_renewal "D5 15:00"

```
== nw_renewal config v7 at D5 15:00: voice changed by supervisor
WHO: the client's supervisor, directly (supervisor edits bypass governance; not our change).
OUR REACTION:
  D5 15:15  a supervisor changed the voice at D5 15:00; their choice stands for good. We stop our voice work and never propose a voice test on this workflow again (E14)
  D5 15:15  supervisor changed voice: leaving it alone until 400 answered calls and the next morning; dropped 0 queued change(s), superseded 0 request(s)
  D6 10:00  voice: 563 answered calls since the supervisor's change; open to proposals again
WHAT HAPPENED NEXT (24 h):
  D5 15:15  [experiments/supervisor_owned] a supervisor changed the voice at D5 15:00; their choice stands for good. We stop our voice work and never propose a voice test on this workflow again (E14)
  D6 09:00  [experiments/end_test] End the greeting test; back to W1 for everyone: no clear winner after 1148+ answered calls per option, enough to find a 8% lift (W2: 53.2% vs 52.9% on 2029 calls (+0.1 luck units); W3: 57.0% vs 52.9% on 1972 calls (+2.7 luck units))
  D6 11:00  [config_observed/recorded] config v8 applied by controller_approved (dims: welcome_message)
KPIs 24 h before: 3600 calls, pickup 28.1%, engagement 50.0%, made-up 1.5%
KPIs 24 h after:  3600 calls, pickup 30.3%, engagement 55.0%, made-up 1.1%  (other changes in that window also count)

```

## python -m controller.why ks_cancel --list

```
D0 00:00  v2  controller_auto  caller_id
D0 00:15  v3  controller_auto  welcome_message, voice
D0 01:15  v4  controller_auto  welcome_message, voice
D0 02:15  v5  controller_auto  welcome_message, voice
D0 03:15  v6  controller_auto  welcome_message, voice
D0 14:00  v7  controller_auto  caller_id
D0 15:00  v8  controller_auto  caller_id
D2 11:00  v9  controller_auto  caller_id
D2 12:00  v10  controller_auto  caller_id
D3 09:00  v11  controller_auto  welcome_message
D3 10:00  v12  controller_auto  welcome_message
D3 11:00  v13  controller_auto  welcome_message
D3 11:30  v14  supervisor  prompt_version
D3 12:00  v15  controller_auto  welcome_message
D3 15:45  v16  controller_approved  prompt_version
D4 09:00  v17  controller_auto  voice
D4 10:00  v18  controller_auto  voice
D4 11:00  v19  controller_auto  voice
D4 12:00  v20  controller_auto  voice
```
