from __future__ import annotations

from pathlib import Path

import pytest

from wxcalls.artifacts import ArtifactWriter
from wxcalls.backends.fake import FakeSipBackend
from wxcalls.config import LabConfig, SipClientConfig
from wxcalls.media import MediaAsset, generate_marker_tone
from wxcalls.orchestrator import CallLab
from wxcalls.scenario import parse_scenario


class MarkerMediaFactory:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def prepare_tts(self, text: str, marker: str | None = None, voice: str | None = None) -> MediaAsset:
        path = self.work_dir / "tts.wav"
        generate_marker_tone(marker or "default-marker", path)
        return MediaAsset(path=path, marker=marker)

    def prepare_wav(self, source: Path, marker: str | None = None) -> MediaAsset:
        return MediaAsset(path=source, marker=marker)


@pytest.mark.parametrize("use_hold_resume", [False, True])
def test_fake_backend_runs_audio_marker_scenario(tmp_path: Path, use_hold_resume: bool) -> None:
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
        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert (tmp_path / "artifacts" / "test-run" / "logs" / "timeline.json").exists()


def test_fake_backend_runs_attended_transfer(tmp_path: Path) -> None:
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
        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert lab.calls["primary"].state == "transferred"


def test_fake_backend_video_smoke_skips_for_opaque_target(tmp_path: Path) -> None:
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
        async with lab:
            await lab.run_scenario(scenario)

    import asyncio

    asyncio.run(run())
    assert any(event["event"] == "video_smoke" and event["skipped"] for event in lab.artifacts.timeline)


def test_register_step_can_stay_registered_for_explicit_duration(tmp_path: Path) -> None:
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
    lab = _lab(tmp_path)

    assert lab.resolve_call_target("alice", "7109") == "sip:7109@registrar.example.invalid"
    assert lab.resolve_call_target("charlie", "7109") == "sip:7109@registrar.example.invalid"
    assert lab.resolve_call_target("alice", "sip:7109@example.invalid") == "sip:7109@example.invalid"


def _lab(tmp_path: Path) -> CallLab:
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
        backend=FakeSipBackend(),
        artifact_writer=ArtifactWriter(config.artifacts_dir, run_id="test-run"),
        media_factory=MarkerMediaFactory(tmp_path / "media"),
    )
