"""Loads controller/settings.yaml: our own checker knobs (see the file header for who owns it).

Every knob is declared here once with its type and allowed range, so a typo or a silly value stops
the controller at startup instead of silently making it reckless.
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).with_name("settings.yaml")


class SettingsError(ValueError):
    pass


def _num(lo, hi, kind=(int, float)):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, kind) or not lo <= v <= hi:
            return f"must be a number in [{lo}, {hi}]"
    return check


def _hours(v):
    if (not isinstance(v, list) or not v or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0
                                                for h in v) or v != sorted(set(v))):
        return "must be a non-empty increasing list of whole hours > 0"


def _bool(v):
    if not isinstance(v, bool):
        return "must be true or false"


def _share(v):
    if v == "equal":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= v <= 0.9:
        return "must be 'equal' or a number in [0.1, 0.9]"


SCHEMA = {
    "storage": {
        "raw_event_days": _num(2, 3650, int),
    },
    "gate": {
        "step_gap_minutes": _num(15, 24 * 60, int),
        "change_ttl_minutes": _num(15, 7 * 24 * 60, int),
    },
    "caller_id_health": {
        "window_hours": _hours,
        "min_calls": _num(30, 100_000, int),
        "min_relative_drop": _num(0.05, 0.95),
        "min_z": _num(2.0, 20.0),
        "pass_z": _num(1.0, 10.0),
        "verify_after_minutes": _num(15, 7 * 24 * 60, int),
        "probe_after_hours": _num(1, 30 * 24, int),
        "probe_share": _num(0.01, 0.5),
        "spread_spares": _bool,
        "spare_trial_share": _num(0.05, 0.5),
        "probe_min_healthy": _num(1, 10, int),
    },
    "script_health": {
        "min_calls": _num(30, 100_000, int),
        "drift_window_hours": _num(3, 7 * 24, int),
        "drift_min_calls": _num(100, 1_000_000, int),
        "min_ratio": _num(1.2, 20.0),
        "min_z": _num(2.0, 20.0),
        "reference_days": _num(1, 60, int),
        "proven_min_calls": _num(100, 1_000_000, int),
        "proven_max_rate": _num(0.001, 0.2),
        "validation_n": _num(50, 2000, int),
        "validation_margin_z": _num(0.0, 5.0),
        "ai_ideas_to_test": _num(0, 3, int),
        "credits_per_incident": _num(1, 1000, int),
        "credit_reserve_share": _num(0.0, 0.9),
        "verify_after_minutes": _num(15, 7 * 24 * 60, int),
        "verify_min_calls": _num(50, 1_000_000, int),
        "verify_z": _num(1.0, 10.0),
        "min_made_up": _num(1, 10_000, int),
    },
    "experiments": {
        "enabled": _bool,
        "control_share": _share,
        "judge_hour": _num(0, 23, int),
        "min_calls": _num(50, 1_000_000, int),
        "win_z": _num(2.0, 10.0),
        "mde": _num(0.01, 0.5),
        "harm_min_calls": _num(30, 1_000_000, int),
        "harm_z": _num(2.0, 10.0),
        "guard_z": _num(1.0, 10.0),
        "segment_min_calls": _num(30, 1_000_000, int),
        "segment_harm_z": _num(1.5, 10.0),
        "max_carve_outs": _num(0, 10, int),
        "holdout_share": _num(0.05, 0.5),
        "decay_window_hours": _num(6, 14 * 24, int),
        "decay_min_calls": _num(30, 1_000_000, int),
        "decay_z": _num(1.5, 10.0),
        "lead_z": _num(1.0, 10.0),
        "recent_hours": _num(6, 7 * 24, int),
        "first_step_add": _num(0.02, 0.8),
    },
}
PER_WORKFLOW = {"caller_id_health", "script_health", "experiments"}  # sections with defaults + per-workflow overrides


class Settings:
    def __init__(self, raw: dict):
        self.raw = raw

    def gate(self, key: str):
        return self.raw["gate"][key]

    def storage(self, key: str):
        return self.raw["storage"][key]

    def unknown_workflows(self, known: list[str]) -> list[str]:
        """Override sections naming a workflow the sim doesn't have (a typo would be silently ignored)."""
        return sorted(f"{s}.workflows.{w}" for s in PER_WORKFLOW for w in (self.raw[s].get("workflows") or {})
                      if w not in known)

    def feature(self, section: str, workflow_id: str) -> dict:
        """A feature's knobs for one workflow: defaults with that workflow's overrides on top."""
        sec = self.raw[section]
        return {**sec["defaults"], **(sec.get("workflows") or {}).get(workflow_id, {})}


def _check(section: str, values: dict, where: str, required: bool) -> list[str]:
    schema = SCHEMA[section]
    if not isinstance(values, dict):
        return [f"{where}: must be a mapping"]
    errs = [f"{where}.{k}: unknown setting" for k in values if k not in schema]
    if required:
        errs += [f"{where}.{k}: missing" for k in schema if k not in values]
    errs += [f"{where}.{k}: {e}" for k, v in values.items() if k in schema and (e := schema[k](v))]
    return errs


def load(path: str | Path = DEFAULT_PATH) -> Settings:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    errs = [f"{k}: unknown section" for k in raw if k not in SCHEMA]
    for section in SCHEMA:
        body = raw.get(section)
        if body is None:
            errs.append(f"{section}: missing section")
        elif not isinstance(body, dict):
            errs.append(f"{section}: must be a mapping")
        elif section in PER_WORKFLOW:
            if not isinstance(body.get("workflows") or {}, dict):
                errs.append(f"{section}.workflows: must be a mapping")
                continue
            errs += _check(section, body.get("defaults") or {}, f"{section}.defaults", required=True)
            errs += [f"{section}: unknown key {k}" for k in body if k not in ("defaults", "workflows")]
            for wid, over in (body.get("workflows") or {}).items():
                errs += _check(section, over, f"{section}.workflows.{wid}", required=False)
        else:
            errs += _check(section, body, section, required=True)
    if errs:
        raise SettingsError(f"{path}: " + "; ".join(errs))
    return Settings(raw)
