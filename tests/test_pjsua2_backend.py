from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wxcalls.backends.base import CallHandle
from wxcalls.backends.pjsua2 import (
    Pjsua2SipBackend,
    _configure_srtp,
    _is_active_media_status,
    _reroute_audio_players,
    _set_call_media_counts,
    _validate_secure_signaling_for_mandatory_srtp,
)
from wxcalls.config import SipClientConfig
from wxcalls.exceptions import BackendError
from wxcalls.media import create_silence_wav


class FakePj:
    PJMEDIA_SRTP_MANDATORY = 2
    PJMEDIA_SRTP_KEYING_SDES = 0
    PJSUA_CALL_MEDIA_ACTIVE = 1

    class SrtpCrypto:
        def __init__(self) -> None:
            self.name = ""


class FakeMediaConfig:
    def __init__(self) -> None:
        self.srtpUse = 0
        self.srtpSecureSignaling = 0
        self.srtpOpt = FakeSrtpOpt()


class FakeSrtpOpt:
    def __init__(self) -> None:
        self.cryptos = []
        self.keyings = []


class FakeAccountConfig:
    def __init__(self) -> None:
        self.mediaConfig = FakeMediaConfig()


def test_configure_srtp_requires_secure_media() -> None:
    account_config = FakeAccountConfig()

    _configure_srtp(account_config, FakePj)

    assert account_config.mediaConfig.srtpUse == FakePj.PJMEDIA_SRTP_MANDATORY
    assert account_config.mediaConfig.srtpSecureSignaling == 0
    assert [crypto.name for crypto in account_config.mediaConfig.srtpOpt.cryptos] == [
        "AES_CM_128_HMAC_SHA1_80"
    ]
    assert account_config.mediaConfig.srtpOpt.keyings == [FakePj.PJMEDIA_SRTP_KEYING_SDES]


def test_configure_srtp_requires_binding_support() -> None:
    account_config = object()

    with pytest.raises(BackendError, match="SRTP"):
        _configure_srtp(account_config, object())


def test_set_call_media_counts_disables_text_and_preserves_audio() -> None:
    call_param = FakeCallParam()

    _set_call_media_counts(call_param, video_count=0, text_count=0)

    assert call_param.opt.audioCount == 1
    assert call_param.opt.videoCount == 0
    assert call_param.opt.textCount == 0


def test_active_media_status_accepts_binding_constant_and_text() -> None:
    assert _is_active_media_status(FakePj, FakePj.PJSUA_CALL_MEDIA_ACTIVE)
    assert _is_active_media_status(FakePj, "PJSUA_CALL_MEDIA_ACTIVE")
    assert not _is_active_media_status(FakePj, 0)


def test_record_wav_ignores_stop_failure_when_artifact_exists(tmp_path: Path) -> None:
    backend = _backend_with_fake_recording(stop_raises=True)
    output = tmp_path / "recording.wav"
    call = CallHandle(id="call-1", client_name="alice", remote_uri="sip:bob")

    asyncio.run(backend.record_wav(call, output, 0))

    assert output.read_bytes() == b"fake wav"
    assert backend.calls["call-1"].recorders == []


def test_play_wav_waits_stops_and_releases_player(tmp_path: Path) -> None:
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
    media = FakeAudioMedia(stop_raises=False, port_id=1)
    player = FakeStreamingPlayer(audio_media=media)
    pj_call = FakePjCall(media)
    pj_call.players.append(player)

    _reroute_audio_players(pj_call, media)

    assert player.transmit_sinks == [media]
    assert player.listeners == [1]


def test_reroute_audio_players_skips_existing_listener() -> None:
    media = FakeAudioMedia(stop_raises=False, port_id=1)
    player = FakeStreamingPlayer(audio_media=media)
    player.listeners.append(1)
    pj_call = FakePjCall(media)
    pj_call.players.append(player)

    _reroute_audio_players(pj_call, media)

    assert player.transmit_sinks == []
    assert player.wxcalls_audio_media is media


@pytest.mark.parametrize(
    ("transport", "proxy_uri"),
    [
        ("tls", None),
        ("udp", "sips:proxy.example.invalid;lr"),
        ("udp", "sip:proxy.example.invalid;transport=tls;lr"),
    ],
)
def test_validate_secure_signaling_accepts_tls_or_sips(transport: str, proxy_uri: str | None) -> None:
    client = _client(transport=transport, proxy_uri=proxy_uri)

    _validate_secure_signaling_for_mandatory_srtp(client)


def test_validate_secure_signaling_rejects_plain_udp() -> None:
    client = _client(transport="udp", proxy_uri="sip:proxy.example.invalid;lr")

    with pytest.raises(BackendError, match="requires TLS or SIPS"):
        _validate_secure_signaling_for_mandatory_srtp(client)


def _client(transport: str, proxy_uri: str | None) -> SipClientConfig:
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
        self.opt = FakeCallSetting()


class FakeCallSetting:
    def __init__(self) -> None:
        self.audioCount = 0
        self.videoCount = 1
        self.textCount = 1


def _backend_with_fake_recording(stop_raises: bool) -> Pjsua2SipBackend:
    backend = Pjsua2SipBackend()
    backend.pj = FakeRecordingPj()
    backend.calls["call-1"] = FakePjCall(FakeAudioMedia(stop_raises=stop_raises))
    return backend


class FakeRecordingPj:
    PJMEDIA_FILE_NO_LOOP = 1

    def __init__(self) -> None:
        self.created_players: list[FakePlayer] = []

        created_players = self.created_players

        class AudioMediaPlayer(FakePlayer):
            """Fake player class suitable for PJSUA2 adapter subclassing."""

            def __init__(self) -> None:
                super().__init__()
                created_players.append(self)

        self.AudioMediaPlayer = AudioMediaPlayer

    def AudioMediaRecorder(self) -> FakeRecorder:  # noqa: N802 - PJSUA2 factory name
        return FakeRecorder()


class FakePjCall:
    def __init__(self, audio_media: FakeAudioMedia) -> None:
        self.audio_media = audio_media
        self.players: list[FakePlayer] = []
        self.recorders: list[FakeRecorder] = []


class FakeAudioMedia:
    def __init__(self, stop_raises: bool, port_id: int = 1) -> None:
        self.stop_raises = stop_raises
        self.port_id = port_id

    def getPortId(self) -> int:  # noqa: N802 - PJSUA2 method name
        return self.port_id

    def startTransmit(self, recorder: FakeRecorder) -> None:  # noqa: N802 - PJSUA2 method name
        recorder.started = True

    def stopTransmit(self, recorder: FakeRecorder) -> None:  # noqa: N802 - PJSUA2 method name
        if self.stop_raises:
            raise RuntimeError("conference route already gone")
        recorder.stopped = True


class FakeRecorder:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def createRecorder(self, path: str) -> None:  # noqa: N802 - PJSUA2 method name
        Path(path).write_bytes(b"fake wav")


class FakePlayer:
    def __init__(self) -> None:
        self.path = ""
        self.flags = 0
        self.started = False
        self.stopped = False

    def createPlayer(self, path: str, flags: int) -> None:  # noqa: N802 - PJSUA2 method name
        self.path = path
        self.flags = flags

    def startTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        self.started = True
        self.onEof2()

    def stopTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        self.stopped = True


class FakeStreamingPlayer:
    def __init__(self, audio_media: FakeAudioMedia) -> None:
        self.wxcalls_audio_media = audio_media
        self.wxcalls_transmitting = True
        self.wxcalls_eof = asyncio.Event()
        self.transmit_sinks: list[FakeAudioMedia] = []
        self.listeners: list[int] = []

    def getPortInfo(self) -> FakePortInfo:  # noqa: N802 - PJSUA2 method name
        return FakePortInfo(self.listeners)

    def startTransmit(self, audio_media: FakeAudioMedia) -> None:  # noqa: N802 - PJSUA2 method name
        self.transmit_sinks.append(audio_media)
        self.listeners.append(audio_media.getPortId())


class FakePortInfo:
    def __init__(self, listeners: list[int]) -> None:
        self.listeners = listeners
