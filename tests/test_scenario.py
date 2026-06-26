from __future__ import annotations

from pathlib import Path

import pytest

from wxcalls.exceptions import ScenarioError
from wxcalls.scenario import load_scenario, parse_scenario


def test_parse_scenario_validates_required_fields() -> None:
    """Verify a valid scenario parses required step fields.

    :returns: None.
    """

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


def test_leslie_call_7109_scenario_matches_expected_flow() -> None:
    """Verify the outbound sample scenario keeps its expected action flow.

    :returns: None.
    """

    scenario = load_scenario(Path("scenarios/leslie_call_7109.yml"))

    assert [step.action for step in scenario.steps] == [
        "register",
        "call",
        "wait_state",
        "wait_media",
        "play_tts",
        "wait_state",
    ]
    assert scenario.steps[1].params["target"] == "80027109"
    assert scenario.steps[-1].params == {
        "call": "leslie_to_7109",
        "state": "disconnected",
        "timeout": 20,
    }


def test_leslie_answer_incoming_scenario_matches_expected_flow() -> None:
    """Verify the inbound sample scenario keeps its expected action flow.

    :returns: None.
    """

    scenario = load_scenario(Path("scenarios/leslie_answer_incoming.yml"))

    assert [step.action for step in scenario.steps] == [
        "register",
        "expect_incoming",
        "answer",
        "wait_media",
        "play_tts",
        "wait_state",
    ]
    assert scenario.steps[1].params == {
        "client": "Leslie",
        "save_as": "leslie_incoming",
        "timeout": 120,
    }
    assert scenario.steps[4].params["text"] == "test call established. Pls. hang up"
    assert scenario.steps[-1].params == {
        "call": "leslie_incoming",
        "state": "disconnected",
        "timeout": 120,
    }


def test_parse_scenario_rejects_unknown_action() -> None:
    """Verify unsupported scenario actions are rejected.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="unsupported action"):
        parse_scenario({"steps": [{"action": "dance"}]})


def test_parse_scenario_rejects_missing_action_field() -> None:
    """Verify missing action-specific fields are rejected.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="missing required"):
        parse_scenario({"steps": [{"action": "call", "client": "alice"}]})


def test_call_step_rejects_invalid_target_extension_flag() -> None:
    """Verify named-target extension dialing requires a YAML boolean flag.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="use_target_extension"):
        parse_scenario(
            {
                "steps": [
                    {
                        "action": "call",
                        "client": "alice",
                        "target": "bob",
                        "use_target_extension": "true",
                    }
                ]
            }
        )


def test_parse_scenario_accepts_parallel_branches() -> None:
    """Verify parallel branch steps are parsed and validated recursively.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "steps": [
                {
                    "action": "parallel",
                    "branches": {
                        "caller": [{"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"}],
                        "callee": [
                            {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                            {"action": "answer", "call": "bob_incoming"},
                        ],
                    },
                }
            ]
        }
    )

    step = scenario.steps[0]
    branches = step.params["branches"]
    assert step.action == "parallel"
    assert sorted(branches) == ["callee", "caller"]
    assert branches["caller"][0].action == "call"
    assert branches["callee"][1].params["call"] == "bob_incoming"


@pytest.mark.parametrize(
    "branches",
    [
        [],
        {},
        {"caller": []},
    ],
)
def test_parse_scenario_rejects_invalid_parallel_branches(branches: object) -> None:
    """Verify malformed parallel branches are rejected.

    :param branches: Invalid branch value.
    :returns: None.
    """

    with pytest.raises(ScenarioError, match="parallel"):
        parse_scenario({"steps": [{"action": "parallel", "branches": branches}]})


def test_record_during_playback_requires_one_media_source() -> None:
    """Verify overlap recording scenarios name exactly one media source.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="exactly one of text or path"):
        parse_scenario(
            {
                "steps": [
                    {
                        "action": "record_during_playback",
                        "playback_call": "alice_to_bob",
                        "recording_call": "bob_incoming",
                    }
                ]
            }
        )

    with pytest.raises(ScenarioError, match="exactly one of text or path"):
        parse_scenario(
            {
                "steps": [
                    {
                        "action": "record_during_playback",
                        "playback_call": "alice_to_bob",
                        "recording_call": "bob_incoming",
                        "text": "hello",
                        "path": "hello.wav",
                    }
                ]
            }
        )


def test_register_step_accepts_stay_registered_options() -> None:
    """Verify registration soak options are accepted.

    :returns: None.
    """

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
    """Verify invalid registration soak durations are rejected.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="stay_registered_for"):
        parse_scenario({"steps": [{"action": "register", "client": "alice", "stay_registered_for": 0}]})
