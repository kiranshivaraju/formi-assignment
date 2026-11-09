"""Pure functions over workflow configs, written from docs/SIMULATOR.md section 3 and 4.2.

Used to check locally, before calling the sim, whether a change may be auto-applied (G3), and to
build the full proposed config a request carries.
"""
from __future__ import annotations

import copy
import math

ALLOC_DIMS = ("welcome_message", "voice", "caller_id")
ALL_DIMS = ("cohorts", *ALLOC_DIMS, "prompt_version", "schedule")


def apply_patch(config: dict, patch: dict) -> dict:
    """Return config with patch merged in. Nested dicts merge, except that an allocation
    distribution ({"V1": 0.5, "V2": 0.5}) and each schedule table are replaced whole, and lists
    (cohorts) are replaced whole. A None value removes the key.
    """
    out = copy.deepcopy(config)
    _merge(out, patch, path=())
    return normalise_schedule(out)


def normalise_schedule(config: dict) -> dict:
    """Hour keys as strings and weights as floats, as the sim stores them, so a patch written with
    int hours never looks like a change to us when the sim sees none."""
    def num(x, cast):
        try:
            return cast(x)
        except (TypeError, ValueError):
            return x  # leave malformed values alone; schedule_problems reports them

    sched = config.get("schedule")
    if isinstance(sched, dict):
        hw = sched.get("hour_weights") or {}
        sched["hour_weights"] = {c: {str(num(h, int)): num(w, float) for h, w in (t or {}).items()}
                                 for c, t in hw.items()}
        sched["lead_preferred_hour"] = {p: num(h, int) for p, h in (sched.get("lead_preferred_hour") or {}).items()}
    return config


def schedule_problems(patch: dict, window: list[int]) -> list[str]:
    """Shape checks for a schedule patch: whole hours inside the dialling window, weights >= 0."""
    sched = patch.get("schedule")
    if sched is None:
        return []
    if not isinstance(sched, dict):
        return ["schedule must be an object"]
    lo, hi = window
    problems = []

    def hour_ok(h) -> bool:  # an int, or a string of digits, as the sim accepts ("9", not "9.0")
        if isinstance(h, bool) or not (isinstance(h, int) or (isinstance(h, str) and h.isdigit())):
            return False
        return lo <= int(h) < hi

    for cohort, table in (sched.get("hour_weights") or {}).items():
        for h, w in (table or {}).items():
            if not hour_ok(h):
                problems.append(f"hour_weights.{cohort}: hour {h!r} not a whole hour in {window}")
            if not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
                problems.append(f"hour_weights.{cohort}: weight {w!r} for hour {h!r} must be a number >= 0")
    for person, h in (sched.get("lead_preferred_hour") or {}).items():
        if not hour_ok(h):
            problems.append(f"lead_preferred_hour.{person}: hour {h!r} not a whole hour in {window}")
    return problems


def _is_leaf(path: tuple) -> bool:
    if path and path[-1] in ALLOC_DIMS and len(path) == 3 and path[0] == "allocations":
        return True  # allocations.<cohort>.<dim>
    return len(path) == 3 and path[0] == "schedule"  # schedule.hour_weights.<cohort>


def _merge(dst: dict, patch: dict, path: tuple) -> None:
    for k, v in patch.items():
        p = (*path, k)
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and not _is_leaf(p) and isinstance(dst.get(k), dict):
            _merge(dst[k], v, p)
        else:
            dst[k] = copy.deepcopy(v)


def distribution(config: dict, cohort: str, dim: str) -> dict[str, float]:
    """The normalised distribution a cohort samples for dim; a cohort that omits dim uses default."""
    allocs = config.get("allocations", {})
    raw = (allocs.get(cohort) or {}).get(dim) or (allocs.get("default") or {}).get(dim) or {}
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items() if v} if total else {}


def _cohort_ids(config: dict) -> set[str]:
    return {"default", *(c["id"] for c in config.get("cohorts", [])), *config.get("allocations", {})}


def changed_dims(old: dict, new: dict) -> list[str]:
    dims = []
    if old.get("cohorts", []) != new.get("cohorts", []):
        dims.append("cohorts")
    cohorts = _cohort_ids(old) | _cohort_ids(new)
    for d in ALLOC_DIMS:
        if any(distribution(old, c, d) != distribution(new, c, d) for c in cohorts):
            dims.append(d)
    if old.get("prompt_version") != new.get("prompt_version"):
        dims.append("prompt_version")
    if normalise_schedule({"schedule": copy.deepcopy(old.get("schedule") or {})}) != \
            normalise_schedule({"schedule": copy.deepcopy(new.get("schedule") or {})}):
        dims.append("schedule")
    return dims


def tv_distance(p: dict[str, float], q: dict[str, float]) -> float:
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in set(p) | set(q))


def max_shift(old: dict, new: dict) -> float:
    """Largest total-variation move of any cohort's welcome/voice/caller_id distribution."""
    cohorts = _cohort_ids(old) | _cohort_ids(new)
    return max((tv_distance(distribution(old, c, d), distribution(new, c, d))
                for c in cohorts for d in ALLOC_DIMS), default=0.0)


def auto_apply_problems(policy: dict, old: dict, new: dict) -> list[str]:
    """Why the sim would refuse this as an auto-apply; empty list means it's allowed."""
    pol = policy.get("auto_apply") or {}
    dims = changed_dims(old, new)
    problems = []
    if not pol.get("enabled"):
        problems.append("auto-apply not enabled")
    bad = [d for d in dims if d not in pol.get("allowed_dims", [])]
    if bad:
        problems.append(f"dims not allowed for auto-apply: {bad}")
    shift = max_shift(old, new)
    if shift > pol.get("max_shift", 0) + 1e-9:
        problems.append(f"shift {shift:.3f} exceeds max_shift {pol.get('max_shift', 0)}")
    if not dims:
        problems.append("no change")
    return problems


def option_problems(options: dict, config: dict) -> list[str]:
    """Options used in config that the workflow doesn't offer."""
    problems = []
    for cohort, dims in (config.get("allocations") or {}).items():
        for d, dist in dims.items():
            bad = [o for o in dist if o not in options.get(d, [])]
            if bad:
                problems.append(f"allocations.{cohort}.{d}: unknown options {bad}")
    pv = config.get("prompt_version")
    if pv is not None and pv not in options.get("prompt_version", []):
        problems.append(f"unknown prompt_version {pv}")
    return problems


def patch_dims(patch: dict) -> list[str]:
    """Which settings a patch touches, from its shape alone (used before a base config exists)."""
    dims = set()
    if "cohorts" in patch:
        dims.add("cohorts")
    for cohort_dims in (patch.get("allocations") or {}).values():
        dims.update(d for d in (cohort_dims or {}) if d in ALLOC_DIMS)
    if "prompt_version" in patch:
        dims.add("prompt_version")
    if "schedule" in patch:
        dims.add("schedule")
    return [d for d in ALL_DIMS if d in dims]


# ---------------------------------------------------------------- stepping (T2, T3)

def step_toward(p: dict[str, float], q: dict[str, float], limit: float) -> dict[str, float]:
    """T2: move distribution p straight toward q by at most `limit` total-variation distance.
    50/50 -> 100/0 with limit 0.2 gives 70/30, then 90/10, then 100/0. Works for any options."""
    if tv_distance(p, q) <= limit + 1e-12:
        return dict(q)
    keys = sorted(set(p) | set(q))
    t = limit / tv_distance(p, q)
    while True:  # rounded, then normalised as the sim does, the step must still be within the limit
        raw = {k: max(p.get(k, 0.0) + t * (q.get(k, 0.0) - p.get(k, 0.0)), 0.0) for k in keys}
        total = sum(raw.values())
        out = {k: round(v / total, 6) for k, v in raw.items() if round(v / total, 6) > 0}
        norm = sum(out.values())
        if tv_distance(_norm(p), {k: v / norm for k, v in out.items()}) <= limit:
            return out
        t -= 1e-6


def _norm(d: dict[str, float]) -> dict[str, float]:
    total = sum(d.values())
    return {k: v / total for k, v in d.items()} if total else {}


def steps_needed(old: dict, new: dict, limit: float) -> int:
    """How many steps of `limit` it takes to get from old to new (1 if it fits now)."""
    return max(1, math.ceil(max_shift(old, new) / limit - 1e-9)) if limit > 0 else 1


def step_config(old: dict, new: dict, limit: float) -> dict | None:
    """T3: `new`, except every welcome/voice/caller_id distribution moved at most `limit` from `old`.
    Other settings are taken from `new` as they are. Returns None if no such step can be built
    (for example a cohort that inherits from `default` while `default` itself changes shape)."""
    if limit <= 0:
        return None
    out = copy.deepcopy(new)
    for c in sorted(_cohort_ids(old) | _cohort_ids(new)):
        for d in ALLOC_DIMS:
            p, q = distribution(old, c, d), distribution(new, c, d)
            if tv_distance(p, q) <= limit:
                continue
            own = (new.get("allocations", {}).get(c) or {}).get(d)
            if own is None and c != "default":
                continue  # follows default, which is stepped on its own
            out.setdefault("allocations", {}).setdefault(c, {})[d] = step_toward(p, q, limit)
    return out if max_shift(old, out) <= limit + 1e-9 else None
