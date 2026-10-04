"""Backend protocols and shared call data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from wxcalls.config import LabConfig, SipClientConfig, SipCredentials


@dataclass(frozen=True)
class RegistrationResult:
    """Result of a successful SIP registration.

    :param client_name: Registered logical client name.
    :param expires: Accepted registration expiration interval in seconds, if known.
    :param metadata: Backend-specific registration details for diagnostics.
    """

    client_name: str
    expires: int | None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistrationWaitResult:
    """Result of keeping a SIP client registered for a requested duration.

    :param client_name: Registered logical client name.
    :param seconds: Requested wait duration in seconds.
    :param refreshes_observed: Successful re-registration refreshes observed during the wait.
    :param last_expires: Last observed registration expiration interval in seconds, if known.
    """

    client_name: str
    seconds: float
    refreshes_observed: int
    last_expires: int | None


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
        """Initialize backend resources for a lab run.

        :param config: Parsed lab configuration.
        :param pjsip_log_path: Optional live-backend PJSIP log path.
        :returns: None.
        """

    async def shutdown(self) -> None:
        """Release backend resources.

        :returns: None.
        """

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> RegistrationResult:
        """Register one SIP client.

        :param client: Client configuration to register.
        :param credentials: Resolved SIP credentials.
        :param timeout: Maximum registration wait in seconds.
        :returns: Registration result.
        """

    async def stay_registered(
        self,
        client_name: str,
        seconds: float,
        require_reregistration: bool = False,
        min_reregistrations: int = 1,
    ) -> RegistrationWaitResult:
        """Keep a SIP client registered before the next scenario step.

        :param client_name: Logical client name.
        :param seconds: Duration to keep the registration alive.
        :param require_reregistration: Whether at least one refresh must be observed.
        :param min_reregistrations: Minimum refresh count when refreshes are required.
        :returns: Registration wait result.
        """

    async def place_call(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
        video: bool = False,
        caller_id: str | None = None,
        pai_caller_id: str | None = None,
    ) -> CallHandle:
        """Place an outgoing call.

        :param client_name: Calling logical client name.
        :param target_uri: Dialable target URI.
        :param timeout: Maximum call setup wait in seconds.
        :param video: Whether to offer video media.
        :param caller_id: Optional numeric caller identity for From and the default PAI.
        :param pai_caller_id: Optional numeric PAI override independent of From.
        :returns: Created call handle.
        """

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for an incoming call.

        :param client_name: Logical client expected to receive the call.
        :param timeout: Maximum wait in seconds.
        :param from_uri: Optional expected remote URI.
        :returns: Incoming call handle.
        """

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Answer an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP status code to answer with.
        :returns: None.
        """

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP status code to reject with.
        :returns: None.
        """

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait until a call reaches a state.

        :param call: Call handle to observe.
        :param state: Backend-neutral state name.
        :param timeout: Maximum wait in seconds.
        :returns: None.
        """

    async def hangup(self, call: CallHandle) -> None:
        """Hang up a call.

        :param call: Call handle to disconnect.
        :returns: None.
        """

    async def hold(self, call: CallHandle) -> None:
        """Put a call on hold.

        :param call: Call handle to hold.
        :returns: None.
        """

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call.

        :param call: Held call handle.
        :returns: None.
        """

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Transfer the primary call to the consult call target.

        :param primary_call: Original call to transfer.
        :param consult_call: Consult call identifying the transfer target.
        :returns: None.
        """

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Play a WAV file into a call leg.

        :param call: Call handle to play into.
        :param path: WAV file path.
        :returns: None.
        """

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record audio from a call leg to a WAV file.

        :param call: Call handle to record.
        :param output_path: Destination WAV path.
        :param seconds: Recording duration.
        :returns: None.
        """

    async def video_smoke(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
    ) -> VideoSmokeResult:
        """Attempt a video smoke probe.

        :param client_name: Calling logical client name.
        :param target_uri: Dialable target URI.
        :param timeout: Maximum call setup wait in seconds.
        :returns: Video smoke result.
        """
