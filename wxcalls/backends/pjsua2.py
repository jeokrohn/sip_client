"""PJSUA2 backend adapter for live Webex Calling SIP tests."""

from __future__ import annotations

import asyncio
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


class Pjsua2SipBackend:
    """Live SIP backend implemented with PJSUA2 Python bindings."""

    def __init__(self) -> None:
        self.pj: Any | None = None
        self.endpoint: Any | None = None
        self.config: LabConfig | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.poll_task: asyncio.Task[None] | None = None
        self.accounts: dict[str, Any] = {}
        self.calls: dict[str, Any] = {}
        self.call_handles: dict[str, CallHandle] = {}

    async def initialize(self, config: LabConfig, pjsip_log_path: Path | None = None) -> None:
        """Initialize PJSUA2 endpoint, transport, and polling."""

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
        """Destroy PJSUA2 resources."""

        if self.poll_task:
            self.poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.poll_task
        if self.endpoint is not None:
            self.endpoint.libDestroy()
        self.accounts.clear()
        self.calls.clear()
        self.call_handles.clear()

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> RegistrationResult:
        """Create and register a PJSUA2 account."""

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
        """Keep an account alive and count successful registration refreshes."""

        account = self._account(client_name)
        baseline_count = account.successful_registration_count
        deadline = asyncio.get_running_loop().time() + seconds

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
        """Place an outgoing call through a registered PJSUA2 account."""

        account = self._account(client_name)
        pj_call = _create_call_adapter(self, account)
        handle = self._register_call_handle(
            pj_call=pj_call,
            client_name=client_name,
            remote_uri=target_uri,
            state="calling",
        )
        prm = self.pj.CallOpParam(True)
        _set_call_video_count(prm, 1 if video else 0)
        pj_call.makeCall(target_uri, prm)
        await self.wait_call_state(handle, "connected", timeout=timeout)
        return handle

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for an incoming PJSUA2 call."""

        account = self._account(client_name)
        try:
            handle = await asyncio.wait_for(account.incoming.get(), timeout=timeout)
        except TimeoutError as exc:
            raise WxTimeoutError(f"Timed out waiting for incoming call on {client_name}") from exc
        if from_uri and handle.remote_uri != from_uri:
            raise BackendError(f"Incoming call on {client_name} came from {handle.remote_uri}, expected {from_uri}")
        return handle

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Answer an incoming call."""

        prm = self.pj.CallOpParam()
        prm.statusCode = int(status_code)
        self._call(call).answer(prm)
        await self.wait_call_state(call, "connected", timeout=30.0)

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject an incoming call."""

        prm = self.pj.CallOpParam()
        prm.statusCode = int(status_code)
        self._call(call).hangup(prm)

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait for a PJSUA2 call state."""

        pj_call = self._call(call)
        await pj_call.wait_state(state, timeout)

    async def hangup(self, call: CallHandle) -> None:
        """Hang up a call."""

        prm = self.pj.CallOpParam()
        self._call(call).hangup(prm)

    async def hold(self, call: CallHandle) -> None:
        """Place a call on hold."""

        prm = self.pj.CallOpParam(True)
        self._call(call).setHold(prm)
        call.state = "held"

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call using re-INVITE."""

        prm = self.pj.CallOpParam(True)
        self._call(call).reinvite(prm)
        await self.wait_call_state(call, "connected", timeout=30.0)

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Perform an attended transfer with ``xferReplaces``."""

        primary = self._call(primary_call)
        consult = self._call(consult_call)
        if not hasattr(primary, "xferReplaces"):
            raise UnsupportedFeature("PJSUA2 Call.xferReplaces is unavailable in this binding")
        prm = self.pj.CallOpParam(True)
        primary.xferReplaces(consult, prm)
        primary_call.state = "transferred"

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Play a WAV file into a connected call."""

        pj_call = self._call(call)
        audio_media = pj_call.audio_media
        if audio_media is None:
            raise BackendError(f"Call {call.id} has no active audio media")
        player = self.pj.AudioMediaPlayer()
        flags = getattr(self.pj, "PJMEDIA_FILE_NO_LOOP", 0)
        player.createPlayer(str(path), flags)
        player.startTransmit(audio_media)
        pj_call.players.append(player)

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record call audio to a WAV file."""

        pj_call = self._call(call)
        audio_media = pj_call.audio_media
        if audio_media is None:
            raise BackendError(f"Call {call.id} has no active audio media")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        recorder = self.pj.AudioMediaRecorder()
        recorder.createRecorder(str(output_path))
        audio_media.startTransmit(recorder)
        await asyncio.sleep(seconds)
        audio_media.stopTransmit(recorder)
        pj_call.recorders.append(recorder)

    async def video_smoke(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
    ) -> VideoSmokeResult:
        """Place a video-offering call and report whether video became active."""

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
        if self.pj is None or self.endpoint is None or self.loop is None:
            raise BackendError("PJSUA2 backend has not been initialized")

    def _account(self, client_name: str) -> Any:
        try:
            return self.accounts[client_name]
        except KeyError as exc:
            raise BackendError(f"Client is not registered: {client_name}") from exc

    def _call(self, call: CallHandle) -> Any:
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
        call_id = f"pjsua2-{uuid4()}"
        handle = CallHandle(id=call_id, client_name=client_name, remote_uri=remote_uri, state=state)
        pj_call.handle_id = call_id
        self.calls[call_id] = pj_call
        self.call_handles[call_id] = handle
        return handle

    def _try_set_null_audio_device(self) -> None:
        try:
            self.endpoint.audDevManager().setNullDev()
        except Exception:
            # Some PJSUA2 builds do not expose null-device support. Live tests can still use
            # CoreAudio devices or fail later with a clearer backend/media error.
            return

    def _transport_type(self, transport: str) -> Any:
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

    async def _poll_events(self) -> None:
        self._require_ready()
        while True:
            self.endpoint.libHandleEvents(50)
            await asyncio.sleep(0.01)


def _create_account_adapter(backend: Pjsua2SipBackend, client_name: str) -> Any:
    """Create a concrete ``pj.Account`` subclass with Python callbacks."""

    pj = backend.pj

    class AccountAdapter(pj.Account):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
            super().__init__()
            self.backend = backend
            self.client_name = client_name
            self.registered = asyncio.Event()
            self.registration_events: asyncio.Queue[RegistrationResult] = asyncio.Queue()
            self.last_registration: RegistrationResult | None = None
            self.successful_registration_count = 0
            self.incoming: asyncio.Queue[CallHandle] = asyncio.Queue()

        def onRegState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for SIP registration state."""

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
            """PJSUA2 callback for incoming calls."""

            call = _create_call_adapter(self.backend, self, prm.callId)
            remote_uri = call.safe_remote_uri()
            handle = self.backend._register_call_handle(
                pj_call=call,
                client_name=self.client_name,
                remote_uri=remote_uri,
                state="incoming",
            )
            self.backend.loop.call_soon_threadsafe(self.incoming.put_nowait, handle)

        async def wait_registered(self, timeout: float) -> None:
            """Wait until account registration succeeds."""

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
    """Create a concrete ``pj.Call`` subclass with Python callbacks."""

    pj = backend.pj

    class CallAdapter(pj.Call):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
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

        def onCallState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for call-state changes."""

            handle = self._handle()
            if handle is None:
                return
            state = self._normalized_state()
            handle.state = state
            event = self.state_events.setdefault(state, asyncio.Event())
            self.backend.loop.call_soon_threadsafe(event.set)

        def onCallMediaState(self, prm: Any) -> None:  # noqa: N802 - PJSUA2 callback name
            """PJSUA2 callback for media-state changes."""

            handle = self._handle()
            if handle is None:
                return
            try:
                info = self.getInfo()
                for index, media in enumerate(info.media):
                    media_type = getattr(media, "type", None)
                    status = str(getattr(media, "status", "")).lower()
                    if _is_audio_type(self.backend.pj, media_type) and "active" in status:
                        self.audio_media = self.getAudioMedia(index)
                        handle.media_active = True
                    if _is_video_type(self.backend.pj, media_type) and "active" in status:
                        handle.video_active = True
            except Exception:
                return

        async def wait_state(self, state: str, timeout: float) -> None:
            """Wait for the call to reach a normalized state."""

            handle = self._handle()
            if handle and handle.state == state:
                return
            event = self.state_events.setdefault(state, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError as exc:
                raise WxTimeoutError(f"Timed out waiting for call to reach {state}") from exc

        def safe_remote_uri(self) -> str:
            """Return remote URI if available."""

            try:
                return str(self.getInfo().remoteUri)
            except Exception:
                return "unknown"

        def _handle(self) -> CallHandle | None:
            if self.handle_id is None:
                return None
            return self.backend.call_handles.get(self.handle_id)

        def _normalized_state(self) -> str:
            try:
                info = self.getInfo()
                state_text = str(getattr(info, "stateText", "")).lower()
                if "confirmed" in state_text:
                    return "connected"
                if "discon" in state_text:
                    return "disconnected"
                if "early" in state_text:
                    return "ringing"
                if "call" in state_text:
                    return "calling"
                return state_text or "unknown"
            except Exception:
                return "unknown"

    return CallAdapter()


def _set_video_count(config_or_param: Any, count: int) -> None:
    video_config = getattr(config_or_param, "videoConfig", None)
    if video_config is not None and hasattr(video_config, "autoShowIncoming"):
        video_config.autoShowIncoming = count > 0
    if video_config is not None and hasattr(video_config, "autoTransmitOutgoing"):
        video_config.autoTransmitOutgoing = count > 0


def _set_call_video_count(call_param: Any, count: int) -> None:
    opt = getattr(call_param, "opt", None)
    if opt is None:
        return
    for attr in ("videoCount", "vidCnt"):
        if hasattr(opt, attr):
            setattr(opt, attr, count)


def _is_audio_type(pj: Any, media_type: Any) -> bool:
    return hasattr(pj, "PJMEDIA_TYPE_AUDIO") and media_type == pj.PJMEDIA_TYPE_AUDIO


def _is_video_type(pj: Any, media_type: Any) -> bool:
    return hasattr(pj, "PJMEDIA_TYPE_VIDEO") and media_type == pj.PJMEDIA_TYPE_VIDEO
