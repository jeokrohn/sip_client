"""Scenario YAML parser tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def test_call_behaviors_sample_scenario_matches_expected_flow() -> None:
    """Verify the behavior-owned outbound sample remains parseable.

    :returns: None.
    """

    scenario = load_scenario(Path("scenarios/call-behaviors.yml"))

    assert scenario.name == "behavior-owned-outbound-call"
    assert scenario.behaviors is not None
    behavior = scenario.behaviors["caller"]
    assert behavior.initial_state == "unregistered"
    assert behavior.states["unregistered"].entry_actions[0].action == "register"
    assert behavior.states["unregistered"].rules[0].event == "registered"
    assert behavior.states["idle"].entry_actions[0].params == {
        "name": "calls_remaining",
        "value": 10,
    }
    assert behavior.states["idle"].rules[0].event == "scenario_trigger"
    assert behavior.states["idle"].rules[0].params["name"] == "start_call"
    assert behavior.states["loop"].entry_actions[0].params == {
        "target": "Heidi",
        "use_target_extension": True,
        "save_as": "outbound",
        "reuse_alias": True,
    }
    assert behavior.states["connected"].entry_actions[1].params == {
        "call": "outbound",
        "text": "This is an automated test call",
    }
    assert behavior.states["connected"].rules[1].event == "call_state"
    assert behavior.states["connected"].rules[1].actions[0].action == "decrement_counter"
    assert behavior.states["connected"].rules[2].event == "counter_reached"
    assert [step.action for step in scenario.steps] == [
        "wait_behavior_state",
        "wait_behavior_state",
        "trigger_behavior",
        "wait_behavior_state",
        "wait_behavior_state",
        "wait_behavior_state",
        "wait_behavior_state",
    ]
    assert scenario.steps[2].params == {"endpoint": "Leslie", "name": "start_call"}


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


def test_parse_scenario_accepts_endpoint_behaviors() -> None:
    """Verify endpoint behavior definitions parse into typed scenario data.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "behaviors": {
                "auto_answer": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "actions": [{"action": "answer", "call": "inbound"}],
                                    "next_state": "connected",
                                }
                            }
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                }
            },
            "endpoints": {"bob": {"client": "bob", "behavior": "auto_answer"}},
            "steps": [{"action": "register", "clients": ["alice", "bob"]}],
        }
    )

    assert scenario.behaviors is not None
    assert scenario.endpoints is not None
    behavior = scenario.behaviors["auto_answer"]
    endpoint = scenario.endpoints["bob"]
    assert behavior.initial_state == "idle"
    assert behavior.states["idle"].rules[0].event == "call_received"
    assert behavior.states["idle"].rules[0].actions[0].action == "answer"
    assert endpoint.client == "bob"
    assert endpoint.behavior == "auto_answer"


def test_parse_scenario_accepts_behavior_entry_and_wait_state_step() -> None:
    """Verify lifecycle behavior syntax and behavior-state waits parse.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "behaviors": {
                "outbound_caller": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "entry": [
                                {
                                    "action": "call",
                                    "target": "webex_user",
                                    "save_as": "outbound",
                                }
                            ],
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            },
                        },
                        "connected": {"on": {"call_state": {"call": "outbound", "state": "disconnected"}}},
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "outbound_caller"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "connected"}],
        }
    )

    assert scenario.behaviors is not None
    behavior = scenario.behaviors["outbound_caller"]
    assert behavior.states["unregistered"].entry_actions[0].action == "register"
    assert behavior.states["unregistered"].rules[0].event == "registered"
    assert behavior.states["idle"].entry_actions[0].params == {
        "target": "webex_user",
        "save_as": "outbound",
    }
    assert scenario.steps[0].action == "wait_behavior_state"


def test_parse_scenario_accepts_behavior_counters() -> None:
    """Verify counter actions and counter events parse.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "behaviors": {
                "counter_loop": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "entry": [
                                {"action": "set_counter", "name": "calls_remaining", "value": 3},
                                {"action": "increment_counter", "name": "calls_remaining", "by": 2},
                            ],
                            "on": {"scenario_trigger": {"name": "start_call", "next_state": "connected"}},
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "state": "disconnected",
                                    "actions": [{"action": "decrement_counter", "name": "calls_remaining"}],
                                    "next_state": "connected",
                                },
                                "counter_reached": {
                                    "counter": "calls_remaining",
                                    "value": 0,
                                    "next_state": "done",
                                },
                            }
                        },
                        "done": {"entry": [{"action": "cancel_timer", "name": "unused"}]},
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "counter_loop"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "done"}],
        }
    )

    assert scenario.behaviors is not None
    behavior = scenario.behaviors["counter_loop"]
    assert behavior.states["idle"].entry_actions[0].params == {"name": "calls_remaining", "value": 3}
    assert behavior.states["idle"].entry_actions[1].params == {"name": "calls_remaining", "by": 2}
    decrement = behavior.states["connected"].rules[0].actions[0]
    assert decrement.action == "decrement_counter"
    assert decrement.params == {"name": "calls_remaining", "by": 1}
    counter_rule = behavior.states["connected"].rules[1]
    assert counter_rule.event == "counter_reached"
    assert counter_rule.params == {"counter": "calls_remaining", "value": 0}


def test_parse_scenario_rejects_malformed_behavior_counters() -> None:
    """Verify counter fields are validated at parse time.

    :returns: None.
    """

    def base_scenario() -> dict[str, Any]:
        """Create a fresh malformed-counter scenario shell.

        :returns: Scenario mapping ready for one mutation.
        """

        return {
            "behaviors": {
                "bad": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "entry": [],
                            "on": {"scenario_trigger": {"name": "start"}},
                        }
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "bad"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "idle"}],
        }

    with pytest.raises(ScenarioError, match="value must be an integer"):
        bad = base_scenario()
        bad["behaviors"]["bad"]["states"]["idle"]["entry"] = [
            {"action": "set_counter", "name": "calls_remaining", "value": "3"}
        ]
        parse_scenario(bad)
    with pytest.raises(ScenarioError, match="by must be a positive integer"):
        bad = base_scenario()
        bad["behaviors"]["bad"]["states"]["idle"]["entry"] = [
            {"action": "increment_counter", "name": "calls_remaining", "by": 0}
        ]
        parse_scenario(bad)
    with pytest.raises(ScenarioError, match="name is required"):
        bad = base_scenario()
        bad["behaviors"]["bad"]["states"]["idle"]["entry"] = [{"action": "set_counter", "value": 1}]
        parse_scenario(bad)
    with pytest.raises(ScenarioError, match="unsupported field"):
        bad = base_scenario()
        bad["behaviors"]["bad"]["states"]["idle"]["entry"] = [
            {"action": "decrement_counter", "name": "calls_remaining", "value": 1}
        ]
        parse_scenario(bad)
    with pytest.raises(ScenarioError, match="counter.*is required"):
        bad = base_scenario()
        bad["behaviors"]["bad"]["states"]["idle"]["on"] = {"counter_reached": {"value": 0}}
        parse_scenario(bad)
    with pytest.raises(ScenarioError, match="unsupported action"):
        parse_scenario({"steps": [{"action": "set_counter", "name": "calls_remaining", "value": 1}]})


def test_parse_scenario_accepts_behavior_alias_reuse() -> None:
    """Verify looping behavior aliases can opt in to explicit reuse.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "behaviors": {
                "looping": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "entry": [
                                {
                                    "action": "call",
                                    "target": "webex_user",
                                    "save_as": "outbound",
                                    "reuse_alias": True,
                                }
                            ],
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "reuse_alias": True,
                                }
                            },
                        }
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "looping"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "idle"}],
        }
    )

    assert scenario.behaviors is not None
    state = scenario.behaviors["looping"].states["idle"]
    assert state.entry_actions[0].params["reuse_alias"] is True
    assert state.rules[0].params["reuse_alias"] is True


def test_parse_scenario_rejects_malformed_behavior_alias_reuse() -> None:
    """Verify alias reuse has constrained behavior-only syntax.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="reuse_alias must be boolean"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "entry": [
                                    {
                                        "action": "call",
                                        "target": "webex_user",
                                        "save_as": "outbound",
                                        "reuse_alias": "yes",
                                    }
                                ]
                            }
                        },
                    }
                },
                "endpoints": {"alice": {"client": "alice", "behavior": "bad"}},
                "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "idle"}],
            }
        )
    with pytest.raises(ScenarioError, match="only supported for call actions"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "entry": [
                                    {
                                        "action": "answer",
                                        "call": "inbound",
                                        "reuse_alias": True,
                                    }
                                ]
                            }
                        },
                    }
                },
                "endpoints": {"alice": {"client": "alice", "behavior": "bad"}},
                "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "idle"}],
            }
        )
    with pytest.raises(ScenarioError, match="only supported for call_received"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "call_state": {
                                        "state": "connected",
                                        "reuse_alias": True,
                                    }
                                }
                            }
                        },
                    }
                },
                "endpoints": {"alice": {"client": "alice", "behavior": "bad"}},
                "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "idle"}],
            }
        )


def test_parse_scenario_accepts_scenario_trigger_event_and_step() -> None:
    """Verify scripted steps can emit named behavior triggers.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "behaviors": {
                "triggered": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "start_call",
                                    "next_state": "called",
                                }
                            }
                        },
                        "called": {"entry": [{"action": "cancel_timer", "name": "unused"}]},
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "triggered"}},
            "steps": [{"action": "trigger_behavior", "endpoint": "alice", "name": "start_call"}],
        }
    )

    assert scenario.behaviors is not None
    rule = scenario.behaviors["triggered"].states["idle"].rules[0]
    assert rule.event == "scenario_trigger"
    assert rule.params["name"] == "start_call"
    assert scenario.steps[0].action == "trigger_behavior"


def test_parse_scenario_rejects_register_in_behavior_event_actions() -> None:
    """Verify registration is only allowed as a behavior entry action.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="entry actions"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "timer_expired": {
                                        "timer": "late",
                                        "actions": [{"action": "register"}],
                                    }
                                }
                            }
                        },
                    }
                },
                "steps": [{"action": "register", "client": "alice"}],
            }
        )


def test_parse_scenario_rejects_invalid_behavior_entry_action() -> None:
    """Verify behavior entry actions are validated.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="missing required"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {"idle": {"entry": [{"action": "call"}]}},
                    }
                },
                "steps": [{"action": "register", "client": "alice"}],
            }
        )


def test_parse_scenario_rejects_wait_behavior_state_without_state() -> None:
    """Verify behavior-state waits require endpoint and state.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="missing required"):
        parse_scenario({"steps": [{"action": "wait_behavior_state", "endpoint": "alice"}]})


def test_parse_scenario_rejects_trigger_behavior_without_name() -> None:
    """Verify behavior triggers require a name.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="missing required"):
        parse_scenario({"steps": [{"action": "trigger_behavior", "endpoint": "alice"}]})


def test_parse_scenario_rejects_endpoint_with_unknown_behavior() -> None:
    """Verify endpoint assignments must reference defined behaviors.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="unknown behavior"):
        parse_scenario(
            {
                "behaviors": {},
                "endpoints": {"bob": {"client": "bob", "behavior": "missing"}},
                "steps": [{"action": "register", "client": "bob"}],
            }
        )


def test_parse_scenario_rejects_unsupported_behavior_event() -> None:
    """Verify behavior rules only accept the v1 event vocabulary.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="unsupported event"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "identity_updated": {},
                                }
                            }
                        },
                    }
                },
                "steps": [{"action": "register", "client": "bob"}],
            }
        )


def test_parse_scenario_rejects_disallowed_behavior_action() -> None:
    """Verify orchestration-only actions cannot be used inside behaviors.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="not supported inside endpoint behaviors"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "call_received": {
                                        "save_call_as": "inbound",
                                        "actions": [{"action": "expect_incoming", "client": "bob"}],
                                    }
                                }
                            }
                        },
                    }
                },
                "steps": [{"action": "register", "client": "bob"}],
            }
        )


def test_parse_scenario_rejects_invalid_behavior_timer() -> None:
    """Verify behavior timer actions require positive durations.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="seconds"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "call_received": {
                                        "save_call_as": "inbound",
                                        "actions": [{"action": "start_timer", "name": "hold", "seconds": 0}],
                                    }
                                }
                            }
                        },
                    }
                },
                "steps": [{"action": "register", "client": "bob"}],
            }
        )


def test_parse_scenario_rejects_unknown_behavior_next_state() -> None:
    """Verify behavior transitions must target defined states.

    :returns: None.
    """

    with pytest.raises(ScenarioError, match="unknown next_state"):
        parse_scenario(
            {
                "behaviors": {
                    "bad": {
                        "initial_state": "idle",
                        "states": {
                            "idle": {
                                "on": {
                                    "call_received": {
                                        "save_call_as": "inbound",
                                        "next_state": "missing",
                                    }
                                }
                            }
                        },
                    }
                },
                "steps": [{"action": "register", "client": "bob"}],
            }
        )


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
