from __future__ import annotations

from pathlib import Path

import pytest

from wxcalls.exceptions import ScenarioError
from wxcalls.scenario import load_scenario, parse_scenario


def test_parse_scenario_validates_required_fields() -> None:
    scenario = parse_scenario(
        {
            "name": "happy",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "call1"},
                {"action": "wait_media", "call": "call1", "timeout": 5},
            ],
        }
    )

    assert scenario.name == "happy"
    assert scenario.steps[1].action == "call"
    assert scenario.steps[1].params["save_as"] == "call1"
    assert scenario.steps[2].action == "wait_media"


def test_device1_call_7109_scenario_matches_expected_flow() -> None:
    scenario = load_scenario(Path("scenarios/device1_call_7109.yml"))

    assert [step.action for step in scenario.steps] == [
        "register",
        "call",
        "wait_state",
        "wait_media",
        "play_tts",
        "wait_state",
    ]
    assert scenario.steps[1].params["target"] == "7109"
    assert scenario.steps[-1].params == {
        "call": "device1_to_7109",
        "state": "disconnected",
        "timeout": 20,
    }


def test_device1_answer_incoming_scenario_matches_expected_flow() -> None:
    scenario = load_scenario(Path("scenarios/device1_answer_incoming.yml"))

    assert [step.action for step in scenario.steps] == [
        "register",
        "expect_incoming",
        "answer",
        "wait_media",
        "play_tts",
        "wait_state",
    ]
    assert scenario.steps[1].params == {
        "client": "device1",
        "save_as": "device1_incoming",
        "timeout": 120,
    }
    assert scenario.steps[4].params["text"] == "test call established. Pls. hang up"
    assert scenario.steps[-1].params == {
        "call": "device1_incoming",
        "state": "disconnected",
        "timeout": 120,
    }


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
