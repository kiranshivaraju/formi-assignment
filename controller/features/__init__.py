"""Features (F1, F2): the detection rules that decide what to change.

Every feature has the same shape: a function `check(ctx) -> list[Change]`. It reads the scoreboard
and the current configs through `ctx`, and returns the changes it wants right now ("target + reason +
numbers"). It never talks to the client or the sim's write endpoints: the Governor (the gate) decides
how and when a change goes out. A feature that still wants a change returns it again every loop
step; one it stops returning is withdrawn by the gate at once (and, as a backstop, any queued change
not refreshed for `change_ttl_minutes` is dropped) (F3).

Adding a feature = one module with a `check` function + one line in FEATURES.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import httpx
import psycopg

from .. import store
from ..client import SimClient
from ..governance import Change, Governor
from ..settings import Settings

log = logging.getLogger(__name__)
_LEADS_CACHE: dict[tuple, int] = {}


@dataclass
class Context:
    """What a feature may look at during one loop step."""
    conn: psycopg.Connection
    run: store.Run
    sim: SimClient
    gov: Governor
    settings: Settings
    workflows: dict[str, dict]
    now: int
    _configs: dict = field(default_factory=dict)
    keep: set = field(default_factory=set)  # intents a feature could not evaluate this step: not withdrawn
    sim_state: dict = field(default_factory=dict)  # GET /sim/state at the start of this step (credits)

    def config(self, wid: str) -> dict:
        """The workflow's current config ({version, config, ...}), read once per step."""
        if wid not in self._configs:
            self._configs[wid] = self.sim.config(wid)
        return self._configs[wid]

    def last_change(self, intent_key: str) -> dict | None:
        """The newest change row for an idea (any status), to see whether it was applied."""
        return self.conn.execute("SELECT * FROM changes WHERE run_id=%s AND intent_key=%s ORDER BY change_id DESC "
                                 "LIMIT 1", (self.run.run_id, intent_key)).fetchone()

    def leads_called_24h(self, wid: str, caller_id: str | None = None) -> int:
        """Distinct leads dialled in the last 24 h (optionally on one caller ID). Heavy on raw events,
        so cached per sim hour (L6)."""
        key = (self.run.run_id, wid, caller_id, self.now // 60)
        if key not in _LEADS_CACHE:
            if len(_LEADS_CACHE) > 1000:
                _LEADS_CACHE.clear()
            _LEADS_CACHE[key] = store.leads_called(self.conn, self.run, wid, self.now - store.MINUTES_PER_DAY,
                                                   caller_id=caller_id)
        return _LEADS_CACHE[key]

    def diary(self, key: str, wid: str, kind: str, action: str, reason: str, inputs: dict) -> None:
        store.record_decision(self.conn, self.run, key=key, sim_minute=self.now, workflow_id=wid, kind=kind,
                              action=action, reason=reason, inputs=inputs)


def _registry() -> list[tuple[str, Callable[[Context], list[Change]]]]:
    from . import caller_id, experiments, script
    return [("caller_id_health", caller_id.check), ("script_health", script.check),
            ("experiments", experiments.check)]


FEATURES = _registry()


def run_features(ctx: Context) -> dict[str, str]:
    """F2: run every feature and hand each change to the gate. Returns intent_key -> what the gate
    said. A crashing feature is logged and skipped for this step, so one bug cannot stop the gate
    from applying approvals or the loop from ingesting (it would crash again on every restart)."""
    out = {}
    for name, check in FEATURES:
        try:
            changes = check(ctx)
        except (httpx.TransportError, psycopg.OperationalError):
            raise  # lost a connection: main exits 75 and run.sh restarts us
        except Exception:
            log.exception("feature %s failed at sim minute %s; skipped this step", name, ctx.now)
            ctx.diary(f"feature_error:{name}:{ctx.now // store.MINUTES_PER_DAY}", None, "feature_error", "skipped",
                      f"feature {name} raised an error and was skipped (see controller log)", {"feature": name})
            continue
        for c in changes:
            out[c.intent_key] = ctx.gov.submit(c, ctx.now)
        # An intent the feature no longer returns is no longer wanted: withdraw it now, so a request
        # already out is never applied after the reason for it is gone (N1).
        ctx.gov.withdraw_unlisted(f"{name}:", {c.intent_key for c in changes} | ctx.keep, ctx.now)
    return out
