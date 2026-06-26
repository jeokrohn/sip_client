"""PJSUA2 backend adapter for live Webex Calling SIP tests."""

from __future__ import annotations

import asyncio
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from wxcalls.backends.base import (
    CallHandle,
    RegistrationResult,
    RegistrationWaitResult,
    VideoSmokeResult,
)
from wxcalls.config import LabConfig, SipClientConfig, SipCredentials
from wxcalls.exceptions import BackendError, UnsupportedFeature
from wxcalls.exceptions import TimeoutError as WxTimeoutError

_RECORDING_READY_TIMEOUT = 1.0
_RECORDING_POLL_INTERVAL = 0.05


class Pjsua2SipBackend:
    """Live SIP backend implemented with PJSUA2 Python bindings."""

    def __init__(self) -> None:
        """Create an uninitialized PJSUA2 backend adapter.

        :returns: None.
        """

        self.pj: Any | None = None
        self.endpoint: Any | None = None
        self.config: LabConfig | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.poll_task: asyncio.Task[None] | None = None
        self.accounts: dict[str, Any] = {}
        self.calls: dict[str, Any] = {}
        self.call_handles: dict[str, CallHandle] = {}

    async def initialize(self, config: LabConfig, pjsip_log_path: Path | None = None) -> None:
        """Initialize PJSUA2 endpoint, transport, and polling.

        :param config: Parsed lab configuration.
        :param pjsip_log_path: Optional PJSIP log file path.
        :returns: None.
        :raises BackendError: If PJSUA2 bindings are not importable.
        """

        try:
            import pjsua2 as pj  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BackendError(
                "pjsua2 is not importable. Build/install PJSIP PJSUA2 Python bindings before using the live backend."
            ) from exc

        self.pj = pj
        self.config = config
        self.loop = asyncio.get_running_loop()
        self.endpoint = pj.Endpoint()
        self.endpoint.libCreate()

        # PJSUA2 callbacks are driven from the asyncio loop by explicit polling
        # rather than background worker threads.
        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.mainThreadOnly = True
        for nameserver in config.dns_nameservers:
            ep_cfg.uaConfig.nameserver.append(nameserver)
        ep_cfg.logConfig.level = 5
        ep_cfg.logConfig.consoleLevel = 3
        if pjsip_log_path is not None:
            pjsip_log_path.parent.mkdir(parents=True, exist_ok=True)
            ep_cfg.logConfig.filename = str(pjsip_log_path)
        self.endpoint.libInit(ep_cfg)

        transport = self._transport_type(config.clients[0].transport)
        transport_cfg = pj.TransportConfig()
        first_port = next((client.local_port for client in config.clients if client.local_port), None)
        if first_port:
            transport_cfg.port = int(first_port)
        self.endpoint.transportCreate(transport, transport_cfg)
        self.endpoint.libStart()
        self._try_set_null_audio_device()
        self.poll_task = asyncio.create_task(self._poll_events())

    async def shutdown(self) -> None:
        """Destroy PJSUA2 resources.

        :returns: None.
        """

        if self.poll_task:
            self.poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.poll_task
        self._release_media_objects()
        self.calls.clear()
        self.call_handles.clear()
        self.accounts.clear()
        if self.endpoint is not None:
            self.endpoint.libDestroy()

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> RegistrationResult:
        """Create and register a PJSUA2 account.

        :param client: Client configuration to register.
        :param credentials: Resolved SIP credentials.
        :param timeout: Maximum registration wait in seconds.
        :returns: Registration result.
        """

        self._require_ready()
        pj = self.pj
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = client.id_uri
        acc_cfg.regConfig.registrarUri = client.registrar_uri
        if client.proxy_uri:
            acc_cfg.sipConfig.proxies.append(client.proxy_uri)
        acc_cfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", credentials.username, 0, credentials.password)
        )
        _validate_secure_signaling_for_mandatory_srtp(client)
        _configure_srtp(acc_cfg, pj)
        _set_video_count(acc_cfg, 1 if client.enable_video else 0)

        account = _create_account_adapter(self, client.name)
        account.create(acc_cfg)
        self.accounts[client.name] = account
        return await account.wait_registered(timeout)

    async def stay_registered(
        self,
        client_name: str,
        seconds: float,
        require_reregistration: bool = False,
        min_reregistrations: int = 1,
    ) -> RegistrationWaitResult:
        """Keep an account alive and count successful registration refreshes.

        :param client_name: Logical client name.
        :param seconds: Duration to observe registration events.
        :param require_reregistration: Whether refreshes are required.
        :param min_reregistrations: Minimum refresh count when required.
        :returns: Registration wait result.
        :raises WxTimeoutError: If required refreshes are not observed.
        """

        account = self._account(client_name)
        baseline_count = account.successful_registration_count
        deadline = asyncio.get_running_loop().time() + seconds

        # PJSUA2 emits registration callbacks for both the initial REGISTER and
        # later refreshes; the baseline count separates refreshes from setup.
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(account.registration_events.get(), timeout=remaining)
            except TimeoutError:
                break

        refreshes_observed = max(0, account.successful_registration_count - baseline_count)
        if require_reregistration and refreshes_observed < min_reregistrations:
            raise WxTimeoutError(
                f"Observed {refreshes_observed} registration refresh(es) for {client_name}; "
                f"expected at least {min_reregistrations}"
            )
        last_registration = account.last_registration
        return RegistrationWaitResult(
            client_name=client_name,
            seconds=seconds,
            refreshes_observed=refreshes_observed,
            last_expires=last_registration.expires if last_registration else None,
        )

    async def place_call(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
        video: bool = False,
    ) -> CallHandle:
        """Place an outgoing call through a registered PJSUA2 account.

        :param client_name: Calling logical client name.
        :param target_uri: Dialable SIP target URI.
        :param timeout: Retained for backend API compatibility; use ``wait_state`` for call setup waits.
        :param video: Whether to offer a video media stream.
        :returns: Created outgoing call handle.
        """

        account = self._account(client_name)
        pj_call = _create_call_adapter(self, account)
        handle = self._register_call_handle(
            pj_call=pj_call,
            client_name=client_name,
            remote_uri=target_uri,
            state="calling",
        )
        prm = self.pj.CallOpParam(True)
        _set_call_media_counts(prm, video_count=1 if video else 0, text_count=0)
        pj_call.makeCall(target_uri, prm)
        return handle

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for an incoming PJSUA2 call.

        :param client_name: Receiving logical client name.
        :param timeout: Maximum wait in seconds.
        :param from_uri: Optional expected remote URI.
        :returns: Incoming call handle.
        :raises BackendError: If ``from_uri`` does not match.
        :raises WxTimeoutError: If no call arrives before timeout.
        """

        account = self._account(client_name)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise WxTimeoutError(f"Timed out waiting for incoming call on {client_name}")
            try:
                handle = await asyncio.wait_for(account.incoming.get(), timeout=remaining)
            except TimeoutError as exc:
                raise WxTimeoutError(f"Timed out waiting for incoming call on {client_name}") from exc

            pj_call = self._call(handle)
            if _refresh_call_state(pj_call, handle) == "disconnected":
                continue
            if from_uri and handle.remote_uri != from_uri:
                raise BackendError(f"Incoming call on {client_name} came from {handle.remote_uri}, expected {from_uri}")
            return handle

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Answer an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP status code to answer with.
        :returns: None.
        """

        pj_call = self._call(call)
        if _refresh_call_state(pj_call, call) == "disconnected":
            raise BackendError(f"Cannot answer call {call.id}; it is already disconnected")
        prm = self.pj.CallOpParam(True)
        prm.statusCode = int(status_code)
        _set_call_media_counts(prm, video_count=0, text_count=0)
        try:
            pj_call.answer(prm)
        except Exception as exc:
            if _refresh_call_state(pj_call, call) == "disconnected":
                raise BackendError(f"Cannot answer call {call.id}; it is already disconnected") from exc
            raise BackendError(f"Failed to answer call {call.id}: {exc}") from exc
        await self.wait_call_state(call, "connected", timeout=30.0)

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP status code to reject with.
        :returns: None.
        """

        prm = self.pj.CallOpParam()
        prm.statusCode = int(status_code)
        self._call(call).hangup(prm)

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait for a PJSUA2 call state.

        :param call: Call handle to observe.
        :param state: Backend-neutral state name.
        :param timeout: Maximum wait in seconds.
        :returns: None.
        """

        pj_call = self._call(call)
        await pj_call.wait_state(state, timeout)

    async def hangup(self, call: CallHandle) -> None:
        """Hang up a call.

        :param call: Call handle to disconnect.
        :returns: None.
        """

        prm = self.pj.CallOpParam()
        pj_call = self._call(call)
        if _refresh_call_state(pj_call, call) == "disconnected":
            return
        try:
            pj_call.hangup(prm)
        except Exception as exc:
            if _refresh_call_state(pj_call, call) == "disconnected":
                return
            raise BackendError(f"Failed to hang up call {call.id}: {exc}") from exc

    async def hold(self, call: CallHandle) -> None:
        """Place a call on hold.

        :param call: Call handle to hold.
        :returns: None.
        """

        prm = self.pj.CallOpParam(True)
        _set_call_media_counts(prm, video_count=1 if call.video_active else 0, text_count=0)
        self._call(call).setHold(prm)
        call.state = "held"

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call using re-INVITE.

        :param call: Held call handle.
        :returns: None.
        """

        prm = self.pj.CallOpParam(True)
        _set_call_media_counts(prm, video_count=1 if call.video_active else 0, text_count=0)
        self._call(call).reinvite(prm)
        await self.wait_call_state(call, "connected", timeout=30.0)

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Perform an attended transfer with ``xferReplaces``.

        :param primary_call: Original call to transfer.
        :param consult_call: Consult call that identifies the transfer target.
        :returns: None.
        :raises UnsupportedFeature: If the PJSUA2 binding lacks ``xferReplaces``.
        """

        primary = self._call(primary_call)
        consult = self._call(consult_call)
        if not hasattr(primary, "xferReplaces"):
            raise UnsupportedFeature("PJSUA2 Call.xferReplaces is unavailable in this binding")
        prm = self.pj.CallOpParam(True)
        primary.xferReplaces(consult, prm)
        primary_call.state = "transferred"

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Play a WAV file into a connected call.

        :param call: Connected call handle.
        :param path: WAV path to play.
        :returns: None.
        :raises BackendError: If no active audio media is available.
        """

        pj_call = self._call(call)
        await _wait_for_audio_media_quiet(pj_call, quiet_seconds=1.0, timeout=5.0)
        audio_media = _active_audio_media(self.pj, pj_call)
        if audio_media is None:
            raise BackendError(f"Call {call.id} has no active audio media")
        duration = _wav_duration_seconds(path)
        # PJSUA2 audio ports can change after re-INVITE/media updates; the player
        # adapter tracks EOF and can be rerouted by media callbacks.
        player = _create_audio_player_adapter(self, audio_media)
        flags = getattr(self.pj, "PJMEDIA_FILE_NO_LOOP", 0)
        player.createPlayer(str(path), flags)
        pj_call.players.append(player)
        started = False
        try:
            _start_audio_player_route(player, audio_media)
            started = True
            await _wait_until_playback_finishes(player, call, seconds=duration + 1.0)
        finally:
            if started:
                _stop_audio_player(player)
                await asyncio.sleep(0.05)
            else:
                _mark_audio_player_transmitting(player, False)
            with suppress(ValueError):
                pj_call.players.remove(player)

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record call audio to a WAV file.

        :param call: Connected call handle.
        :param output_path: Destination WAV path.
        :param seconds: Recording duration in seconds.
        :returns: None.
        :raises BackendError: If no active audio media exists or recording cleanup fails.
        """

        pj_call = self._call(call)
        audio_media = _active_audio_media(self.pj, pj_call)
        if audio_media is None:
            raise BackendError(f"Call {call.id} has no active audio media")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        recorder = self.pj.AudioMediaRecorder()
        recorder.createRecorder(str(output_path))
        pj_call.recorders.append(recorder)
        started = False
        try:
            _start_audio_recorder_route(recorder, audio_media)
            started = True
            await asyncio.sleep(seconds)
        finally:
            stop_error = None
            if started:
                try:
                    _stop_audio_recorder_route(recorder)
                except Exception as exc:
                    stop_error = exc
            with suppress(ValueError):
                pj_call.recorders.remove(recorder)
            release_error = None
            try:
                _release_audio_recorder(recorder)
            except Exception as exc:
                release_error = exc
            if not await _wait_for_readable_recording(output_path, timeout=_RECORDING_READY_TIMEOUT):
                if stop_error is not None:
                    raise BackendError(f"Failed to stop recording for call {call.id}: {stop_error}") from stop_error
                if release_error is not None:
                    message = f"Failed to finalize recording for call {call.id}: {release_error}"
                    raise BackendError(message) from release_error
                raise BackendError(f"Recorder did not produce a readable WAV for call {call.id}: {output_path}")

    async def video_smoke(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
    ) -> VideoSmokeResult:
        """Place a video-offering call and report whether video became active.

        :param client_name: Calling logical client name.
        :param target_uri: Dialable SIP target URI.
        :param timeout: Maximum call setup wait in seconds.
        :returns: Video smoke result.
        """

        try:
            call = await self.place_call(client_name, target_uri, timeout=timeout, video=True)
        except BackendError as exc:
            return VideoSmokeResult(supported=False, skipped=True, evidence=str(exc))
        try:
            if call.video_active:
                return VideoSmokeResult(
                    supported=True,
                    skipped=False,
                    evidence=f"call {call.id} reported active video media",
                )
            return VideoSmokeResult(
                supported=False,
                skipped=True,
                evidence=f"call {call.id} connected without active video media",
            )
        finally:
            await self.hangup(call)

    def _require_ready(self) -> None:
        """Ensure the PJSUA2 endpoint has been initialized.

        :returns: None.
        :raises BackendError: If required backend fields are unavailable.
        """

        if self.pj is None or self.endpoint is None or self.loop is None:
            raise BackendError("PJSUA2 backend has not been initialized")

    def _account(self, client_name: str) -> Any:
        """Return a registered PJSUA2 account adapter.

        :param client_name: Logical client name.
        :returns: Account adapter.
        :raises BackendError: If the client has not been registered.
        """

        try:
            return self.accounts[client_name]
        except KeyError as exc:
            raise BackendError(f"Client is not registered: {client_name}") from exc

    def _call(self, call: CallHandle) -> Any:
        """Return the PJSUA2 call adapter for a neutral call handle.

        :param call: Backend-neutral call handle.
        :returns: PJSUA2 call adapter.
        :raises BackendError: If the handle is unknown.
        """

        try:
            return self.calls[call.id]
        except KeyError as exc:
            raise BackendError(f"Unknown call handle: {call.id}") from exc

    def _register_call_handle(
        self,
        pj_call: Any,
        client_name: str,
        remote_uri: str,
        state: str,
    ) -> CallHandle:
        """Create and register a backend-neutral call handle for a PJSUA2 call.

        :param pj_call: PJSUA2 call adapter.
        :param client_name: Local logical client name.
        :param remote_uri: Remote party URI.
        :param state: Initial backend-neutral call state.
        :returns: Created call handle.
        """

        call_id = f"pjsua2-{uuid4()}"
        handle = CallHandle(id=call_id, client_name=client_name, remote_uri=remote_uri, state=state)
        pj_call.handle_id = call_id
        self.calls[call_id] = pj_call
        self.call_handles[call_id] = handle
        return handle

    def _try_set_null_audio_device(self) -> None:
        """Prefer PJSUA2's null audio device when the binding supports it.

        :returns: None.
        """

        try:
            self.endpoint.audDevManager().setNullDev()
        except Exception:
            # Some PJSUA2 builds do not expose null-device support. Live tests can still use
            # CoreAudio devices or fail later with a clearer backend/media error.
            return

    def _transport_type(self, transport: str) -> Any:
        """Resolve a transport label to a PJSUA2 transport constant.

        :param transport: Transport label from client config.
        :returns: PJSUA2 transport constant.
        :raises BackendError: If the transport is unavailable.
        """

        pj = self.pj
        mapping = {
            "udp": "PJSIP_TRANSPORT_UDP",
            "tcp": "PJSIP_TRANSPORT_TCP",
            "tls": "PJSIP_TRANSPORT_TLS",
        }
        attr = mapping.get(transport.lower())
        if attr is None or not hasattr(pj, attr):
            raise BackendError(f"Unsupported or unavailable PJSIP transport: {transport}")
        return getattr(pj, attr)

    def _release_media_objects(self) -> None:
        """Release retained PJSUA2 media objects before endpoint destruction.

        :returns: None.
        """

        for pj_call in self.calls.values():
            players = getattr(pj_call, "players", None)
            if players is not None:
                for player in list(players):
                    _stop_audio_player(player)
                players.clear()
            recorders = getattr(pj_call, "recorders", None)
            if recorders is not None:
                for recorder in list(recorders):
                    with suppress(Exception):
                        _release_audio_recorder(recorder)
                recorders.clear()

    async def _poll_events(self) -> None:
        """Pump PJSUA2 events from the asyncio loop.

        :returns: None.
        """

        self._require_ready()
        while True:
            self.endpoint.libHandleEvents(50)
            await asyncio.sleep(0.01)


def _create_account_adapter(backend: Pjsua2SipBackend, client_name: str) -> Any:
    """Create a concrete ``pj.Account`` subclass with Python callbacks.

    :param backend: Owning PJSUA2 backend.
    :param client_name: Logical client name.
    :returns: Account adapter instance.
    """

    pj = backend.pj

    class AccountAdapter(pj.Account):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
            """Create account callback state.

            :returns: None.
            """

            super().__init__()
            self.backend = backend
            self.client_name = client_name
            self.registered = asyncio.Event()
            self.registration_events: asyncio.Queue[RegistrationResult] = asyncio.Queue()
            self.last_registration: RegistrationResult | None = None
            self.successful_registration_count = 0
            self.incoming: asyncio.Queue[CallHandle] = asyncio.Queue()

        def onRegState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for SIP registration state.

            :param prm: PJSUA2 registration callback parameter.
            :returns: None.
            """

            try:
                info = self.getInfo()
                is_active = bool(info.regIsActive)
                code = int(getattr(prm, "code", 0))
                expiration = int(getattr(prm, "expiration", 0) or 0)
            except Exception:
                is_active = False
                code = 0
                expiration = 0
            if is_active and 200 <= code < 300:
                # Registration callbacks may originate from PJSUA2 internals; all
                # asyncio primitives are updated through the owning event loop.
                result = RegistrationResult(
                    client_name=self.client_name,
                    expires=expiration or None,
                    metadata={"code": code, "reason": str(getattr(prm, "reason", ""))},
                )
                self.last_registration = result
                self.successful_registration_count += 1
                self.backend.loop.call_soon_threadsafe(self.registration_events.put_nowait, result)
                self.backend.loop.call_soon_threadsafe(self.registered.set)

        def onIncomingCall(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for incoming calls.

            :param prm: PJSUA2 incoming-call callback parameter.
            :returns: None.
            """

            call = _create_call_adapter(self.backend, self, prm.callId)
            remote_uri = call.safe_remote_uri()
            handle = self.backend._register_call_handle(
                pj_call=call,
                client_name=self.client_name,
                remote_uri=remote_uri,
                state="incoming",
            )
            self.backend.loop.call_soon_threadsafe(self.incoming.put_nowait, handle)

        async def wait_registered(self, timeout: float) -> RegistrationResult:
            """Wait until account registration succeeds.

            :param timeout: Maximum registration wait in seconds.
            :returns: Registration result from the successful callback.
            :raises WxTimeoutError: If registration does not succeed before timeout.
            """

            try:
                return await asyncio.wait_for(self.registration_events.get(), timeout=timeout)
            except TimeoutError as exc:
                raise WxTimeoutError(f"Timed out registering SIP client {self.client_name}") from exc

    return AccountAdapter()


def _create_call_adapter(
    backend: Pjsua2SipBackend,
    account: Any,
    call_id: int | None = None,
) -> Any:
    """Create a concrete ``pj.Call`` subclass with Python callbacks.

    :param backend: Owning PJSUA2 backend.
    :param account: Account adapter that owns the call.
    :param call_id: Optional incoming PJSUA2 call id.
    :returns: Call adapter instance.
    """

    pj = backend.pj

    class CallAdapter(pj.Call):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
            """Create call callback state.

            :returns: None.
            """

            if call_id is None:
                super().__init__(account)
            else:
                super().__init__(account, call_id)
            self.backend = backend
            self.account = account
            self.handle_id: str | None = None
            self.state_events: dict[str, asyncio.Event] = {}
            self.audio_media: Any | None = None
            self.players: list[Any] = []
            self.recorders: list[Any] = []
            self.media_update_seq = 0
            self.last_media_update_at = 0.0

        def onCallState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for call-state changes.

            :param prm: PJSUA2 call-state callback parameter.
            :returns: None.
            """

            handle = self._handle()
            if handle is None:
                return
            state = self._normalized_state()
            handle.state = state
            event = self.state_events.setdefault(state, asyncio.Event())
            self.backend.loop.call_soon_threadsafe(event.set)

        def onCallMediaState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for media-state changes.

            :param prm: PJSUA2 media-state callback parameter.
            :returns: None.
            """

            handle = self._handle()
            if handle is None:
                return
            try:
                self.media_update_seq += 1
                self.last_media_update_at = self.backend.loop.time()
                info = self.getInfo()
                # Media callbacks are the source of truth for active audio/video
                # and for rerouting players when PJSUA2 changes conference ports.
                for index, media in enumerate(info.media):
                    media_type = getattr(media, "type", None)
                    status = getattr(media, "status", None)
                    if _is_audio_type(self.backend.pj, media_type) and _is_active_media_status(self.backend.pj, status):
                        self.audio_media = self.getAudioMedia(index)
                        handle.media_active = True
                        _reroute_audio_players(self, self.audio_media)
                        _reroute_audio_recorders(self, self.audio_media)
                    if _is_video_type(self.backend.pj, media_type) and _is_active_media_status(self.backend.pj, status):
                        handle.video_active = True
            except Exception:
                return

        async def wait_state(self, state: str, timeout: float) -> None:
            """Wait for the call to reach a normalized state.

            :param state: Backend-neutral state name.
            :param timeout: Maximum wait in seconds.
            :returns: None.
            :raises WxTimeoutError: If the state is not reached before timeout.
            """

            handle = self._handle()
            if handle and handle.state == state:
                return
            event = self.state_events.setdefault(state, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError as exc:
                raise WxTimeoutError(f"Timed out waiting for call to reach {state}") from exc

        def safe_remote_uri(self) -> str:
            """Return remote URI if available.

            :returns: Remote URI, or ``unknown`` when PJSUA2 cannot provide it.
            """

            try:
                return str(self.getInfo().remoteUri)
            except Exception:
                return "unknown"

        def _handle(self) -> CallHandle | None:
            """Return the backend-neutral handle for this call adapter.

            :returns: Matching call handle, if registered.
            """

            if self.handle_id is None:
                return None
            return self.backend.call_handles.get(self.handle_id)

        def _normalized_state(self) -> str:
            """Normalize PJSUA2 state text into the backend state vocabulary.

            :returns: Backend-neutral call state.
            """

            try:
                info = self.getInfo()
                return _normalize_call_state_text(str(getattr(info, "stateText", "")))
            except Exception:
                return "unknown"

    return CallAdapter()


def _refresh_call_state(pj_call: Any, call: CallHandle) -> str:
    """Refresh a neutral call handle from the current PJSUA2 call state.

    :param pj_call: PJSUA2 call adapter.
    :param call: Backend-neutral call handle to update.
    :returns: Refreshed backend-neutral state.
    """

    state = _current_call_state(pj_call, fallback=call.state)
    call.state = state
    return state


def _current_call_state(pj_call: Any, fallback: str = "unknown") -> str:
    """Return a PJSUA2 call's current backend-neutral state.

    :param pj_call: PJSUA2 call adapter.
    :param fallback: State to return when PJSUA2 cannot provide one.
    :returns: Backend-neutral call state.
    """

    normalizer = getattr(pj_call, "_normalized_state", None)
    if callable(normalizer):
        try:
            state = str(normalizer())
        except Exception:
            return fallback
        return state or fallback
    try:
        info = pj_call.getInfo()
    except Exception:
        return fallback
    return _normalize_call_state_text(str(getattr(info, "stateText", "")), fallback=fallback)


def _normalize_call_state_text(state_text: str, fallback: str = "unknown") -> str:
    """Normalize PJSUA2 call-state text into the backend state vocabulary.

    :param state_text: Raw PJSUA2 state text.
    :param fallback: State to return when the raw text is empty.
    :returns: Backend-neutral call state.
    """

    normalized = state_text.lower()
    if "confirmed" in normalized:
        return "connected"
    if "discon" in normalized:
        return "disconnected"
    if "early" in normalized:
        return "ringing"
    if "call" in normalized:
        return "calling"
    return normalized or fallback


def _set_video_count(config_or_param: Any, count: int) -> None:
    """Enable or disable automatic video behavior on a PJSUA2 object.

    :param config_or_param: PJSUA2 account or call configuration object.
    :param count: Requested video stream count.
    :returns: None.
    """

    video_config = getattr(config_or_param, "videoConfig", None)
    if video_config is not None and hasattr(video_config, "autoShowIncoming"):
        video_config.autoShowIncoming = count > 0
    if video_config is not None and hasattr(video_config, "autoTransmitOutgoing"):
        video_config.autoTransmitOutgoing = count > 0


def _configure_srtp(account_config: Any, pj: Any) -> None:
    """Require SRTP negotiation for Webex Calling account media.

    :param account_config: PJSUA2 account configuration object.
    :param pj: Imported ``pjsua2`` module.
    :returns: None.
    :raises BackendError: If the PJSUA2 binding does not expose SRTP controls.
    """

    media_config = getattr(account_config, "mediaConfig", None)
    srtp_opt = getattr(media_config, "srtpOpt", None)
    srtp_mandatory = getattr(pj, "PJMEDIA_SRTP_MANDATORY", None)
    sdes_keying = getattr(pj, "PJMEDIA_SRTP_KEYING_SDES", None)
    crypto_cls = getattr(pj, "SrtpCrypto", None)
    if (
        media_config is None
        or srtp_opt is None
        or srtp_mandatory is None
        or sdes_keying is None
        or crypto_cls is None
        or not hasattr(media_config, "srtpUse")
    ):
        raise BackendError("PJSUA2 binding does not expose SRTP account media configuration")

    # BroadWorks/Webex can offer RTP/SAVP with SDES crypto for inbound calls.
    # Mandatory SRTP keeps all live calls on secure media and rejects plain RTP.
    # Webex still uses sip: URIs over TLS, so URI-based secure-signaling enforcement
    # must stay disabled here; client config validation enforces TLS/SIPS instead.
    media_config.srtpUse = srtp_mandatory
    if hasattr(media_config, "srtpSecureSignaling"):
        media_config.srtpSecureSignaling = 0
    _clear_vector(srtp_opt.cryptos)
    crypto = crypto_cls()
    crypto.name = "AES_CM_128_HMAC_SHA1_80"
    srtp_opt.cryptos.append(crypto)
    _clear_vector(srtp_opt.keyings)
    srtp_opt.keyings.append(sdes_keying)


def _validate_secure_signaling_for_mandatory_srtp(client: SipClientConfig) -> None:
    """Validate that a live client uses secure signaling with mandatory SRTP.

    :param client: SIP client configuration.
    :returns: None.
    :raises BackendError: If the client is not configured for TLS/SIPS signaling.
    """

    proxy_uri = client.proxy_uri.lower() if client.proxy_uri else ""
    if client.transport.lower() == "tls" or proxy_uri.startswith("sips:") or "transport=tls" in proxy_uri:
        return
    raise BackendError(f"Client {client.name} requires TLS or SIPS signaling because live PJSUA2 calls require SRTP")


def _clear_vector(vector: Any) -> None:
    """Clear a PJSUA2 SWIG vector or Python list.

    :param vector: Vector-like object.
    :returns: None.
    """

    if hasattr(vector, "clear"):
        vector.clear()
        return
    del vector[:]


def _release_audio_recorder(recorder: Any) -> None:
    """Release a PJSUA2 recorder object so the WAV file is finalized.

    :param recorder: Recorder object created by PJSUA2.
    :returns: None.
    """

    destroy = getattr(type(recorder), "__swig_destroy__", None)
    if callable(destroy):
        destroy(recorder)
        with suppress(Exception):
            recorder.thisown = False


async def _wait_for_readable_recording(path: Path, timeout: float) -> bool:
    """Wait until a recorder output can be opened as a WAV file.

    :param path: Recording path to inspect.
    :param timeout: Maximum wait duration in seconds.
    :returns: ``True`` when the file is readable before the deadline.
    """

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if _recording_file_is_readable_wav(path):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_RECORDING_POLL_INTERVAL)


def _recording_file_is_readable_wav(path: Path) -> bool:
    """Return whether a recorder produced a readable WAV artifact.

    :param path: Recording path.
    :returns: ``True`` when the path exists and has a valid WAV header.
    """

    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnchannels() > 0 and wav.getsampwidth() > 0 and wav.getframerate() > 0
    except (OSError, EOFError, wave.Error):
        return False


def _active_audio_media(pj: Any, pj_call: Any) -> Any | None:
    """Return the currently active PJSUA2 audio media for a call.

    :param pj: Imported ``pjsua2`` module.
    :param pj_call: PJSUA2 call adapter.
    :returns: Active audio media, if available.
    """

    try:
        info = pj_call.getInfo()
        for index, media in enumerate(info.media):
            media_type = getattr(media, "type", None)
            status = getattr(media, "status", None)
            if _is_audio_type(pj, media_type) and _is_active_media_status(pj, status):
                audio_media = pj_call.getAudioMedia(index)
                pj_call.audio_media = audio_media
                return audio_media
    except Exception:
        return getattr(pj_call, "audio_media", None)
    return getattr(pj_call, "audio_media", None)


async def _wait_for_audio_media_quiet(pj_call: Any, quiet_seconds: float, timeout: float) -> None:
    """Wait until no PJSUA2 audio-media callback has fired for a short period.

    :param pj_call: PJSUA2 call adapter.
    :param quiet_seconds: Required quiet period before returning.
    :param timeout: Maximum time to wait for quiescence.
    :returns: None.
    """

    if not hasattr(pj_call, "media_update_seq"):
        return

    loop = asyncio.get_running_loop()
    observed_seq = int(getattr(pj_call, "media_update_seq", 0))
    quiet_started = loop.time()
    deadline = quiet_started + timeout
    while True:
        now = loop.time()
        current_seq = int(getattr(pj_call, "media_update_seq", observed_seq))
        if current_seq != observed_seq:
            observed_seq = current_seq
            quiet_started = now
        if now - quiet_started >= quiet_seconds:
            return
        if now >= deadline:
            return
        await asyncio.sleep(min(0.05, quiet_seconds - (now - quiet_started), deadline - now))


def _create_audio_player_adapter(backend: Pjsua2SipBackend, audio_media: Any) -> Any:
    """Create an EOF-aware PJSUA2 audio player.

    :param backend: Owning PJSUA2 backend.
    :param audio_media: Destination call audio media.
    :returns: Player object with a ``wxcalls_eof`` event.
    """

    player_cls = backend.pj.AudioMediaPlayer
    if isinstance(player_cls, type):

        class AudioPlayerAdapter(player_cls):  # type: ignore[valid-type,misc]
            """PJSUA2 player that marks EOF and stops its bridge route."""

            def __init__(self) -> None:
                """Create playback bookkeeping state.

                :returns: None.
                """

                super().__init__()
                self.wxcalls_eof = asyncio.Event()
                self.wxcalls_transmitting = False
                self.wxcalls_audio_media = audio_media
                self.wxcalls_loop = backend.loop

            def onEof2(self) -> None:  # noqa: N802 - PJSUA2 callback name
                """PJSUA2 callback fired when one-shot WAV playback reaches EOF.

                :returns: None.
                """

                _stop_audio_player(self)

        return AudioPlayerAdapter()

    player = player_cls()
    player.wxcalls_eof = asyncio.Event()
    player.wxcalls_transmitting = False
    player.wxcalls_audio_media = audio_media
    player.wxcalls_loop = backend.loop
    return player


def _reroute_audio_players(pj_call: Any, audio_media: Any) -> None:
    """Reconnect active WAV players after a call audio port changes.

    :param pj_call: PJSUA2 call adapter.
    :param audio_media: New active call audio media.
    :returns: None.
    """

    port_id = _audio_media_port_id(audio_media)
    for player in list(getattr(pj_call, "players", [])):
        if _audio_player_eof_seen(player) or not getattr(player, "wxcalls_transmitting", False):
            continue
        if port_id is not None and _audio_player_is_routed_to(player, port_id):
            player.wxcalls_audio_media = audio_media
            continue
        _mark_audio_player_transmitting(player, False)
        with suppress(Exception):
            _start_audio_player_route(player, audio_media)


def _reroute_audio_recorders(pj_call: Any, audio_media: Any) -> None:
    """Reconnect active WAV recorders after a call audio port changes.

    :param pj_call: PJSUA2 call adapter.
    :param audio_media: New active call audio media.
    :returns: None.
    """

    for recorder in list(getattr(pj_call, "recorders", [])):
        if not getattr(recorder, "wxcalls_transmitting", False):
            continue
        recorder_port_id = _audio_media_port_id(recorder)
        if recorder_port_id is not None and _audio_media_is_routed_to(audio_media, recorder_port_id):
            recorder.wxcalls_audio_media = audio_media
            continue
        with suppress(Exception):
            _stop_audio_recorder_route(recorder)
        with suppress(Exception):
            _start_audio_recorder_route(recorder, audio_media)


def _start_audio_player_route(player: Any, audio_media: Any) -> None:
    """Start routing a player into the supplied audio media.

    :param player: PJSUA2 audio player.
    :param audio_media: Destination call audio media.
    :returns: None.
    """

    player.wxcalls_audio_media = audio_media
    _mark_audio_player_transmitting(player, True)
    try:
        player.startTransmit(audio_media)
    except Exception:
        _mark_audio_player_transmitting(player, False)
        raise


def _start_audio_recorder_route(recorder: Any, audio_media: Any) -> None:
    """Start routing call audio into a recorder.

    :param recorder: PJSUA2 audio recorder.
    :param audio_media: Source call audio media.
    :returns: None.
    """

    recorder.wxcalls_audio_media = audio_media
    _mark_audio_recorder_transmitting(recorder, True)
    try:
        audio_media.startTransmit(recorder)
    except Exception:
        _mark_audio_recorder_transmitting(recorder, False)
        raise


def _mark_audio_player_transmitting(player: Any, transmitting: bool) -> None:
    """Record whether a player route has been started.

    :param player: PJSUA2 audio player.
    :param transmitting: Current route state.
    :returns: None.
    """

    with suppress(Exception):
        player.wxcalls_transmitting = transmitting


def _mark_audio_recorder_transmitting(recorder: Any, transmitting: bool) -> None:
    """Record whether a recorder route has been started.

    :param recorder: PJSUA2 audio recorder.
    :param transmitting: Current route state.
    :returns: None.
    """

    with suppress(Exception):
        recorder.wxcalls_transmitting = transmitting


def _stop_audio_player(player: Any) -> None:
    """Stop player transmission and signal EOF/cleanup waiters.

    :param player: PJSUA2 audio player.
    :returns: None.
    """

    audio_media = getattr(player, "wxcalls_audio_media", None)
    if audio_media is not None and getattr(player, "wxcalls_transmitting", False):
        with suppress(Exception):
            player.stopTransmit(audio_media)
        _mark_audio_player_transmitting(player, False)
    _set_audio_player_eof(player)


def _stop_audio_recorder_route(recorder: Any) -> None:
    """Stop routing call audio into a recorder.

    :param recorder: PJSUA2 audio recorder.
    :returns: None.
    """

    audio_media = getattr(recorder, "wxcalls_audio_media", None)
    try:
        if audio_media is not None and getattr(recorder, "wxcalls_transmitting", False):
            audio_media.stopTransmit(recorder)
    finally:
        _mark_audio_recorder_transmitting(recorder, False)


def _audio_player_eof_seen(player: Any) -> bool:
    """Return whether a player's EOF event has been signalled.

    :param player: PJSUA2 audio player.
    :returns: ``True`` once playback has completed.
    """

    event = getattr(player, "wxcalls_eof", None)
    return bool(event is not None and event.is_set())


def _audio_player_is_routed_to(player: Any, port_id: int) -> bool:
    """Return whether a player is currently transmitting to a port.

    :param player: PJSUA2 audio player.
    :param port_id: Destination conference port id.
    :returns: ``True`` when the player has the destination in its listener list.
    """

    try:
        listeners = player.getPortInfo().listeners
    except Exception:
        return False
    return any(int(listener) == port_id for listener in listeners)


def _audio_media_is_routed_to(audio_media: Any, port_id: int) -> bool:
    """Return whether an audio media source is transmitting to a port.

    :param audio_media: Source call audio media.
    :param port_id: Destination conference port id.
    :returns: ``True`` when the media has the destination in its listener list.
    """

    try:
        listeners = audio_media.getPortInfo().listeners
    except Exception:
        return False
    return any(int(listener) == port_id for listener in listeners)


def _set_audio_player_eof(player: Any) -> None:
    """Signal a player's EOF event on its owning asyncio loop.

    :param player: PJSUA2 audio player.
    :returns: None.
    """

    event = getattr(player, "wxcalls_eof", None)
    if event is None:
        return
    loop = getattr(player, "wxcalls_loop", None)
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(event.set)
    else:
        event.set()


def _audio_media_port_id(audio_media: Any | None) -> int | None:
    """Return a conference port id for an audio media wrapper.

    :param audio_media: PJSUA2 audio media wrapper.
    :returns: Conference port id, if available.
    """

    if audio_media is None:
        return None
    try:
        return int(audio_media.getPortId())
    except Exception:
        return None


def _wav_duration_seconds(path: Path) -> float:
    """Return the duration of a WAV file.

    :param path: WAV file path.
    :returns: Duration in seconds.
    :raises BackendError: If the WAV file cannot be inspected.
    """

    try:
        with wave.open(str(path), "rb") as wav:
            frame_rate = wav.getframerate()
            if frame_rate <= 0:
                raise BackendError(f"Invalid WAV frame rate for {path}: {frame_rate}")
            return wav.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error) as exc:
        raise BackendError(f"Unable to inspect WAV duration for {path}: {exc}") from exc


async def _wait_until_playback_finishes(player: Any, call: CallHandle, seconds: float) -> None:
    """Wait for PJSUA2 player EOF, playback duration, or call disconnect.

    :param player: PJSUA2 audio player.
    :param call: Call being played into.
    :param seconds: Maximum playback wait in seconds.
    :returns: None.
    """

    eof_event = getattr(player, "wxcalls_eof", None)
    deadline = asyncio.get_running_loop().time() + seconds
    while call.state != "disconnected":
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        if eof_event is None:
            await asyncio.sleep(min(0.05, remaining))
            continue
        try:
            await asyncio.wait_for(eof_event.wait(), timeout=min(0.05, remaining))
            return
        except TimeoutError:
            continue


def _set_call_media_counts(call_param: Any, video_count: int, text_count: int) -> None:
    """Set per-call media counts while preserving the default audio stream.

    :param call_param: PJSUA2 call operation parameter.
    :param video_count: Number of video streams to offer.
    :param text_count: Number of text streams to offer.
    :returns: None.
    """

    opt = getattr(call_param, "opt", None)
    if opt is None:
        return
    for attr in ("audioCount", "audCnt"):
        if hasattr(opt, attr):
            setattr(opt, attr, 1)
    for attr in ("videoCount", "vidCnt"):
        if hasattr(opt, attr):
            setattr(opt, attr, video_count)
    for attr in ("textCount", "txtCnt"):
        if hasattr(opt, attr):
            setattr(opt, attr, text_count)


def _is_audio_type(pj: Any, media_type: Any) -> bool:
    """Return whether a media type is PJSUA2 audio.

    :param pj: Imported ``pjsua2`` module.
    :param media_type: Media type value from PJSUA2.
    :returns: ``True`` when the media type is audio.
    """

    return hasattr(pj, "PJMEDIA_TYPE_AUDIO") and media_type == pj.PJMEDIA_TYPE_AUDIO


def _is_video_type(pj: Any, media_type: Any) -> bool:
    """Return whether a media type is PJSUA2 video.

    :param pj: Imported ``pjsua2`` module.
    :param media_type: Media type value from PJSUA2.
    :returns: ``True`` when the media type is video.
    """

    return hasattr(pj, "PJMEDIA_TYPE_VIDEO") and media_type == pj.PJMEDIA_TYPE_VIDEO


def _is_active_media_status(pj: Any, status: Any) -> bool:
    """Return whether a PJSUA2 media status is active.

    :param pj: Imported ``pjsua2`` module.
    :param status: Media status value from ``CallInfo.media``.
    :returns: ``True`` when the media stream is active.
    """

    active = getattr(pj, "PJSUA_CALL_MEDIA_ACTIVE", None)
    if active is not None and status == active:
        return True
    return "active" in str(status).lower()
