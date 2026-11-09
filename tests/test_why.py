"""C12: the "why did X change" tool answers from our own records only."""

import pytest

from controller import store, why
from tests.test_controller_store import STATE, TEST_DSN, URL, test_db  # noqa: F401


def test_time_labels():
    assert why.parse_label("D3 14:00") == 3 * 1440 + 14 * 60
    assert why.parse_label("D-1 09:15") == -1440 + 9 * 60 + 15
    with pytest.raises(ValueError):
        why.parse_label("day 3")


def test_supervisor_change_and_our_reaction(capsys, request):
    with store.connect(TEST_DSN) as conn:
        run, _ = store.resolve_run(conn, URL, {**STATE, "scenario": request.node.name})
        t = 3 * 1440 + 11 * 60 + 30
        store.record_decision(conn, run, key="cfg:1", sim_minute=t, workflow_id="ks", kind="config_observed",
                              action="recorded", reason="config v9 applied by supervisor",
                              inputs={"version": 9, "actor": "supervisor", "changed_dims": ["prompt_version"]})
        store.record_decision(conn, run, key="hold:1", sim_minute=t + 15, workflow_id="ks", kind="supervisor_hold",
                              action="hold", reason="supervisor changed prompt_version: leaving it alone", inputs={})
        store.record_decision(conn, run, key="inc:1", sim_minute=t + 75, workflow_id="ks", kind="script_health",
                              action="incident", reason="script P2 makes things up on 8.3% of answered calls",
                              inputs={})
    assert why.main(["ks", "D3 11:30", "--dsn", TEST_DSN, "--scenario", request.node.name]) == 0
    out = capsys.readouterr().out
    assert "config v9 at D3 11:30: prompt_version changed by supervisor" in out
    assert out.count("leaving it alone") == 1                       # reaction shown once
    assert "[script_health/incident] script P2 makes things up" in out
    assert why.main(["ks", "--list", "--dsn", TEST_DSN, "--scenario", request.node.name]) == 0
    assert "D3 11:30  v9  supervisor  prompt_version" in capsys.readouterr().out
