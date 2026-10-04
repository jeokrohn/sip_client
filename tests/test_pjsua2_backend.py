from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wxcalls.backends import pjsua2 as pjsua2_backend
from wxcalls.backends.base import CallHandle, RegistrationResult
from wxcalls.backends.pjsua2 import (
    Pjsua2SipBackend,
    _configure_srtp,
    _disable_audio_codecs,
    _is_active_media_status,
    _local_gateway_caller_uri,
    _prioritize_audio_codecs,
    _reroute_audio_players,
    _reroute_audio_recorders,
    _set_call_media_counts,
    _validate_secure_signaling_for_mandatory_srtp,
)
from wxcalls.config import SipClientConfig, SipCredentials
from wxcalls.exceptions import BackendError
from wxcalls.media import create_silence_wav


def test_local_gateway_caller_uri_keeps_otg_inside_bracketed_uri() -> None:
    """Ensure a caller ID override serializes OTG as a URI parameter.

    :returns: None.
    """

    client = SipClientConfig(
        name="lgw1",
        id_uri="sip:line@registrar.example.invalid;otg=trunk",
        registrar_uri="sip:registrar.example.invalid:5061",
        username_env="LGW_USER",
        password_env="LGW_PASSWORD",
        kind="local_gateway",
        registrar_domain="registrar.example.invalid",
        trunk_group="trunk",
    )

    assert _local_gateway_caller_uri(client, "+442074037970") == (
        "<sip:+442074037970@registrar.example.invalid;otg=trunk>"
    )


class FakePj:
    """Minimal PJSUA2 constants and value objects used by backend tests."""

    PJMEDIA_SRTP_MANDATORY = 2
    PJMEDIA_SRTP_KEYING_SDES = 0
    PJSUA_CALL_MEDIA_ACTIVE = 1

    class SrtpCrypto:
        def __init__(self) -> None:
            """Create fake SRTP crypto config.

            :returns: None.
            """

            self.name = ""

    class SipHeader:
        """Capture custom SIP header values for tests."""

        def __init__(self) -> None:
            """Create an empty fake SIP header.

            :returns: None.
            """

            self.hName = ""
            self.hValue = ""


class FakeMediaConfig:
    def __init__(self) -> None:
        """Create fake account media configuration.

        :returns: None.
        """

        self.srtpUse = 0
        self.srtpSecureSignaling = 0
        self.srtpOpt = FakeSrtpOpt()


class FakeSrtpOpt:
    def __init__(self) -> None:
        """Create fake SRTP option vectors.

        :returns: None.
        """

        self.cryptos = []
        self.keyings = []


class FakeAccountConfig:
    def __init__(self) -> None:
        """Create fake account configuration.

        :returns: None.
        """

        self.mediaConfig = FakeMediaConfig()


def test_register_local_gateway_configures_identity_auth_proxy_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify LGW account fields configure independent identity and Digest credentials.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    captured: dict[str, object] = {}

    class FakeRegistrationPj(FakePj):
        """Provide fake PJSUA2 constructors for registration account tests."""

        @staticmethod
        def AccountConfig() -> SimpleNamespace:  # noqa: N802 - PJSUA2 API name
            """Build a mutable fake PJSUA2 account config.

            :returns: Account config with registration, signaling, and media fields.
            """

            return SimpleNamespace(
                idUri="",
                regConfig=SimpleNamespace(
                    registrarUri="",
                    timeoutSec=0,
                    delayBeforeRefreshSec=0,
                    headers=[],
                ),
                sipConfig=SimpleNamespace(proxies=[], authCreds=[], transportId=0),
                mediaConfig=FakeMediaConfig(),
                videoConfig=SimpleNamespace(),
            )

        @staticmethod
        def AuthCredInfo(*args: object) -> tuple[object, ...]:  # noqa: N802 - PJSUA2 API name
            """Capture the PJSUA2 digest credential arguments.

            :param args: Credential constructor arguments.
            :returns: Captured credential arguments.
            """

            return args

    class FakeRegistrationAccount:
        """Capture registration configuration without opening a network socket."""

        def create(self, account_config: object) -> None:
            """Save the generated PJSUA2 account configuration.

            :param account_config: Generated account configuration.
            :returns: None.
            """

            captured["account_config"] = account_config

        async def wait_registered(self, timeout: float) -> RegistrationResult:
            """Return a simulated successful registration result.

            :param timeout: Ignored registration timeout.
            :returns: Simulated successful registration result.
            """

            return RegistrationResult(client_name="lgw1", expires=120, metadata={"code": 200})

    backend = Pjsua2SipBackend()
    backend.pj = FakeRegistrationPj
    backend.endpoint = object()
    client = SipClientConfig(
        name="lgw1",
        id_uri="sip:line_lgu@registrar.example.invalid;otg=trunk_lgu",
        registrar_uri="sip:registrar.example.invalid:5061",
        username_env="LGW_USER",
        password_env="LGW_PASSWORD",
        proxy_uri="sips:proxy.example.invalid;lr",
        transport="tls",
        kind="local_gateway",
    )
    backend.transport_ids["tls"] = 42

    async def run() -> RegistrationResult:
        """Register the fake Local Gateway account.

        :returns: Simulated successful registration result.
        """

        backend.loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            pjsua2_backend,
            "_create_account_adapter",
            lambda backend_, name: FakeRegistrationAccount(),
        )
        return await backend.register_client(
            client,
            SipCredentials(username="auth_user", password="auth_password"),
        )

    result = asyncio.run(run())
    account_config = cast(Any, captured["account_config"])

    assert result.expires == 120
    assert account_config.idUri == "sip:line_lgu@registrar.example.invalid;otg=trunk_lgu"
    assert account_config.regConfig.registrarUri == "sip:registrar.example.invalid:5061"
    assert account_config.regConfig.timeoutSec == 240
    assert account_config.regConfig.delayBeforeRefreshSec == 60
    assert [(header.hName, header.hValue) for header in account_config.regConfig.headers] == [("Supported", "path")]
    assert account_config.sipConfig.proxies == ["sips:proxy.example.invalid;lr"]
    assert account_config.sipConfig.authCreds == [("digest", "BroadWorks", "auth_user", 0, "auth_password")]
    assert account_config.sipConfig.transportId == 42


def test_registration_rejection_reports_sip_status() -> None:
    """Verify a rejected registration surfaces its SIP response instead of only timing out.

    :returns: None.
    """

    class FakeCallbackPj:
        """Provide a minimal PJSUA2 account base for callback testing."""

        class Account:
            """Provide account info for a failed registration callback."""

            def __init__(self) -> None:
                """Create a fake account base.

                :returns: None.
                """

            def getInfo(self) -> SimpleNamespace:  # noqa: N802 - PJSUA2 API name
                """Return inactive registration state.

                :returns: Fake account info.
                """

                return SimpleNamespace(regIsActive=False)

    async def run() -> None:
        """Exercise an unauthorized registration callback.

        :returns: None.
        """

        backend = Pjsua2SipBackend()
        backend.pj = FakeCallbackPj
        backend.loop = asyncio.get_running_loop()
        account = pjsua2_backend._create_account_adapter(backend, "lgw1")
        account.onRegState(SimpleNamespace(code=401, reason="Unauthorized", expiration=0))

        with pytest.raises(BackendError, match="SIP 401 Unauthorized"):
            await account.wait_registered(0.001)

    asyncio.run(run())


class FakeCodecEndpoint:
    def __init__(self, codecs: list[object]) -> None:
        """Create a fake endpoint with configurable codecs.

        :param codecs: Fake codec info objects.
        :returns: None.
        """

        self.codecs = codecs
        self.priorities: dict[str, int] = {}

    def codecEnum2(self) -> list[object]:  # noqa: N802 - PJSUA2 API name
        """Return fake endpoint codec info.

        :returns: Fake codec info objects.
        """

        return self.codecs

    def codecSetPriority(self, codec_id: str, priority: int) -> None:  # noqa: N802 - PJSUA2 API name
        """Record a codec priority update.

        :param codec_id: Codec ID.
        :param priority: Requested codec priority.
        :returns: None.
        """

        self.priorities[codec_id] = priority


def test_configure_srtp_requires_secure_media() -> None:
    """Verify SRTP configuration enforces secure media settings.

    :returns: None.
    """

    account_config = FakeAccountConfig()

    _configure_srtp(account_config, FakePj)

    assert account_config.mediaConfig.srtpUse == FakePj.PJMEDIA_SRTP_MANDATORY
    assert account_config.mediaConfig.srtpSecureSignaling == 0
    assert [crypto.name for crypto in account_config.mediaConfig.srtpOpt.cryptos] == ["AES_CM_128_HMAC_SHA1_80"]
    assert account_config.mediaConfig.srtpOpt.keyings == [FakePj.PJMEDIA_SRTP_KEYING_SDES]


def test_configure_srtp_requires_binding_support() -> None:
    """Verify missing SRTP binding support raises a backend error.

    :returns: None.
    """

    account_config = object()

    with pytest.raises(BackendError, match="SRTP"):
        _configure_srtp(account_config, object())


def test_disable_audio_codecs_sets_matching_priorities() -> None:
    """Verify configured codec patterns are disabled on the PJSUA2 endpoint.

    :returns: None.
    """

    endpoint = FakeCodecEndpoint(
        [
            SimpleNamespace(codecId="speex/16000/1"),
            SimpleNamespace(codecId="PCMU/8000/1"),
            SimpleNamespace(codecId="Speex/8000/1"),
        ]
    )

    disabled = _disable_audio_codecs(endpoint, ("speex/*",))

    assert disabled == ["speex/16000/1", "Speex/8000/1"]
    assert endpoint.priorities == {"speex/16000/1": 0, "Speex/8000/1": 0}


def test_disable_audio_codecs_requires_binding_support() -> None:
    """Verify codec configuration fails when endpoint controls are missing.

    :returns: None.
    """

    with pytest.raises(BackendError, match="codec priority"):
        _disable_audio_codecs(object(), ("speex/*",))


def test_prioritize_audio_codecs_sets_matching_priorities() -> None:
    """Verify preferred codecs receive explicit endpoint priorities.

    :returns: None.
    """

    endpoint = FakeCodecEndpoint(
        [
            SimpleNamespace(codecId="iLBC/8000/1"),
            SimpleNamespace(codecId="PCMU/8000/1"),
            SimpleNamespace(codecId="PCMA/8000/1"),
        ]
    )

    applied = _prioritize_audio_codecs(endpoint, (("PCMU/*", 255), ("PCMA/*", 254)))

    assert applied == {"PCMU/8000/1": 255, "PCMA/8000/1": 254}
    assert endpoint.priorities == {"PCMU/8000/1": 255, "PCMA/8000/1": 254}


def test_prioritize_audio_codecs_requires_binding_support() -> None:
    """Verify preferred codec configuration requires endpoint controls.

    :returns: None.
    """

    with pytest.raises(BackendError, match="codec priority"):
        _prioritize_audio_codecs(object(), (("PCMU/*", 255),))


def test_set_call_media_counts_disables_text_and_preserves_audio() -> None:
    """Verify media-count configuration preserves audio and disables text.

    :returns: None.
    """

    call_param = FakeCallParam()

    _set_call_media_counts(call_param, video_count=0, text_count=0)

    assert call_param.opt.audioCount == 1
    assert call_param.opt.videoCount == 0
    assert call_param.opt.textCount == 0


def test_place_call_returns_before_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify outgoing PJSUA2 calls do not block before paired inbound steps run.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    backend = Pjsua2SipBackend()
    backend.pj = FakeCallPlacementPj()
    backend.accounts["alice"] = object()
    pj_call = FakeOutgoingPjCall()
    monkeypatch.setattr(pjsua2_backend, "_create_call_adapter", lambda backend_, account: pj_call)

    call = asyncio.run(backend.place_call("alice", "sip:bob@example.invalid", timeout=0.01))

    assert call.state == "calling"
    assert pj_call.target_uri == "sip:bob@example.invalid"
    assert pj_call.waited_states == []


def test_wait_for_incoming_skips_disconnected_call() -> None:
    """Verify stale incoming calls are skipped before the scenario can answer them.

    :returns: None.
    """

    backend = Pjsua2SipBackend()
    stale = CallHandle(id="stale", client_name="bob", remote_uri="sip:alice", state="incoming")
    live = CallHandle(id="live", client_name="bob", remote_uri="sip:alice", state="incoming")
    backend.calls = {
        stale.id: FakeStatefulPjCall("DISCONNECTED"),
        live.id: FakeStatefulPjCall("INCOMING"),
    }

    async def run() -> CallHandle:
        """Queue stale and live inbound calls, then wait for the first usable one.

        :returns: Incoming call handle that is still answerable.
        """

        account = SimpleNamespace(incoming=asyncio.Queue())
        backend.accounts["bob"] = account
        await account.incoming.put(stale)
        await account.incoming.put(live)
        return await backend.wait_for_incoming("bob", timeout=0.1)

    assert asyncio.run(run()) is live
    assert stale.state == "disconnected"
    assert live.state == "incoming"


def test_answer_disconnected_call_raises_backend_error() -> None:
    """Verify dead incoming calls produce a framework error instead of a PJSUA2 error.

    :returns: None.
    """

    backend = Pjsua2SipBackend()
    backend.pj = FakeCallPlacementPj()
    call = CallHandle(id="call-1", client_name="bob", remote_uri="sip:alice", state="incoming")
    pj_call = FakeStatefulPjCall("DISCONNECTED")
    backend.calls[call.id] = pj_call

    with pytest.raises(BackendError, match="already disconnected"):
        asyncio.run(backend.answer(call))

    assert call.state == "disconnected"
    assert not pj_call.answered


def test_active_media_status_accepts_binding_constant_and_text() -> None:
    """Verify active media detection accepts constants and text values.

    :returns: None.
    """

    assert _is_active_media_status(FakePj, FakePj.PJSUA_CALL_MEDIA_ACTIVE)
    assert _is_active_media_status(FakePj, "PJSUA_CALL_MEDIA_ACTIVE")
    assert not _is_active_media_status(FakePj, 0)


def test_record_wav_finalizes_recorder_before_return(tmp_path: Path) -> None:
    """Verify recorder output is readable before ``record_wav`` returns.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    backend = _backend_with_fake_recording(stop_raises=True)
    output = tmp_path / "recording.wav"
    call = CallHandle(id="call-1", client_name="alice", remote_uri="sip:bob")

    asyncio.run(backend.record_wav(call, output, 0))

    recorder = backend.pj.created_recorders[0]
    assert output.read_bytes().startswith(b"RIFF")
    assert recorder.finalized
    assert backend.calls["call-1"].recorders == []


def test_record_wav_finalizes_recorder_when_cancelled(tmp_path: Path) -> None:
    """Verify cancelled recordings still stop and finalize their WAV artifact.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    backend = _backend_with_fake_recording(stop_raises=False)
    output = tmp_path / "recording.wav"
    call = CallHandle(id="call-1", client_name="alice", remote_uri="sip:bob")

    async def run() -> None:
        """Start and cancel a recording task.

        :returns: None.
        """

        task = asyncio.create_task(backend.record_wav(call, output, 30.0))
        while not backend.pj.created_recorders:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    recorder = backend.pj.created_recorders[0]
    assert output.read_bytes().startswith(b"RIFF")
    assert recorder.finalized
    assert recorder.stopped
    assert backend.calls["call-1"].recorders == []


def test_record_wav_rejects_unreadable_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify invalid recorder output is reported as a backend error.

    :param tmp_path: Temporary pytest directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    monkeypatch.setattr(pjsua2_backend, "_RECORDING_READY_TIMEOUT", 0.01)
    monkeypatch.setattr(pjsua2_backend, "_RECORDING_POLL_INTERVAL", 0.001)
    backend = _backend_with_fake_recording(stop_raises=False, finalizes=False)
    output = tmp_path / "recording.wav"
    call = CallHandle(id="call-1", client_name="alice", remote_uri="sip:bob")

    with pytest.raises(BackendError, match="readable WAV"):
        asyncio.run(backend.record_wav(call, output, 0))

    assert backend.calls["call-1"].recorders == []


def test_play_wav_waits_stops_and_releases_player(tmp_path: Path) -> None:
    """Verify WAV playback waits for EOF and releases player state.

    :param tmp_path: Temporary pytest directory.
    :returns: None.
    """

    backend = _backend_with_fake_recording(stop_raises=False)
    wav_path = tmp_path / "playback.wav"
    create_silence_wav(wav_path, seconds=0.01)
    call = CallHandle(id="call-1", client_name="alice", remote_uri="sip:bob", state="connected")

    asyncio.run(backend.play_wav(call, wav_path))

    player = backend.pj.created_players[0]
    assert player.path == str(wav_path)
    assert player.started
    assert player.stopped
    assert backend.calls["call-1"].players == []


def test_reroute_audio_players_reconnects_active_player() -> None:
    """Verify active audio players reroute to a changed media port.

    :returns: None.
    """

    old_media = FakeAudioMedia(stop_raises=False, port_id=1)
    new_media = FakeAudioMedia(stop_raises=False, port_id=3)
    player = FakeStreamingPlayer(audio_media=old_media)
    pj_call = FakePjCall(old_media)
    pj_call.players.append(player)

    _reroute_audio_players(pj_call, new_media)

    assert player.wxcalls_audio_media is new_media
    assert player.transmit_sinks == [new_media]
    assert player.wxcalls_transmitting
    assert not player.wxcalls_eof.is_set()


def test_reroute_audio_players_reconnects_when_port_id_was_reused() -> None:
    """Verify rerouting reconnects when the media wrapper is refreshed.

    :returns: None.
    """

    media = FakeAudioMedia(stop_raises=False, port_id=1)
    player = FakeStreamingPlayer(audio_media=media)
    pj_call = FakePjCall(media)
    pj_call.players.append(player)

    _reroute_audio_players(pj_call, media)

    assert player.transmit_sinks == [media]
    assert player.listeners == [1]


def test_reroute_audio_players_skips_existing_listener() -> None:
    """Verify rerouting skips a player already attached to the target port.

    :returns: None.
    """

    media = FakeAudioMedia(stop_raises=False, port_id=1)
    player = FakeStreamingPlayer(audio_media=media)
    player.listeners.append(1)
    pj_call = FakePjCall(media)
    pj_call.players.append(player)

    _reroute_audio_players(pj_call, media)

    assert player.transmit_sinks == []
    assert player.wxcalls_audio_media is media


def test_reroute_audio_recorders_reconnects_to_changed_media_port() -> None:
    """Verify active audio recorders reroute to a changed media port.

    :returns: None.
    """

    old_media = FakeAudioMedia(stop_raises=False, port_id=1)
    new_media = FakeAudioMedia(stop_raises=False, port_id=3)
    recorder = FakeRecorder(port_id=2)
    recorder.wxcalls_audio_media = old_media
    recorder.wxcalls_transmitting = True
    old_media.startTransmit(recorder)
    pj_call = FakePjCall(old_media)
    pj_call.recorders.append(recorder)

    _reroute_audio_recorders(pj_call, new_media)

    assert recorder.wxcalls_audio_media is new_media
    assert recorder.wxcalls_transmitting
    assert old_media.transmit_sinks == []
    assert new_media.transmit_sinks == [recorder]
    assert new_media.listeners == [2]


def test_reroute_audio_recorders_skips_existing_listener() -> None:
    """Verify recorder rerouting skips an already attached target port.

    :returns: None.
    """

    media = FakeAudioMedia(stop_raises=False, port_id=1)
    recorder = FakeRecorder(port_id=2)
    recorder.wxcalls_audio_media = media
    recorder.wxcalls_transmitting = True
    media.listeners.append(2)
    pj_call = FakePjCall(media)
    pj_call.recorders.append(recorder)

    _reroute_audio_recorders(pj_call, media)

    assert media.transmit_sinks == []
    assert recorder.wxcalls_audio_media is media
    assert recorder.wxcalls_transmitting


@pytest.mark.parametrize(
    ("transport", "proxy_uri"),
    [
        ("tls", None),
        ("udp", "sips:proxy.example.invalid;lr"),
        ("udp", "sip:proxy.example.invalid;transport=tls;lr"),
    ],
)
def test_validate_secure_signaling_accepts_tls_or_sips(transport: str, proxy_uri: str | None) -> None:
    """Verify secure signaling validation accepts TLS and SIPS variants.

    :param transport: Client transport label.
    :param proxy_uri: Optional outbound proxy URI.
    :returns: None.
    """

    client = _client(transport=transport, proxy_uri=proxy_uri)

    _validate_secure_signaling_for_mandatory_srtp(client)


def test_validate_secure_signaling_rejects_plain_udp() -> None:
    """Verify mandatory SRTP rejects plain UDP signaling.

    :returns: None.
    """

    client = _client(transport="udp", proxy_uri="sip:proxy.example.invalid;lr")

    with pytest.raises(BackendError, match="requires TLS or SIPS"):
        _validate_secure_signaling_for_mandatory_srtp(client)


def _client(transport: str, proxy_uri: str | None) -> SipClientConfig:
    """Create a SIP client config for validation tests.

    :param transport: Client transport label.
    :param proxy_uri: Optional outbound proxy URI.
    :returns: SIP client configuration.
    """

    return SipClientConfig(
        name="alice",
        id_uri="sip:alice@example.invalid",
        registrar_uri="sip:registrar.example.invalid",
        username_env="ALICE_USER",
        password_env="ALICE_PASS",
        proxy_uri=proxy_uri,
        transport=transport,
    )


class FakeCallParam:
    def __init__(self) -> None:
        """Create fake PJSUA2 call operation parameters.

        :returns: None.
        """

        self.opt = FakeCallSetting()


class FakeCallSetting:
    def __init__(self) -> None:
        """Create fake PJSUA2 call media settings.

        :returns: None.
        """

        self.audioCount = 0
        self.videoCount = 1
        self.textCount = 1


class FakeCallPlacementPj:
    def CallOpParam(self, use_default_call_setting: bool = False) -> FakeCallParam:  # noqa: N802 - PJSUA2 API name
        """Create fake call operation parameters.

        :param use_default_call_setting: Whether default call settings were requested.
        :returns: Fake call operation parameter.
        """

        return FakeCallParam()


class FakeOutgoingPjCall:
    def __init__(self) -> None:
        """Create fake outgoing-call state.

        :returns: None.
        """

        self.handle_id: str | None = None
        self.target_uri: str | None = None
        self.waited_states: list[str] = []

    def makeCall(self, target_uri: str, prm: FakeCallParam) -> None:  # noqa: N802 - PJSUA2 API name
        """Record the requested outbound target.

        :param target_uri: Dialable SIP target URI.
        :param prm: Fake call operation parameter.
        :returns: None.
        """

        self.target_uri = target_uri

    async def wait_state(self, state: str, timeout: float) -> None:
        """Record any unexpected wait-state request.

        :param state: Desired call state.
        :param timeout: Maximum wait duration.
        :returns: None.
        """

        self.waited_states.append(state)


class FakeStatefulPjCall:
    def __init__(self, state_text: str) -> None:
        """Create a fake PJSUA2 call with state text.

        :param state_text: PJSUA2-like state text.
        :returns: None.
        """

        self.state_text = state_text
        self.answered = False

    def getInfo(self) -> SimpleNamespace:  # noqa: N802 - PJSUA2 API name
        """Return fake call information.

        :returns: Object exposing PJSUA2-like ``stateText``.
        """

        return SimpleNamespace(stateText=self.state_text)

    def answer(self, prm: FakeCallParam) -> None:
        """Mark the fake call answered.

        :param prm: Fake call operation parameter.
        :returns: None.
        """

        self.answered = True


def _backend_with_fake_recording(stop_raises: bool, finalizes: bool = True) -> Pjsua2SipBackend:
    """Create a PJSUA2 backend wired to fake media objects.

    :param stop_raises: Whether fake media stop should raise.
    :param finalizes: Whether fake recorder finalization creates a valid WAV.
    :returns: Backend with one fake call registered.
    """

    backend = Pjsua2SipBackend()
    backend.pj = FakeRecordingPj(finalizes=finalizes)
    backend.calls["call-1"] = FakePjCall(FakeAudioMedia(stop_raises=stop_raises))
    return backend


class FakeRecordingPj:
    PJMEDIA_FILE_NO_LOOP = 1

    def __init__(self, finalizes: bool = True) -> None:
        """Create fake PJSUA2 module state.

        :param finalizes: Whether fake recorder finalization creates a valid WAV.
        :returns: None.
        """

        self.created_players: list[FakePlayer] = []
        self.created_recorders: list[FakeRecorder] = []
        self.finalizes = finalizes

        created_players = self.created_players

        class AudioMediaPlayer(FakePlayer):
            """Fake player class suitable for PJSUA2 adapter subclassing."""

            def __init__(self) -> None:
                """Create and register a fake audio player.

                :returns: None.
                """

                super().__init__()
                created_players.append(self)

        self.AudioMediaPlayer = AudioMediaPlayer

    def AudioMediaRecorder(self) -> FakeRecorder:  # noqa: N802 - PJSUA2 factory name
        """Create a fake audio recorder.

        :returns: Fake recorder.
        """

        recorder = FakeRecorder(finalizes=self.finalizes)
        self.created_recorders.append(recorder)
        return recorder


class FakePjCall:
    def __init__(self, audio_media: FakeAudioMedia) -> None:
        """Create a fake PJSUA2 call with media containers.

        :param audio_media: Fake active audio media.
        :returns: None.
        """

        self.audio_media = audio_media
        self.players: list[FakePlayer] = []
        self.recorders: list[FakeRecorder] = []


class FakeAudioMedia:
    def __init__(self, stop_raises: bool, port_id: int = 1) -> None:
        """Create fake PJSUA2 audio media.

        :param stop_raises: Whether ``stopTransmit`` should raise.
        :param port_id: Fake conference port id.
        :returns: None.
        """

        self.stop_raises = stop_raises
        self.port_id = port_id
        self.transmit_sinks: list[FakeRecorder] = []
        self.listeners: list[int] = []

    def getPortId(self) -> int:  # noqa: N802 - PJSUA2 method name
        """Return the fake conference port id.

        :returns: Fake port id.
        """

        return self.port_id

    def getPortInfo(self) -> FakePortInfo:  # noqa: N802 - PJSUA2 method name
        """Return fake conference port listener info.

        :returns: Fake port info.
        """

        return FakePortInfo(self.listeners)

    def startTransmit(self, recorder: FakeRecorder) -> None:  # noqa: N802 - PJSUA2 method name
        """Mark a fake recorder as started.

        :param recorder: Fake recorder sink.
        :returns: None.
        """

        recorder.started = True
        self.transmit_sinks.append(recorder)
        self.listeners.append(recorder.getPortId())

    def stopTransmit(self, recorder: FakeRecorder) -> None:  # noqa: N802 - PJSUA2 method name
        """Mark a fake recorder as stopped or raise the configured failure.

        :param recorder: Fake recorder sink.
        :returns: None.
        :raises RuntimeError: If stop failure simulation is enabled.
        """

        if self.stop_raises:
            raise RuntimeError("conference route already gone")
        recorder.stopped = True
        with suppress(ValueError):
            self.transmit_sinks.remove(recorder)
        with suppress(ValueError):
            self.listeners.remove(recorder.getPortId())


class FakeRecorder:
    def __init__(self, finalizes: bool = True, port_id: int = 2) -> None:
        """Create fake recorder state.

        :param finalizes: Whether finalization creates a readable WAV.
        :param port_id: Fake conference port id.
        :returns: None.
        """

        self.started = False
        self.stopped = False
        self.finalized = False
        self.finalizes = finalizes
        self.port_id = port_id
        self.path: Path | None = None
        self.thisown = True

    def getPortId(self) -> int:  # noqa: N802 - PJSUA2 method name
        """Return the fake conference port id.

        :returns: Fake port id.
        """

        return self.port_id

    def createRecorder(self, path: str) -> None:  # noqa: N802 - PJSUA2 method name
        """Create a fake recording file.

        :param path: Output path.
        :returns: None.
        """

        self.path = Path(path)
        self.path.write_bytes(b"pending wav header")

    def __swig_destroy__(self) -> None:
        """Finalize the fake recording like PJSUA2's SWIG destructor.

        :returns: None.
        """

        if self.path is not None and self.finalizes:
            create_silence_wav(self.path, seconds=0.01)
        self.finalized = True


class FakePlayer:
    def __init__(self) -> None:
        """Create fake player state.

        :returns: None.
        """

        self.path = ""
        self.flags = 0
        self.started = False
        self.stopped = False

    def createPlayer(self, path: str, flags: int) -> None:  # noqa: N802 - PJSUA2 method name
        """Record fake player creation arguments.

        :param path: WAV path.
        :param flags: PJSUA2 playback flags.
        :returns: None.
        """

        self.path = path
        self.flags = flags

    def startTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        """Mark playback started and immediately fire EOF.

        :param audio_media: Fake audio media sink.
        :returns: None.
        """

        self.started = True
        self.onEof2()

    def stopTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        """Mark playback stopped.

        :param audio_media: Fake audio media sink.
        :returns: None.
        """

        self.stopped = True


class FakeStreamingPlayer:
    def __init__(self, audio_media: FakeAudioMedia) -> None:
        """Create fake active streaming player state.

        :param audio_media: Current fake audio media sink.
        :returns: None.
        """

        self.wxcalls_audio_media = audio_media
        self.wxcalls_transmitting = True
        self.wxcalls_eof = asyncio.Event()
        self.transmit_sinks: list[FakeAudioMedia] = []
        self.listeners: list[int] = []

    def getPortInfo(self) -> FakePortInfo:  # noqa: N802 - PJSUA2 method name
        """Return fake conference port listener info.

        :returns: Fake port info.
        """

        return FakePortInfo(self.listeners)

    def startTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        """Record a fake streaming route.

        :param audio_media: Fake audio media sink.
        :returns: None.
        """

        self.transmit_sinks.append(audio_media)
        self.listeners.append(audio_media.getPortId())


class FakePortInfo:
    def __init__(self, listeners: list[int]) -> None:
        """Create fake port info with listener ids.

        :param listeners: Fake conference listener ids.
        :returns: None.
        """

        self.listeners = listeners
