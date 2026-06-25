from __future__ import annotations

import pytest

from wxcalls.exceptions import ScenarioError
from wxcalls.scenario import parse_scenario


def test_parse_scenario_validates_required_fields() -> None:
    scenario = parse_scenario(
        {
            "name": "happy",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "call1"},
            ],
        }
    )

    assert scenario.name == "happy"
    assert scenario.steps[1].action == "call"
    assert scenario.steps[1].params["save_as"] == "call1"


def test_parse_scenario_rejects_unknown_action() -> None:
    with pytest.raises(ScenarioError, match="unsupported action"):
        parse_scenario({"steps": [{"action": "dance"}]})


def test_parse_scenario_rejects_missing_action_field() -> None:
    with pytest.raises(ScenarioError, match="missing required"):
        parse_scenario({"steps": [{"action": "call", "client": "alice"}]})
