from __future__ import annotations

import pytest

from wxcalls.backends.pjsua2 import (
    _configure_srtp,
    _set_call_media_counts,
    _validate_secure_signaling_for_mandatory_srtp,
)
from wxcalls.config import SipClientConfig
from wxcalls.exceptions import BackendError


class FakePj:
    PJMEDIA_SRTP_MANDATORY = 2
    PJMEDIA_SRTP_KEYING_SDES = 0

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
