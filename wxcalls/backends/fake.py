"""Deterministic in-memory backend for tests and dry-run scenario validation."""

from __future__ import annotations

import asyncio
import builtins
import shutil
from pathlib import Path
from uuid import uuid4

from wxcalls.backends.base import (
    CallHandle,
    RegistrationResult,
    RegistrationWaitResult,
    VideoSmokeResult,
)
from wxcalls.config import LabConfig, SipClientConfig, SipCredentials
from wxcalls.exceptions import BackendError, TimeoutError
from wxcalls.media import create_silence_wav


class FakeSipBackend:
    """A deterministic backend that models SIP call state without network traffic."""

    def __init__(self) -> None:
        """Create empty fake backend state.

        :returns: None.
        """

        self.config: LabConfig | None = None
        self.registered: set[str] = set()
        self.incoming: dict[str, asyncio.Queue[CallHandle]] = {}
        self.linked_calls: dict[str, str] = {}
        self.calls: dict[str, CallHandle] = {}
        self.last_played: dict[str, Path] = {}
        self.registration_results: dict[str, RegistrationResult] = {}

    async def initialize(self, config: LabConfig, pjsip_log_path: Path | None = None) -> None:
        """Initialize fake state for the configured clients.

        :param config: Parsed lab configuration.
        :param pjsip_log_path: Ignored live-backend log path.
        :returns: None.
        """

        self.config = config
        self.incoming = {client.name: asyncio.Queue() for client in config.clients}

    async def shutdown(self) -> None:
        """Clear fake backend state.

        :returns: None.
        """

        self.registered.clear()
        self.calls.clear()
        self.linked_calls.clear()
        self.last_played.clear()
        self.registration_results.clear()

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> RegistrationResult:
        """Mark a client as registered if credentials are non-empty.

        :param client: Client configuration to register.
        :param credentials: Resolved SIP credentials.
        :param timeout: Ignored timeout kept for backend API compatibility.
        :returns: Fake registration result.
        :raises BackendError: If either credential field is empty.
        """

        if not credentials.username or not credentials.password:
            raise BackendError(f"Fake registration failed for {client.name}: empty credentials")
        self.registered.add(client.name)
        result = RegistrationResult(
            client_name=client.name,
            expires=30,
            metadata={"backend": "fake"},
        )
        self.registration_results[client.name] = result
        return result

    async def stay_registered(
        self,
        client_name: str,
        seconds: float,
        require_reregistration: bool = False,
        min_reregistrations: int = 1,
    ) -> RegistrationWaitResult:
        """Simulate staying registered without making dry-run scenarios slow.

        :param client_name: Logical client name.
        :param seconds: Requested registration wait duration.
        :param require_reregistration: Whether refreshes should be reported.
        :param min_reregistrations: Refresh count to report when refreshes are required.
        :returns: Fake registration wait result.
        """

        self._require_registered(client_name)
        await asyncio.sleep(min(seconds, 0.05))
        refreshes_observed = min_reregistrations if require_reregistration else 0
        result = self.registration_results.get(client_name)
        return RegistrationWaitResult(
            client_name=client_name,
            seconds=seconds,
            refreshes_observed=refreshes_observed,
            last_expires=result.expires if result else None,
        )

    async def place_call(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
        video: bool = False,
    ) -> CallHandle:
        """Create an outgoing call and a linked incoming call for simulated clients.

        :param client_name: Calling logical client name.
        :param target_uri: Target URI to dial.
        :param timeout: Ignored timeout kept for backend API compatibility.
        :param video: Whether to mark simulated video active.
        :returns: Outgoing call handle.
        """

        self._require_registered(client_name)
        outgoing = CallHandle(
            id=f"fake-{uuid4()}",
            client_name=client_name,
            remote_uri=target_uri,
            state="calling",
            video_active=video,
        )
        self.calls[outgoing.id] = outgoing

        # Known client targets get a paired inbound leg so two-party scenarios can
        # exercise answer, reject, hold, transfer, and hangup without network I/O.
        target_client = self._client_name_for_uri(target_uri)
        if target_client:
            incoming = CallHandle(
                id=f"fake-{uuid4()}",
                client_name=target_client,
                remote_uri=self._client_uri(client_name),
                state="incoming",
                video_active=video,
            )
            self.calls[incoming.id] = incoming
            self.linked_calls[outgoing.id] = incoming.id
            self.linked_calls[incoming.id] = outgoing.id
            await self.incoming[target_client].put(incoming)
        else:
            outgoing.state = "connected"
            outgoing.media_active = True
        return outgoing

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for a simulated incoming call.

        :param client_name: Receiving logical client name.
        :param timeout: Maximum wait in seconds.
        :param from_uri: Optional expected remote URI.
        :returns: Incoming call handle.
        :raises BackendError: If the caller URI does not match ``from_uri``.
        :raises TimeoutError: If no incoming call is queued before timeout.
        """

        self._require_registered(client_name)
        try:
            call = await asyncio.wait_for(self.incoming[client_name].get(), timeout=timeout)
        except builtins.TimeoutError as exc:
            raise TimeoutError(f"Timed out waiting for incoming call on {client_name}") from exc
        if from_uri and call.remote_uri != from_uri:
            raise BackendError(f"Incoming call on {client_name} came from {call.remote_uri}, expected {from_uri}")
        return call

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Connect a call and its linked peer.

        :param call: Incoming call handle.
        :param status_code: Ignored SIP status code.
        :returns: None.
        """

        call.state = "connected"
        call.media_active = True
        peer = self._linked(call)
        if peer:
            peer.state = "connected"
            peer.media_active = True

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject a call and mark its linked peer disconnected.

        :param call: Incoming call handle.
        :param status_code: Ignored SIP rejection code.
        :returns: None.
        """

        call.state = "rejected"
        peer = self._linked(call)
        if peer:
            peer.state = "disconnected"

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait for a call to reach the requested state.

        :param call: Call handle to observe.
        :param state: Desired state string.
        :param timeout: Maximum wait in seconds.
        :returns: None.
        :raises TimeoutError: If the state is not reached before timeout.
        """

        deadline = asyncio.get_running_loop().time() + timeout
        while call.state != state:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for call {call.id} to reach {state}")
            await asyncio.sleep(0.01)

    async def hangup(self, call: CallHandle) -> None:
        """Disconnect a call and its linked peer.

        :param call: Call handle to disconnect.
        :returns: None.
        """

        call.state = "disconnected"
        call.media_active = False
        peer = self._linked(call)
        if peer:
            peer.state = "disconnected"
            peer.media_active = False

    async def hold(self, call: CallHandle) -> None:
        """Mark a call held.

        :param call: Call handle to hold.
        :returns: None.
        """

        call.state = "held"
        call.media_active = False

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call.

        :param call: Held call handle.
        :returns: None.
        """

        call.state = "connected"
        call.media_active = True

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Model an attended transfer as primary transfer and consult disconnect.

        :param primary_call: Original call to transfer.
        :param consult_call: Consult call that identifies the transfer target.
        :returns: None.
        """

        primary_call.state = "transferred"
        consult_call.state = "disconnected"

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Remember the last file played on the call and linked peer.

        :param call: Call handle that receives playback.
        :param path: WAV path to play.
        :returns: None.
        :raises BackendError: If the WAV file does not exist.
        """

        if not path.exists():
            raise BackendError(f"Audio file does not exist: {path}")
        self.last_played[call.id] = path
        peer = self._linked(call)
        if peer:
            self.last_played[peer.id] = path

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Copy the peer's played media or create silence.

        :param call: Call handle to record.
        :param output_path: Destination recording path.
        :param seconds: Duration for silence fallback.
        :returns: None.
        """

        output_path.parent.mkdir(parents=True, exist_ok=True)
        played = self.last_played.get(call.id)
        if played:
            shutil.copy2(played, output_path)
        else:
            create_silence_wav(output_path, seconds)

    async def video_smoke(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
    ) -> VideoSmokeResult:
        """Return a deterministic unsupported result for non-simulated targets.

        :param client_name: Calling logical client name.
        :param target_uri: Dialable target URI.
        :param timeout: Ignored timeout kept for backend API compatibility.
        :returns: Fake video smoke result.
        """

        self._require_registered(client_name)
        if self._client_name_for_uri(target_uri):
            return VideoSmokeResult(
                supported=True,
                skipped=False,
                evidence="fake backend linked two simulated clients with video enabled",
            )
        return VideoSmokeResult(
            supported=False,
            skipped=True,
            evidence="fake backend cannot verify video with opaque targets",
        )

    def _require_registered(self, client_name: str) -> None:
        """Ensure a fake client has been registered.

        :param client_name: Logical client name.
        :returns: None.
        :raises BackendError: If the client is not registered.
        """

        if client_name not in self.registered:
            raise BackendError(f"Client is not registered: {client_name}")

    def _client_name_for_uri(self, uri: str) -> str | None:
        """Resolve a SIP URI back to a configured fake client name.

        :param uri: SIP identity URI.
        :returns: Matching client name, if any.
        """

        if self.config is None:
            return None
        for client in self.config.clients:
            if client.id_uri == uri:
                return client.name
        return self.config.client_name_for_extension_uri(uri)

    def _client_uri(self, name: str) -> str:
        """Return a configured fake client's identity URI.

        :param name: Logical client name.
        :returns: Client SIP identity URI.
        :raises BackendError: If the backend has not been initialized.
        """

        if self.config is None:
            raise BackendError("Fake backend not initialized")
        return self.config.client(name).id_uri

    def _linked(self, call: CallHandle) -> CallHandle | None:
        """Return the simulated peer call for a linked call leg.

        :param call: Call handle to resolve.
        :returns: Linked peer call handle, if one exists.
        """

        linked_id = self.linked_calls.get(call.id)
        return self.calls.get(linked_id or "")
