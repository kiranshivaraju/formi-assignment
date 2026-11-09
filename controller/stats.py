"""Shared statistics (T1). Every feature that compares two rates uses this one test."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


def z_score(hits_a: int, n_a: int, hits_b: int, n_b: int) -> float:
    """Two-proportion z-test ("luck units"): how many standard errors rate B sits below rate A.
    Positive when B is worse. Example (Kestrel day 2): number D1 573/2385 = 24.0%, number D2
    224/2415 = 9.3%  ->  z = 13.7. Day 1 (23.7% vs 23.1%) gives about 0.5."""
    if not n_a or not n_b:
        return 0.0
    pooled = (hits_a + hits_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    return (hits_a / n_a - hits_b / n_b) / se if se else 0.0


def clearly_not_worse(hits_ref: int, n_ref: int, hits: int, n: int, margin: float) -> float:
    """One-sided test that rate B is NOT `margin` (relative) below rate A: luck units by which B sits
    above the line (1 - margin) x A. Large positive = clearly fine. Used to pass a trial, where
    "not proven bad" is not enough (C10)."""
    if not n_ref or not n:
        return 0.0
    ra, rb = hits_ref / n_ref, hits / n
    line = (1 - margin) * ra
    se = math.sqrt(rb * (1 - rb) / n + (1 - margin) ** 2 * ra * (1 - ra) / n_ref)
    return (rb - line) / se if se else 0.0


def rate(hits: int, n: int) -> float | None:
    return hits / n if n else None


@dataclass
class Lift:
    """Rate of B minus rate of A, compared stratum by stratum (E4). z > 0 when B is better."""
    diff: float
    se: float
    z: float
    n_a: int
    hits_a: int
    n_b: int
    hits_b: int

    @property
    def rate_a(self) -> float | None:
        return rate(self.hits_a, self.n_a)

    @property
    def rate_b(self) -> float | None:
        return rate(self.hits_b, self.n_b)


def stratified_lift(strata: Iterable[tuple[int, int, int, int]]) -> Lift:
    """Compare B with A inside each stratum (an hour of a day, a cohort) and add up, weighting each
    stratum by how much it can tell us (na x nb / (na + nb)). Strata where either side has no calls
    say nothing and are skipped. With one stratum this is the ordinary two-proportion test.

    Why (E4, M1): if B ran mostly in the evening and evenings engage better, pooling every call
    would credit B with the evening. Comparing within each hour removes that."""
    num = wsum = var = 0.0
    na = ha = nb = hb = 0
    for n_a, h_a, n_b, h_b in strata:
        if not n_a or not n_b:
            continue
        w = n_a * n_b / (n_a + n_b)
        p = (h_a + h_b) / (n_a + n_b)
        num += w * (h_b / n_b - h_a / n_a)
        var += w * w * p * (1 - p) * (1 / n_a + 1 / n_b)
        wsum += w
        na, ha, nb, hb = na + n_a, ha + h_a, nb + n_b, hb + h_b
    if not wsum:
        return Lift(0.0, 0.0, 0.0, na, ha, nb, hb)
    diff, se = num / wsum, math.sqrt(var) / wsum
    return Lift(diff, se, diff / se if se else 0.0, na, ha, nb, hb)


def calls_needed(p: float, mde: float, z: float, power_z: float = 0.84) -> int:
    """Calls per option to catch a real difference of `mde` (absolute) at `z` luck units, 80% of the
    time. At p 0.5, mde 0.08, z 3: about 1,150."""
    p = min(max(p, 0.01), 0.99)
    return math.ceil((z + power_z) ** 2 * 2 * p * (1 - p) / mde ** 2)
