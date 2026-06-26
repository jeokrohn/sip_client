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

    lab = _lab(tmp_path)
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

    lab = _lab(tmp_path)
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
