# Grade report: public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.236 | +0.0% | 0.181 | +0.0% | 0.468 | +0.0% | 0% | 348 (348) |
| nw_kyc | 0.374 | +0.0% | 0.234 | +0.0% | 0.608 | +0.0% | 0% | 29 (29) |
| nw_renewal | 0.433 | +0.0% | 0.262 | +0.0% | 0.555 | +0.0% | 5% | 54 (54) |
| ALL | 0.331 | +0.0% | 0.219 | +0.0% | 0.533 | +0.0% | 3% | 431 (431) |

## Changes applied by the controller

0 applied: 


## Governance

```
{
  "approval_requests": 0,
  "decisions": {
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 0,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {}
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.503,
      "3": 0.491,
      "4": 0.492,
      "5": 0.486,
      "6": 0.499,
      "7": 0.5,
      "8": 0.496,
      "9": 0.501
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants: share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0