"""Backend protocols and shared call data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from wxcalls.config import LabConfig, SipClientConfig, SipCredentials


@dataclass
class CallHandle:
    """Backend-neutral reference to a call leg.

    :param id: Backend call identifier.
    :param client_name: Local logical client name.
    :param remote_uri: Remote party URI.
    :param state: Last known call state.
    :param media_active: Whether audio media is active.
    :param video_active: Whether video media is active.
    :param metadata: Backend-specific details for diagnostics.
    """

    id: str
    client_name: str
    remote_uri: str
    state: str = "new"
    media_active: bool = False
    video_active: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VideoSmokeResult:
    """Result of a video smoke probe.

    :param supported: Whether video was negotiated or observed.
    :param skipped: Whether the probe was skipped because support was absent.
    :param evidence: Human-readable backend evidence.
    """

    supported: bool
    skipped: bool
    evidence: str


class SipBackend(Protocol):
    """Protocol implemented by SIP/media backends."""

    async def initialize(self, config: LabConfig, pjsip_log_path: Path | None = None) -> None:
        """Initialize backend resources for a lab run."""

    async def shutdown(self) -> None:
        """Release backend resources."""

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> None:
        """Register one SIP client."""

    async def place_call(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
        video: bool = False,
    ) -> CallHandle:
        """Place an outgoing call."""

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for an incoming call."""

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Answer an incoming call."""

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject an incoming call."""

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait until a call reaches a state."""

    async def hangup(self, call: CallHandle) -> None:
        """Hang up a call."""

    async def hold(self, call: CallHandle) -> None:
        """Put a call on hold."""

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call."""

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Transfer the primary call to the consult call target."""

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Play a WAV file into a call leg."""

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record audio from a call leg to a WAV file."""

    async def video_smoke(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
    ) -> VideoSmokeResult:
        """Attempt a video smoke probe."""
