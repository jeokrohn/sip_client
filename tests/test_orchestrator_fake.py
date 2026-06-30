"""Fake backend orchestration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from wxcalls.artifacts import ArtifactWriter
from wxcalls.backends.base import CallHandle
from wxcalls.backends.fake import FakeSipBackend
from wxcalls.config import LabConfig, SipClientConfig
from wxcalls.exceptions import ScenarioError
from wxcalls.media import MediaAsset, create_silence_wav, generate_marker_tone
from wxcalls.orchestrator import CallLab
from wxcalls.scenario import parse_scenario


class MarkerMediaFactory:
    def __init__(self, work_dir: Path) -> None:
        """Create a deterministic marker media factory for tests.

        :param work_dir: Directory where fake media should be written.
        :returns: None.
        """

        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare_tts(self, text: str, marker: str | None = None, voice: str | None = None) -> MediaAsset:
        """Generate marker-only media for a TTS request.

        :param text: Requested speech text.
        :param marker: Optional marker identifier.
        :param voice: Optional voice name.
        :returns: Prepared media asset.
        """

        path = self.work_dir / "tts.wav"
        generate_marker_tone(marker or "default-marker", path)
        return MediaAsset(path=path, marker=marker)

    def prepare_wav(self, source: Path, marker: str | None = None) -> MediaAsset:
        """Return an existing WAV path as prepared media.

        :param source: Source WAV path.
        :param marker: Optional marker identifier.
        :returns: Prepared media asset.
        """

        return MediaAsset(path=source, marker=marker)


class TimingFakeSipBackend(FakeSipBackend):
    """Fake backend that records media operation ordering for tests."""

    def __init__(self) -> None:
        """Create a timing fake backend.

        :returns: None.
        """

        super().__init__()
        self.media_events: list[tuple[str, float]] = []

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Record playback start time before delegating to the fake backend.

        :param call: Call handle receiving playback.
        :param path: WAV path to play.
        :returns: None.
        """

        self.media_events.append(("play", asyncio.get_running_loop().time()))
        await super().play_wav(call, path)

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record recording start/end times before delegating to the fake backend.

        :param call: Call handle being recorded.
        :param output_path: Destination recording path.
        :param seconds: Requested recording duration.
        :returns: None.
        """

        loop = asyncio.get_running_loop()
        self.media_events.append(("record_start", loop.time()))
        await super().record_wav(call, output_path, seconds)
        self.media_events.append(("record_end", loop.time()))


class CancellableRecordSipBackend(FakeSipBackend):
    """Fake backend that records whether overlap recording is cancelled."""

    def __init__(self) -> None:
        """Create a cancellation-aware fake backend.

        :returns: None.
        """

        super().__init__()
        self.record_cancelled = False
        self.requested_record_seconds: float | None = None

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Keep recording until cancelled and leave a finalized WAV artifact.

        :param call: Call handle being recorded.
        :param output_path: Destination recording path.
        :param seconds: Requested recording duration.
        :returns: None.
        :raises asyncio.CancelledError: If the orchestrator stops recording.
        """

        self.requested_record_seconds = seconds
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            self.record_cancelled = True
            raise
        finally:
            create_silence_wav(output_path, seconds=0.01)


@pytest.mark.parametrize("use_hold_resume", [False, True])
def test_fake_backend_runs_audio_marker_scenario(tmp_path: Path, use_hold_resume: bool) -> None:
    """Verify the fake backend runs an audio-marker scenario end to end.

    :param tmp_path: Temporary pytest directory.
    :param use_hold_resume: Whether to include hold/resume steps.
    :returns: None.
    """

    scenario_steps = [
        {"action": "register", "clients": ["alice", "bob"]},
        {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
        {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
        {"action": "answer", "call": "bob_incoming"},
        {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
        {"action": "wait_media", "call": "alice_to_bob"},
    ]
    if use_hold_resume:
        scenario_steps.extend(
            [
                {"action": "hold", "call": "alice_to_bob"},
                {"action": "resume", "call": "alice_to_bob"},
            ]
        )
    scenario_steps.extend(
        [
            {
                "action": "play_tts",
                "call": "alice_to_bob",
                "text": "hello",
                "marker": "marker-live",
            },
            {
                "action": "record",
                "call": "bob_incoming",
                "seconds": 1.0,
                "save_as": "bob_recording",
            },
            {"action": "assert_marker", "recording": "bob_recording", "marker": "marker-live"},
            {"action": "hangup", "calls": ["alice_to_bob", "bob_incoming"]},
        ]
    )

    messages: list[str] = []
    lab = _lab(tmp_path, progress_reporter=messages.append)
    scenario = parse_scenario({"name": "fake-audio", "steps": scenario_steps})

    async def run() -> None:
        """Run the fake audio marker scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert (tmp_path / "artifacts" / "test-run" / "logs" / "timeline.json").exists()


def test_fake_backend_runs_parallel_audio_marker_scenario(tmp_path: Path) -> None:
    """Verify parallel branches can set up a call and verify media.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    messages: list[str] = []
    lab = _lab(tmp_path, progress_reporter=messages.append)
    scenario = parse_scenario(
        {
            "name": "parallel-audio",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {
                    "action": "parallel",
                    "branches": {
                        "caller": [
                            {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                            {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
                            {"action": "wait_media", "call": "alice_to_bob"},
                        ],
                        "callee": [
                            {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                            {"action": "answer", "call": "bob_incoming"},
                            {"action": "wait_state", "call": "bob_incoming", "state": "connected"},
                            {"action": "wait_media", "call": "bob_incoming"},
                        ],
                    },
                },
                {
                    "action": "record_during_playback",
                    "playback_call": "alice_to_bob",
                    "recording_call": "bob_incoming",
                    "text": "hello",
                    "marker": "marker-parallel",
                    "pre_roll": 0.01,
                    "post_roll": 0.01,
                    "save_as": "bob_recording",
                },
                {"action": "hangup", "calls": ["alice_to_bob", "bob_incoming"]},
            ],
        }
    )

    async def run() -> None:
        """Run the parallel audio marker scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["alice_to_bob"].state == "disconnected"
    assert lab.recordings["bob_recording"].exists()
    assert any(event["event"] == "parallel_started" for event in lab.artifacts.timeline)
    assert any(event["event"] == "branch_finished" and event["branch"] == "caller" for event in lab.artifacts.timeline)
    assert any(event["event"] == "branch_finished" and event["branch"] == "callee" for event in lab.artifacts.timeline)
    assert any(
        event["event"] == "marker_detected" and event["marker"] == "marker-parallel" for event in lab.artifacts.timeline
    )


def test_record_during_playback_starts_recording_before_playback(tmp_path: Path) -> None:
    """Verify overlap recording starts before playback begins.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    backend = TimingFakeSipBackend()
    lab = _lab(tmp_path, backend=backend)
    scenario = parse_scenario(
        {
            "name": "overlap-audio",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                {"action": "answer", "call": "bob_incoming"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
                {
                    "action": "record_during_playback",
                    "playback_call": "alice_to_bob",
                    "recording_call": "bob_incoming",
                    "text": "hello",
                    "marker": "marker-overlap",
                    "pre_roll": 0.01,
                    "post_roll": 0,
                    "save_as": "bob_recording",
                },
            ],
        }
    )

    async def run() -> None:
        """Run the overlap media scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    event_times = {name: timestamp for name, timestamp in backend.media_events}
    assert event_times["record_start"] < event_times["play"]
    assert lab.recordings["bob_recording"].exists()


def test_record_during_playback_auto_duration_stops_after_playback(tmp_path: Path) -> None:
    """Verify automatic overlap recording ends after playback returns.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    backend = CancellableRecordSipBackend()
    lab = _lab(tmp_path, backend=backend)
    scenario = parse_scenario(
        {
            "name": "overlap-auto-duration",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                {"action": "answer", "call": "bob_incoming"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
                {
                    "action": "record_during_playback",
                    "playback_call": "alice_to_bob",
                    "recording_call": "bob_incoming",
                    "text": "hello",
                    "marker": "marker-overlap",
                    "pre_roll": 0.01,
                    "post_roll": 0.01,
                    "save_as": "bob_recording",
                    "assert_marker": False,
                },
            ],
        }
    )

    async def run() -> None:
        """Run the automatic-duration overlap scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert backend.record_cancelled
    assert backend.requested_record_seconds is not None
    assert backend.requested_record_seconds > 5.0
    assert lab.recordings["bob_recording"].exists()


def test_parallel_branch_failure_records_failed_branch(tmp_path: Path) -> None:
    """Verify parallel failures identify the branch that failed.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "parallel-failure",
            "steps": [
                {
                    "action": "parallel",
                    "branches": {
                        "broken": [{"action": "wait_state", "call": "missing", "state": "connected"}],
                        "other": [{"action": "register", "client": "alice"}],
                    },
                }
            ],
        }
    )

    async def run() -> None:
        """Run the failing parallel scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    with pytest.raises(ScenarioError, match="broken"):
        asyncio.run(run())
    assert any(event["event"] == "branch_failed" and event["branch"] == "broken" for event in lab.artifacts.timeline)
    assert any(event["event"] == "parallel_finished" and event["failed"] for event in lab.artifacts.timeline)


def test_fake_backend_runs_attended_transfer(tmp_path: Path) -> None:
    """Verify the fake backend models attended transfer state.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "transfer",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "primary"},
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                {"action": "answer", "call": "bob_incoming"},
                {
                    "action": "consult_call",
                    "client": "alice",
                    "target": "webex_user",
                    "save_as": "consult",
                },
                {
                    "action": "attended_transfer",
                    "primary_call": "primary",
                    "consult_call": "consult",
                },
            ],
        }
    )

    async def run() -> None:
        """Run the transfer scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["primary"].state == "transferred"


def test_fake_backend_pairs_numeric_extension_to_owner(tmp_path: Path) -> None:
    """Verify owned numeric extensions produce paired fake inbound calls.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "extension-pairing",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "7108", "save_as": "alice_to_bob"},
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                {"action": "answer", "call": "bob_incoming"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
            ],
        }
    )

    async def run() -> None:
        """Run the extension-pairing scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["bob_incoming"].client_name == "bob"


def test_fake_backend_pairs_named_target_extension_to_owner(tmp_path: Path) -> None:
    """Verify client-name targets can dial the target client's extension.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "named-extension-pairing",
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {
                    "action": "call",
                    "client": "alice",
                    "target": "bob",
                    "use_target_extension": True,
                    "save_as": "alice_to_bob",
                },
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
                {"action": "answer", "call": "bob_incoming"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
            ],
        }
    )

    async def run() -> None:
        """Run the named-extension pairing scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["alice_to_bob"].remote_uri == "sip:7108@registrar.example.invalid"
    assert lab.calls["bob_incoming"].client_name == "bob"


def test_endpoint_behavior_auto_answers_incoming_call(tmp_path: Path) -> None:
    """Verify a behavior endpoint can answer without explicit callee steps.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-auto-answer",
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
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
                {"action": "wait_media", "call": "bob.inbound"},
            ],
        }
    )

    async def run() -> None:
        """Run the auto-answer behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["bob.inbound"].state == "connected"
    assert any(event["event"] == "behavior_started" and event["endpoint"] == "bob" for event in lab.artifacts.timeline)
    assert any(
        event["event"] == "behavior_transition" and event["to_state"] == "connected" for event in lab.artifacts.timeline
    )


def test_endpoint_behavior_timer_holds_and_resumes_call(tmp_path: Path) -> None:
    """Verify behavior timers can drive hold and resume actions.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-hold-resume",
            "behaviors": {
                "answer_hold_resume": {
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
                                    "state": "connected",
                                    "actions": [
                                        {
                                            "action": "start_timer",
                                            "name": "hold_delay",
                                            "seconds": 0.01,
                                        }
                                    ],
                                },
                                "timer_expired": {
                                    "timer": "hold_delay",
                                    "actions": [
                                        {"action": "hold", "call": "inbound"},
                                        {
                                            "action": "start_timer",
                                            "name": "resume_delay",
                                            "seconds": 0.01,
                                        },
                                    ],
                                    "next_state": "held",
                                },
                            }
                        },
                        "held": {
                            "on": {
                                "timer_expired": {
                                    "timer": "resume_delay",
                                    "actions": [{"action": "resume", "call": "inbound"}],
                                    "next_state": "connected",
                                }
                            }
                        },
                    },
                }
            },
            "endpoints": {"bob": {"client": "bob", "behavior": "answer_hold_resume"}},
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected"},
                {"action": "wait_state", "call": "bob.inbound", "state": "held", "timeout": 2},
                {"action": "wait_state", "call": "bob.inbound", "state": "connected", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the timer behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["bob.inbound"].state == "connected"
    assert any(
        event["event"] == "call_held" and event["call"] == lab.calls["bob.inbound"].id
        for event in lab.artifacts.timeline
    )
    assert any(
        event["event"] == "call_resumed" and event["call"] == lab.calls["bob.inbound"].id
        for event in lab.artifacts.timeline
    )


def test_endpoint_behavior_registers_and_places_outbound_call_from_entry(tmp_path: Path) -> None:
    """Verify entry actions can register an endpoint and place an outbound call.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-owned-outbound-call",
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
                        "connected": {
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "outbound_caller"}},
            "steps": [
                {
                    "action": "wait_behavior_state",
                    "endpoint": "alice",
                    "state": "connected",
                    "timeout": 2,
                }
            ],
        }
    )

    async def run() -> None:
        """Run the behavior-owned outbound scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert "alice" in lab.registrations
    assert lab.calls["alice.outbound"].client_name == "alice"
    assert lab.calls["alice.outbound"].state == "connected"
    assert any(
        event["event"] == "behavior_state_seen" and event["state"] == "connected" for event in lab.artifacts.timeline
    )
    assert any(
        event["event"] == "behavior_transition"
        and event["from_state"] == "unregistered"
        and event["to_state"] == "idle"
        for event in lab.artifacts.timeline
    )
    assert any(
        event["event"] == "behavior_transition" and event["from_state"] == "idle" and event["to_state"] == "connected"
        for event in lab.artifacts.timeline
    )


def test_endpoint_behavior_trigger_barrier_starts_call_after_callee_ready(tmp_path: Path) -> None:
    """Verify scripted triggers can start behavior calls after readiness waits.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    messages: list[str] = []
    lab = _lab(tmp_path, progress_reporter=messages.append)
    scenario = parse_scenario(
        {
            "name": "behavior-trigger-barrier",
            "behaviors": {
                "caller": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "start_call",
                                    "actions": [
                                        {
                                            "action": "call",
                                            "target": "bob",
                                            "use_target_extension": True,
                                            "save_as": "outbound",
                                        }
                                    ],
                                    "next_state": "calling",
                                }
                            }
                        },
                        "calling": {
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            }
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                },
                "callee": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {"on": {"call_received": {"save_call_as": "inbound", "next_state": "ringing"}}},
                        "ringing": {
                            "entry": [{"action": "answer", "call": "inbound"}],
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            },
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                },
            },
            "endpoints": {
                "alice": {"client": "alice", "behavior": "caller"},
                "bob": {"client": "bob", "behavior": "callee"},
            },
            "steps": [
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "idle", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "start_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "connected", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "connected", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the trigger-barrier behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    asyncio.run(run())
    assert lab.calls["alice.outbound"].state == "connected"
    assert lab.calls["bob.inbound"].state == "connected"

    def first_event_index(**criteria: object) -> int:
        """Return the first timeline index matching all criteria.

        :param criteria: Event key/value pairs to match.
        :returns: Zero-based timeline index.
        :raises AssertionError: If no event matches.
        """

        for index, event in enumerate(lab.artifacts.timeline):
            if all(event.get(key) == value for key, value in criteria.items()):
                return index
        raise AssertionError(f"Missing event matching {criteria!r}")

    alice_ready_index = first_event_index(event="behavior_state_seen", endpoint="alice", state="idle")
    bob_ready_index = first_event_index(event="behavior_state_seen", endpoint="bob", state="idle")
    trigger_step_index = first_event_index(event="step_started", action="trigger_behavior")
    call_placed_index = first_event_index(event="call_placed", client="alice")

    assert alice_ready_index < trigger_step_index
    assert bob_ready_index < trigger_step_index
    assert trigger_step_index < call_placed_index
    assert any(
        event["event"] == "behavior_event"
        and event["endpoint"] == "alice"
        and event["trigger"] == "scenario_trigger"
        and event["trigger_name"] == "start_call"
        for event in lab.artifacts.timeline
    )
    assert "behavior alice/caller state=unregistered event=entry next_state=unregistered action=register" in messages
    assert "behavior bob/callee state=unregistered event=registered next_state=idle action=-" in messages
    assert (
        "behavior alice/caller state=idle event=scenario_trigger:start_call next_state=calling action=call" in messages
    )
    assert "behavior alice/caller state=calling event=call_state:connected next_state=connected action=-" in messages


def test_endpoint_behavior_failure_cancels_scenario(tmp_path: Path) -> None:
    """Verify behavior action failures surface with endpoint context.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-failure",
            "behaviors": {
                "broken": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "actions": [{"action": "answer", "call": "missing"}],
                                }
                            }
                        }
                    },
                }
            },
            "endpoints": {"bob": {"client": "bob", "behavior": "broken"}},
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "wait_state", "call": "alice_to_bob", "state": "connected", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the failing behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    with pytest.raises(ScenarioError, match="missing"):
        asyncio.run(run())
    assert any(event["event"] == "behavior_failed" and event["endpoint"] == "bob" for event in lab.artifacts.timeline)


def test_endpoint_behavior_rejects_duplicate_call_alias_from_entry(tmp_path: Path) -> None:
    """Verify behavior-owned calls cannot overwrite scoped call aliases.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-duplicate-alias",
            "behaviors": {
                "looping_caller": {
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
                                    "next_state": "idle",
                                }
                            },
                        },
                        "done": {
                            "on": {
                                "timer_expired": {
                                    "timer": "unused",
                                }
                            }
                        },
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "looping_caller"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "done", "timeout": 2}],
        }
    )

    async def run() -> None:
        """Run the duplicate-alias behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    with pytest.raises(ScenarioError, match="call alias already exists"):
        asyncio.run(run())


def test_endpoint_behavior_reuses_disconnected_call_aliases_for_loop(tmp_path: Path) -> None:
    """Verify looping endpoint behaviors can intentionally reuse call aliases.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-reuse-call-aliases",
            "behaviors": {
                "caller": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "place_call",
                                    "actions": [
                                        {
                                            "action": "call",
                                            "target": "bob",
                                            "use_target_extension": True,
                                            "save_as": "outbound",
                                            "reuse_alias": True,
                                        }
                                    ],
                                    "next_state": "calling",
                                }
                            }
                        },
                        "calling": {
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            }
                        },
                        "connected": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "hangup_call",
                                    "actions": [{"action": "hangup", "call": "outbound"}],
                                    "next_state": "disconnecting",
                                }
                            }
                        },
                        "disconnecting": {
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                },
                "callee": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "reuse_alias": True,
                                    "next_state": "ringing",
                                }
                            }
                        },
                        "ringing": {
                            "entry": [{"action": "answer", "call": "inbound"}],
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            },
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                },
            },
            "endpoints": {
                "alice": {"client": "alice", "behavior": "caller"},
                "bob": {"client": "bob", "behavior": "callee"},
            },
            "steps": [
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "idle", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "place_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "connected", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "connected", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "hangup_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "idle", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "place_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "connected", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "connected", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the alias-reuse behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    asyncio.run(run())

    assert lab.calls["alice.outbound"].state == "connected"
    assert lab.calls["bob.inbound"].state == "connected"
    reused_aliases = {
        event["call_alias"] for event in lab.artifacts.timeline if event["event"] == "behavior_call_alias_reused"
    }
    assert reused_aliases == {"alice.outbound", "bob.inbound"}


def test_endpoint_behavior_rejects_active_call_alias_reuse(tmp_path: Path) -> None:
    """Verify alias reuse cannot hide an active behavior-owned call.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-active-alias-reuse",
            "behaviors": {
                "caller": {
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
                                    "reuse_alias": True,
                                }
                            ],
                            "on": {
                                "call_state": {
                                    "call": "outbound",
                                    "state": "connected",
                                    "next_state": "idle",
                                }
                            },
                        },
                        "done": {
                            "on": {
                                "timer_expired": {
                                    "timer": "unused",
                                }
                            }
                        },
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "caller"}},
            "steps": [{"action": "wait_behavior_state", "endpoint": "alice", "state": "done", "timeout": 2}],
        }
    )

    async def run() -> None:
        """Run the active-alias-reuse scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    with pytest.raises(ScenarioError, match="cannot reuse active call alias"):
        asyncio.run(run())


def test_endpoint_behavior_counter_terminates_call_loop(tmp_path: Path) -> None:
    """Verify counters terminate a call loop without placing an extra call.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    messages: list[str] = []
    lab = _lab(tmp_path, progress_reporter=messages.append)
    scenario = parse_scenario(
        {
            "name": "behavior-counter-loop",
            "behaviors": {
                "caller": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "entry": [{"action": "set_counter", "name": "calls_remaining", "value": 2}],
                            "on": {"scenario_trigger": {"name": "place_call", "next_state": "loop"}},
                        },
                        "loop": {
                            "entry": [
                                {
                                    "action": "call",
                                    "target": "bob",
                                    "use_target_extension": True,
                                    "save_as": "outbound",
                                    "reuse_alias": True,
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
                        "connected": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "hangup_call",
                                    "actions": [{"action": "hangup", "call": "outbound"}],
                                },
                                "call_state": {
                                    "call": "outbound",
                                    "state": "disconnected",
                                    "actions": [{"action": "decrement_counter", "name": "calls_remaining"}],
                                    "next_state": "loop",
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
                },
                "callee": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "reuse_alias": True,
                                    "next_state": "ringing",
                                }
                            }
                        },
                        "ringing": {
                            "entry": [{"action": "answer", "call": "inbound"}],
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "connected",
                                    "next_state": "connected",
                                }
                            },
                        },
                        "connected": {
                            "on": {
                                "call_state": {
                                    "call": "inbound",
                                    "state": "disconnected",
                                    "next_state": "idle",
                                }
                            }
                        },
                    },
                },
            },
            "endpoints": {
                "alice": {"client": "alice", "behavior": "caller"},
                "bob": {"client": "bob", "behavior": "callee"},
            },
            "steps": [
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "idle", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "place_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "connected", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "connected", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "hangup_call"},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "connected", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "connected", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "hangup_call"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "done", "timeout": 2},
                {"action": "wait_behavior_state", "endpoint": "bob", "state": "idle", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the counter loop scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    asyncio.run(run())

    assert sum(1 for event in lab.artifacts.timeline if event["event"] == "call_placed") == 2
    assert lab.calls["alice.outbound"].state == "disconnected"
    counter_values = [
        event["value"]
        for event in lab.artifacts.timeline
        if event["event"] == "behavior_counter_updated" and event["counter"] == "calls_remaining"
    ]
    assert counter_values == [2, 1, 0]
    assert any(
        event["event"] == "behavior_event"
        and event["trigger"] == "counter_reached"
        and event["counter"] == "calls_remaining"
        and event["counter_value"] == 0
        for event in lab.artifacts.timeline
    )
    assert (
        "behavior alice/caller state=connected event=counter_reached:calls_remaining=0 next_state=done action=-"
    ) in messages


def test_endpoint_behavior_unknown_counter_fails_with_context(tmp_path: Path) -> None:
    """Verify counter mutations require an existing counter unless setting it.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-unknown-counter",
            "behaviors": {
                "caller": {
                    "initial_state": "unregistered",
                    "states": {
                        "unregistered": {
                            "entry": [{"action": "register"}],
                            "on": {"registered": {"next_state": "idle"}},
                        },
                        "idle": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "decrement",
                                    "actions": [{"action": "decrement_counter", "name": "missing"}],
                                }
                            }
                        },
                        "done": {"entry": [{"action": "cancel_timer", "name": "unused"}]},
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "caller"}},
            "steps": [
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "idle", "timeout": 2},
                {"action": "trigger_behavior", "endpoint": "alice", "name": "decrement"},
                {"action": "wait_behavior_state", "endpoint": "alice", "state": "done", "timeout": 2},
            ],
        }
    )

    async def run() -> None:
        """Run the unknown-counter scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    with pytest.raises(ScenarioError, match="endpoint 'alice' action 1 \\(decrement_counter\\).*missing"):
        asyncio.run(run())


def test_endpoint_behavior_rejects_unknown_scripted_trigger(tmp_path: Path) -> None:
    """Verify scripted triggers must target a trigger defined by the behavior.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-unknown-trigger",
            "behaviors": {
                "idle": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "scenario_trigger": {
                                    "name": "known",
                                }
                            }
                        }
                    },
                }
            },
            "endpoints": {"alice": {"client": "alice", "behavior": "idle"}},
            "steps": [{"action": "trigger_behavior", "endpoint": "alice", "name": "missing"}],
        }
    )

    async def run() -> None:
        """Run the unknown-trigger scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    with pytest.raises(ScenarioError, match="unknown trigger"):
        asyncio.run(run())


def test_endpoint_behavior_rejects_explicit_incoming_conflict(tmp_path: Path) -> None:
    """Verify behavior and explicit incoming waits cannot consume the same client.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-conflict",
            "behaviors": {
                "auto_answer": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "call_received": {
                                    "save_call_as": "inbound",
                                    "actions": [{"action": "answer", "call": "inbound"}],
                                }
                            }
                        }
                    },
                }
            },
            "endpoints": {"bob": {"client": "bob", "behavior": "auto_answer"}},
            "steps": [
                {"action": "register", "clients": ["alice", "bob"]},
                {"action": "call", "client": "alice", "target": "bob", "save_as": "alice_to_bob"},
                {"action": "expect_incoming", "client": "bob", "save_as": "bob_incoming"},
            ],
        }
    )

    async def run() -> None:
        """Run the conflicting incoming-consumer scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    with pytest.raises(ScenarioError, match="expect_incoming"):
        asyncio.run(run())


def test_endpoint_behavior_rejects_unknown_client(tmp_path: Path) -> None:
    """Verify endpoint behavior assignments must name configured clients.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "behavior-unknown-client",
            "behaviors": {
                "idle": {
                    "initial_state": "idle",
                    "states": {
                        "idle": {
                            "on": {
                                "media_active": {},
                            }
                        }
                    },
                }
            },
            "endpoints": {"ghost": {"client": "ghost", "behavior": "idle"}},
            "steps": [{"action": "register", "client": "alice"}],
        }
    )

    async def run() -> None:
        """Run the unknown-client behavior scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    with pytest.raises(ScenarioError, match="unknown client"):
        asyncio.run(run())


def test_progress_reporter_receives_step_and_call_lifecycle_messages(tmp_path: Path) -> None:
    """Verify progress reporting includes steps and call lifecycle events.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    messages: list[str] = []
    lab = _lab(tmp_path, progress_reporter=messages.append)
    scenario = parse_scenario(
        {
            "name": "progress",
            "steps": [
                {"action": "register", "clients": ["alice"]},
                {"action": "call", "client": "alice", "target": "webex_user", "save_as": "outbound"},
                {"action": "wait_state", "call": "outbound", "state": "connected"},
                {"action": "hangup", "call": "outbound"},
            ],
        }
    )

    async def run() -> None:
        """Run the progress scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert messages == [
        "step 1/4: register",
        "step 2/4: call",
        "call initiated: alice -> sip:webex@example.invalid (outbound)",
        "call established: outbound alice <-> sip:webex@example.invalid",
        "step 3/4: wait_state",
        "step 4/4: hangup",
        "call ended: outbound alice <-> sip:webex@example.invalid",
    ]


def test_fake_backend_video_smoke_skips_for_opaque_target(tmp_path: Path) -> None:
    """Verify fake video smoke probes skip opaque targets.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "video",
            "steps": [
                {"action": "register", "clients": ["alice"]},
                {"action": "video_smoke", "client": "alice", "target": "webex_user"},
            ],
        }
    )

    async def run() -> None:
        """Run the video smoke scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert any(event["event"] == "video_smoke" and event["skipped"] for event in lab.artifacts.timeline)


def test_register_step_can_stay_registered_for_explicit_duration(tmp_path: Path) -> None:
    """Verify explicit registration soak duration records a wait event.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)
    scenario = parse_scenario(
        {
            "name": "registration-watch",
            "steps": [
                {
                    "action": "register",
                    "client": "alice",
                    "stay_registered_for": 0.01,
                    "require_reregistration": True,
                }
            ],
        }
    )

    async def run() -> None:
        """Run the registration watch scenario inside a managed lab.

        :returns: None.
        """

        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert any(
        event["event"] == "registration_wait_finished"
        and event["client"] == "alice"
        and event["refreshes_observed"] == 1
        for event in lab.artifacts.timeline
    )


def test_numeric_extension_targets_resolve_against_calling_client_registrar(tmp_path: Path) -> None:
    """Verify numeric targets resolve against the caller registrar host.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    lab = _lab(tmp_path)

    assert lab.resolve_call_target("alice", "bob") == "sip:bob@example.invalid"
    assert lab.resolve_call_target("alice", "bob", use_target_extension=True) == "sip:7108@registrar.example.invalid"
    assert lab.resolve_call_target("alice", "7109") == "sip:7109@registrar.example.invalid"
    assert lab.resolve_call_target("charlie", "7109") == "sip:7109@registrar.example.invalid"
    assert lab.resolve_call_target("alice", "sip:7109@example.invalid") == "sip:7109@example.invalid"
    assert lab.config.client_name_for_extension_uri("sip:7108@registrar.example.invalid") == "bob"

    with pytest.raises(ScenarioError, match="no configured extension"):
        lab.resolve_call_target("alice", "charlie", use_target_extension=True)

    with pytest.raises(ScenarioError, match="configured SIP client"):
        lab.resolve_call_target("alice", "webex_user", use_target_extension=True)


def _lab(
    tmp_path: Path,
    progress_reporter: Callable[[str], None] | None = None,
    backend: FakeSipBackend | None = None,
) -> CallLab:
    """Build a fake call lab for orchestrator tests.

    :param tmp_path: Temporary pytest directory.
    :param progress_reporter: Optional progress callback.
    :param backend: Optional fake backend implementation.
    :returns: Configured call lab with fake backend and deterministic media.
    """

    config = LabConfig(
        clients=(
            SipClientConfig(
                name="alice",
                id_uri="sip:alice@example.invalid",
                registrar_uri="sip:registrar.example.invalid",
                username_env="ALICE_USER",
                password_env="ALICE_PASS",
            ),
            SipClientConfig(
                name="bob",
                id_uri="sip:bob@example.invalid",
                registrar_uri="sip:registrar.example.invalid",
                username_env="BOB_USER",
                password_env="BOB_PASS",
                extension="7108",
            ),
            SipClientConfig(
                name="charlie",
                id_uri="sip:charlie@example.invalid",
                registrar_uri="sip:registrar.example.invalid",
                username_env="CHARLIE_USER",
                password_env="CHARLIE_PASS",
                transport="udp",
            ),
        ),
        targets=(),
        artifacts_dir=tmp_path / "artifacts",
        env={
            "ALICE_USER": "alice",
            "ALICE_PASS": "secret",
            "BOB_USER": "bob",
            "BOB_PASS": "secret",
            "CHARLIE_USER": "charlie",
            "CHARLIE_PASS": "secret",
        },
    )
    config = LabConfig(
        clients=config.clients,
        targets=(type("Target", (), {"name": "webex_user", "uri": "sip:webex@example.invalid"})(),),
        artifacts_dir=config.artifacts_dir,
        env=config.env,
    )
    return CallLab(
        config=config,
        backend=backend or FakeSipBackend(),
        artifact_writer=ArtifactWriter(config.artifacts_dir, run_id="test-run"),
        media_factory=MarkerMediaFactory(tmp_path / "media"),
        progress_reporter=progress_reporter,
    )
