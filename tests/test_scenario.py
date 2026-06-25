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


def test_register_step_accepts_stay_registered_options() -> None:
    scenario = parse_scenario(
        {
            "steps": [
                {
                    "action": "register",
                    "client": "alice",
                    "stay_registered_for": 1.5,
                    "require_reregistration": True,
                    "min_reregistrations": 1,
                }
            ]
        }
    )

    assert scenario.steps[0].params["stay_registered_for"] == 1.5


def test_register_step_rejects_invalid_stay_registered_duration() -> None:
    with pytest.raises(ScenarioError, match="stay_registered_for"):
        parse_scenario({"steps": [{"action": "register", "client": "alice", "stay_registered_for": 0}]})
