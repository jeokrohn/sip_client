"""Experimental ``SipBackend`` that proxies one client per local subprocess."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from wxcalls.backends.base import (
    CallHandle,
    RegistrationResult,
    RegistrationWaitResult,
    SipBackend,
    VideoSmokeResult,
)
from wxcalls.config import LabConfig, SipClientConfig, SipCredentials
from wxcalls.exceptions import (
    BackendError,
    ConfigError,
    ScenarioError,
    UnsupportedFeature,
)
from wxcalls.exceptions import (
    TimeoutError as WxTimeoutError,
)
from wxcalls.scenario import Scenario, ScenarioStep

_WORKER_MODULE = "wxcalls.workers.worker"


class _Worker:
    """Controller-side protocol handle for one endpoint process.

    :param client: Client owned by this worker.
    :param process: Worker subprocess.
    :param pjsip_log_path: Worker PJSIP log location.
    :param stderr_log_path: Worker standard-error log location.
    :param secret_values: Credential strings redacted from evidence and errors.
    :param event_callback: Controller callback for worker lifecycle events.
    """

    def __init__(
        self,
        client: SipClientConfig,
        process: asyncio.subprocess.Process,
        pjsip_log_path: Path,
        stderr_log_path: Path,
        secret_values: tuple[str, ...],
        event_callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Create response routing state for a worker.

        :param client: Client owned by this worker.
        :param process: Worker subprocess.
        :param pjsip_log_path: Worker PJSIP log location.
        :returns: None.
        """

        self.client = client
        self.process = process
        self.pjsip_log_path = pjsip_log_path
        self.stderr_log_path = stderr_log_path
        self.secret_values = tuple(value for value in secret_values if value)
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.write_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None
        self.event_callback = event_callback
        self.last_action = "starting"
        self.last_state = "initializing"
        self.stopping = False

    async def _read_responses(self) -> None:
        """Route worker responses to waiting backend operations.

        :returns: None.
        :side effect: Resolves in-flight protocol futures and records worker termination.
        """

        assert self.process.stdout is not None
        async for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._fail_pending(f"Worker {self.client.name} emitted invalid protocol data: {exc}")
                continue
            if message.get("type") == "event":
                if message.get("event") == "call_update":
                    fields = {key: value for key, value in message.items() if key not in {"type", "event"}}
                    self.event_callback("call_update", fields)
                continue
            if message.get("type") != "response":
                continue
            future = self.pending.pop(str(message.get("id", "")), None)
            if future is not None and not future.done():
                future.set_result(message)
        return_code = await self.process.wait()
        if not self.stopping:
            self.event_callback(
                "worker_failed",
                {
                    "client": self.client.name,
                    "pid": self.process.pid,
                    "exit_code": return_code,
                    "last_action": self.last_action,
                    "last_state": self.last_state,
                    "pjsip_log": str(self.pjsip_log_path),
                },
            )
        self._fail_pending(f"Worker {self.client.name} exited with code {return_code}")

    def _fail_pending(self, message: str) -> None:
        """Fail all in-flight requests after worker termination.

        :param message: Failure description.
        :returns: None.
        """

        for future in self.pending.values():
            if not future.done():
                future.set_exception(BackendError(message))
        self.pending.clear()

    async def request(self, action: str, request_timeout: float = 120, **payload: Any) -> Any:
        """Send an operation and await its correlated response.

        :param action: Backend method name.
        :param request_timeout: Maximum wait for the worker response in seconds.
        :param payload: JSON-serializable operation arguments.
        :returns: Decoded operation result.
        :raises BackendError: If the worker exits or rejects the operation.
        :raises WxTimeoutError: If the worker operation reaches its deadline.
        :raises TimeoutError: If the worker does not respond in time.
        """

        if self.process.stdin is None or self.process.returncode is not None:
            raise BackendError(f"Worker {self.client.name} is not running")
        request_id = uuid4().hex
        self.last_action = action
        self.last_state = f"{action}:in_progress"
        self.event_callback(
            "worker_action",
            {
                "client": self.client.name,
                "action": action,
                "phase": "started",
                "last_state": self.last_state,
                "wait_limit": next(
                    (payload[key] for key in ("operation_timeout", "timeout_seconds", "timeout") if key in payload),
                    None,
                ),
            },
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = json.dumps({"id": request_id, "action": action, **payload}, separators=(",", ":")) + "\n"
        try:
            async with self.write_lock:
                self.process.stdin.write(message.encode())
                await self.process.stdin.drain()
            response = await asyncio.wait_for(future, timeout=request_timeout)
        finally:
            self.pending.pop(request_id, None)
        if not response.get("ok"):
            self.last_state = f"{action}:failed"
            self.event_callback(
                "worker_action",
                {"client": self.client.name, "action": action, "phase": "failed", "last_state": self.last_state},
            )
            error = _redact_sensitive_text(str(response.get("error", "unknown error")), self.secret_values)
            message = f"Worker {self.client.name} failed {action}: {error}"
            error_types: dict[str, type[Exception]] = {
                "BackendError": BackendError,
                "ConfigError": ConfigError,
                "ScenarioError": ScenarioError,
                "TimeoutError": WxTimeoutError,
                "UnsupportedFeature": UnsupportedFeature,
            }
            exception_type = error_types.get(str(response.get("error_type", "")), BackendError)
            raise exception_type(message)
        self.last_state = f"{action}:completed"
        self.event_callback(
            "worker_action",
            {"client": self.client.name, "action": action, "phase": "completed", "last_state": self.last_state},
        )
        return response.get("result")

    async def stop(self) -> None:
        """Shut down the endpoint and wait for its process to exit.

        :returns: None.
        :side effect: Requests SIP unregistration and closes the worker process.
        """

        self.stopping = True
        if self.process.returncode is None and self.process.stdin is not None:
            try:
                self.process.stdin.write(b'{"action":"shutdown"}\n')
                await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(), timeout=25)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
        if self.reader_task is not None and not self.reader_task.done():
            await self.reader_task
        self._redact_logs()

    def _redact_logs(self) -> None:
        """Remove credentials and SIP authorization headers from worker logs.

        :returns: None.
        :side effect: Rewrites worker log files after their process has stopped.
        """

        for path in (self.pjsip_log_path, self.stderr_log_path):
            try:
                contents = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            path.write_text(_redact_sensitive_text(contents, self.secret_values), encoding="utf-8")


class WorkerSipBackend(SipBackend):
    """Experimental backend routing each SIP client to a dedicated worker process."""

    def __init__(self) -> None:
        """Create an uninitialized worker-backed backend.

        :returns: None.
        """

        self.config: LabConfig | None = None
        self.workers: dict[str, _Worker] = {}
        self.worker_clients: dict[str, SipClientConfig] = {}
        self.worker_dirs: dict[str, Path] = {}
        self._failure_event = asyncio.Event()
        self._failure: BackendError | None = None
        self._scenario_index = 0
        self._scenario_worker_root: Path | None = None
        self.pjsip_log_path: Path | None = None
        self.events_path: Path | None = None
        self.call_handles: dict[str, CallHandle] = {}
        self.pending_call_updates: dict[str, dict[str, Any]] = {}
        self.dashboard_sink: Callable[[str, dict[str, Any]], None] | None = None

    async def initialize(self, config: LabConfig, pjsip_log_path: Path | None = None) -> None:
        """Store run configuration; endpoint processes start at scenario boundaries.

        :param config: Lab configuration.
        :param pjsip_log_path: Base PJSIP log path supplied by ``CallLab``.
        :returns: None.
        """

        self.config = config
        self.pjsip_log_path = pjsip_log_path
        if pjsip_log_path is not None:
            self.events_path = pjsip_log_path.parent / "workers.jsonl"

    async def begin_scenario(self, scenario: Scenario) -> None:
        """Start one worker for every configured client referenced by the scenario.

        :param scenario: Validated scenario about to run.
        :returns: None.
        :raises BackendError: If worker setup fails.
        """

        if self.config is None:
            raise BackendError("Worker backend has not been initialized")
        self._failure_event = asyncio.Event()
        self._failure = None
        self._scenario_index += 1
        if self.pjsip_log_path is None:
            raise BackendError("Worker backend is not initialized with run artifacts")
        scenario_name = _safe_name(scenario.name)
        self._scenario_worker_root = (
            self.pjsip_log_path.parent / "workers" / f"{self._scenario_index:03d}-{scenario_name}"
        )
        client_names = _scenario_client_names(scenario)
        try:
            for name in sorted(client_names):
                client = self.config.client(name)
                await self._start_worker(client)
        except Exception:
            await self.end_scenario(scenario)
            raise
        self._record("scenario_workers_ready", scenario=scenario.name, clients=sorted(client_names))

    async def wait_for_failure(self) -> None:
        """Wait until an endpoint process exits unexpectedly.

        :returns: None.
        :raises BackendError: If a worker exits during scenario execution.
        """

        await self._failure_event.wait()
        if self._failure is not None:
            raise self._failure

    async def end_scenario(self, scenario: Scenario) -> None:
        """Stop all endpoint workers after a scenario completes or fails.

        :param scenario: Scenario that just completed.
        :returns: None.
        """

        workers = list(self.workers.values())
        results = await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
        for worker, result in zip(workers, results, strict=True):
            self._record(
                "worker_stopped",
                scenario=scenario.name,
                client=worker.client.name,
                exit_code=worker.process.returncode,
                error=str(result) if isinstance(result, Exception) else None,
                unregistration_confirmed=_unregistration_confirmed(worker.pjsip_log_path),
            )
        self.workers.clear()
        self.worker_clients.clear()
        self.worker_dirs.clear()
        self._scenario_worker_root = None
        self.call_handles.clear()
        self.pending_call_updates.clear()

    async def shutdown(self) -> None:
        """Stop any remaining child processes.

        :returns: None.
        """

        for worker in list(self.workers.values()):
            await worker.stop()
        self.workers.clear()
        self.worker_clients.clear()
        self.worker_dirs.clear()
        self._scenario_worker_root = None
        self.call_handles.clear()
        self.pending_call_updates.clear()

    async def register_client(
        self,
        client: SipClientConfig,
        credentials: SipCredentials,
        timeout: float = 30.0,
    ) -> RegistrationResult:
        """Register a client through its dedicated worker.

        :param client: SIP client to register.
        :param credentials: Resolved authentication credentials.
        :param timeout: Maximum registration wait in seconds.
        :returns: Registration result.
        """

        result = await self._worker(client.name).request(
            "register_client", request_timeout=timeout + 15, timeout_seconds=timeout
        )
        return RegistrationResult(
            client_name=str(result["client_name"]),
            expires=result.get("expires"),
            metadata=dict(result.get("metadata", {})),
        )

    async def stay_registered(
        self,
        client_name: str,
        seconds: float,
        require_reregistration: bool = False,
        min_reregistrations: int = 1,
    ) -> RegistrationWaitResult:
        """Wait for registration maintenance events from one endpoint worker.

        :param client_name: Registered client name.
        :param seconds: Observation duration.
        :param require_reregistration: Whether refreshes are required.
        :param min_reregistrations: Minimum refresh count.
        :returns: Registration wait result.
        """

        raw = await self._worker(client_name).request(
            "stay_registered",
            request_timeout=seconds + 15,
            seconds=seconds,
            require_reregistration=require_reregistration,
            min_reregistrations=min_reregistrations,
        )
        return RegistrationWaitResult(
            client_name=str(raw["client_name"]),
            seconds=float(raw["seconds"]),
            refreshes_observed=int(raw["refreshes_observed"]),
            last_expires=raw.get("last_expires"),
        )

    async def place_call(
        self,
        client_name: str,
        target_uri: str,
        timeout: float = 30.0,
        video: bool = False,
        caller_id: str | None = None,
        pai_caller_id: str | None = None,
    ) -> CallHandle:
        """Place an outgoing call on the caller's worker.

        :param client_name: Calling client.
        :param target_uri: Dialable SIP target.
        :param timeout: Maximum call setup wait.
        :param video: Whether to offer video.
        :param caller_id: Optional LGW caller identity.
        :param pai_caller_id: Optional P-Asserted-Identity override.
        :returns: Call handle.
        """

        raw = await self._worker(client_name).request(
            "place_call",
            request_timeout=timeout + 10,
            target_uri=target_uri,
            timeout=timeout,
            video=video,
            caller_id=caller_id,
            pai_caller_id=pai_caller_id,
        )
        return self._remember_call(raw)

    async def wait_for_incoming(
        self,
        client_name: str,
        timeout: float = 30.0,
        from_uri: str | None = None,
    ) -> CallHandle:
        """Wait for an incoming call on one endpoint worker.

        :param client_name: Receiving client.
        :param timeout: Maximum wait in seconds.
        :param from_uri: Optional expected remote URI.
        :returns: Incoming call handle.
        """

        raw = await self._worker(client_name).request(
            "wait_for_incoming",
            request_timeout=timeout + 10,
            operation_timeout=timeout,
            from_uri=from_uri,
        )
        return self._remember_call(raw)

    async def answer(self, call: CallHandle, status_code: int = 200) -> None:
        """Answer an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP response status.
        :returns: None.
        """

        await self._call_request("answer", call, status_code=status_code)

    async def reject(self, call: CallHandle, status_code: int = 486) -> None:
        """Reject an incoming call.

        :param call: Incoming call handle.
        :param status_code: SIP response status.
        :returns: None.
        """

        await self._call_request("reject", call, status_code=status_code)

    async def wait_call_state(self, call: CallHandle, state: str, timeout: float = 30.0) -> None:
        """Wait for a call state on the owning worker.

        :param call: Call handle.
        :param state: Desired call state.
        :param timeout: Maximum wait in seconds.
        :returns: None.
        """

        await self._call_request("wait_call_state", call, state=state, timeout=timeout, worker_timeout=timeout + 10)

    async def hangup(self, call: CallHandle) -> None:
        """Hang up a call.

        :param call: Call handle.
        :returns: None.
        """

        await self._call_request("hangup", call)

    async def hold(self, call: CallHandle) -> None:
        """Hold a call.

        :param call: Call handle.
        :returns: None.
        """

        await self._call_request("hold", call)

    async def resume(self, call: CallHandle) -> None:
        """Resume a held call.

        :param call: Call handle.
        :returns: None.
        """

        await self._call_request("resume", call)

    async def attended_transfer(self, primary_call: CallHandle, consult_call: CallHandle) -> None:
        """Transfer a call using two call handles owned by the same worker.

        :param primary_call: Original call.
        :param consult_call: Consult call.
        :returns: None.
        :raises BackendError: If the calls belong to different endpoint processes.
        """

        if primary_call.client_name != consult_call.client_name:
            raise BackendError("Attended transfer requires both calls to belong to the same endpoint worker")
        await self._worker(primary_call.client_name).request(
            "attended_transfer",
            primary_call=asdict(primary_call),
            consult_call=asdict(consult_call),
        )

    async def play_wav(self, call: CallHandle, path: Path) -> None:
        """Play WAV audio through the worker owning a call.

        :param call: Call handle.
        :param path: WAV file path visible to the child process.
        :returns: None.
        """

        await self._call_request("play_wav", call, path=str(path), timeout=120)

    async def record_wav(self, call: CallHandle, output_path: Path, seconds: float) -> None:
        """Record WAV audio through the worker owning a call.

        :param call: Call handle.
        :param output_path: Destination WAV path visible to the child process.
        :param seconds: Recording duration.
        :returns: None.
        """

        await self._call_request(
            "record_wav",
            call,
            output_path=str(output_path),
            seconds=seconds,
            timeout=seconds + 20,
        )

    async def video_smoke(self, client_name: str, target_uri: str, timeout: float = 30.0) -> VideoSmokeResult:
        """Run a video smoke call on one endpoint worker.

        :param client_name: Calling client.
        :param target_uri: Dialable SIP target.
        :param timeout: Maximum call setup wait.
        :returns: Video smoke result.
        """

        raw = await self._worker(client_name).request(
            "video_smoke",
            request_timeout=timeout + 45,
            target_uri=target_uri,
            operation_timeout=timeout,
        )
        return VideoSmokeResult(bool(raw["supported"]), bool(raw["skipped"]), str(raw["evidence"]))

    async def _call_request(self, action: str, call: CallHandle, worker_timeout: float = 120, **payload: Any) -> None:
        """Route an operation to the worker owning a call handle.

        :param action: Worker operation name.
        :param call: Call handle identifying its owner.
        :param worker_timeout: Worker response deadline, separate from operation payload fields.
        :param payload: Additional operation fields.
        :returns: None.
        """

        await self._worker(call.client_name).request(
            action,
            request_timeout=worker_timeout,
            call=asdict(call),
            **payload,
        )

    async def _start_worker(self, client: SipClientConfig) -> _Worker:
        """Spawn and initialize one endpoint worker.

        :param client: Client assigned to the worker.
        :returns: Initialized worker.
        :raises BackendError: If PJSUA2 initialization fails.
        """

        if client.name in self.workers:
            return self.workers[client.name]
        if self.config is None or self.pjsip_log_path is None:
            raise BackendError("Worker backend is not initialized with run artifacts")
        credentials = client.credentials(self.config.env)
        worker_client = replace(client, local_port=_available_local_port(client.transport))
        if self._scenario_worker_root is None:
            raise BackendError("Worker scenario directory has not been initialized")
        worker_dir = self._scenario_worker_root / _safe_name(client.name)
        worker_dir.mkdir(parents=True, exist_ok=True)
        self._record("worker_starting", client=client.name)
        stderr_path = worker_dir / "stderr.log"
        pjsip_path = worker_dir / "pjsip.log"
        with stderr_path.open("w", encoding="utf-8") as stderr_file:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                _WORKER_MODULE,
                cwd=Path(__file__).resolve().parents[2],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file,
            )
        worker = _Worker(
            worker_client,
            process,
            pjsip_path,
            stderr_path,
            (credentials.username, credentials.password),
            self._worker_event,
        )
        self.workers[client.name] = worker
        self.worker_clients[client.name] = worker_client
        self.worker_dirs[client.name] = worker_dir
        try:
            if process.stdin is None:
                raise BackendError(f"Worker {client.name} has no control input")
            startup = {
                "action": "start",
                "client": asdict(worker_client),
                "credentials": {"username": credentials.username, "password": credentials.password},
                "dns_nameservers": self.config.dns_nameservers,
                "pjsip_log_path": str(pjsip_path),
            }
            process.stdin.write((json.dumps(startup, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()
            await _wait_worker_ready(worker, timeout=60)
            self._record("worker_ready", client=client.name, pid=process.pid, local_port=worker_client.local_port)
            return worker
        except Exception as exc:
            self._record(
                "worker_start_failed",
                client=client.name,
                pid=process.pid,
                error=type(exc).__name__,
                pjsip_log=str(pjsip_path),
                stderr_log=str(stderr_path),
            )
            await worker.stop()
            self.workers.pop(client.name, None)
            self.worker_clients.pop(client.name, None)
            self.worker_dirs.pop(client.name, None)
            raise

    def _worker(self, client_name: str) -> _Worker:
        """Return a started worker by client name.

        :param client_name: Client name.
        :returns: Worker handle.
        :raises BackendError: If the scenario did not start that endpoint.
        """

        try:
            return self.workers[client_name]
        except KeyError as exc:
            raise BackendError(f"No worker is active for SIP client {client_name}") from exc

    def _record(self, event: str, **fields: Any) -> None:
        """Append a controller event to the worker run log.

        :param event: Event name.
        :param fields: Event fields.
        :returns: None.
        """

        record = {"event": event, "ts": datetime.now(tz=UTC).isoformat(), **fields}
        if self.events_path is not None:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, default=str) + "\n")
        if self.dashboard_sink is not None:
            self.dashboard_sink(event, fields)

    def publish_dashboard_event(self, event: str, **fields: Any) -> None:
        """Forward scenario-level state to the optional live dashboard.

        :param event: Dashboard event name.
        :param fields: Scenario state details.
        :returns: None.
        """

        if self.dashboard_sink is not None:
            self.dashboard_sink(event, fields)

    def _worker_event(self, event: str, fields: dict[str, Any]) -> None:
        """Record worker events and publish unexpected process exits.

        :param event: Worker event name.
        :param fields: Event details without protocol payloads or credentials.
        :returns: None.
        """

        self._record(event, **fields)
        if event == "call_update":
            call_id = str(fields.get("call_id", ""))
            call = self.call_handles.get(call_id)
            if call is None:
                self.pending_call_updates[call_id] = fields
            else:
                _apply_call_update(call, fields)
        elif event == "worker_failed":
            client_name = fields.get("client", "unknown")
            last_state = fields.get("last_state", "unknown")
            exit_code = fields.get("exit_code", "unknown")
            self._failure = BackendError(
                f"Worker {client_name} exited unexpectedly with code {exit_code}; last known state: {last_state}"
            )
            self._failure_event.set()

    def _remember_call(self, raw: dict[str, Any]) -> CallHandle:
        """Store a controller-side call handle and apply updates that arrived early.

        :param raw: Serialized call fields returned by a worker.
        :returns: Mutable call handle observed by scenario behavior and step logic.
        """

        call = _call_handle(raw)
        self.call_handles[call.id] = call
        pending_update = self.pending_call_updates.pop(call.id, None)
        if pending_update is not None:
            _apply_call_update(call, pending_update)
        return call


async def _wait_worker_ready(worker: _Worker, timeout: float) -> None:
    """Wait for the worker startup event or report early process failure.

    :param worker: Worker being initialized.
    :param timeout: Maximum startup wait in seconds.
    :returns: None.
    :raises BackendError: If worker startup fails or times out.
    """

    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    assert worker.process.stdout is not None
    stdout = worker.process.stdout

    async def read_ready() -> None:
        """Consume the initial worker startup event.

        :returns: None.
        """

        while line := await stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                if not ready.done():
                    ready.set_exception(BackendError(f"Worker {worker.client.name} sent invalid startup data: {exc}"))
                return
            if message.get("event") == "ready":
                if not ready.done():
                    ready.set_result(None)
                return
            if message.get("event") == "worker_error":
                if not ready.done():
                    ready.set_exception(
                        BackendError(f"Worker {worker.client.name} startup failed: {message.get('error')}")
                    )
                return
        if not ready.done():
            ready.set_exception(BackendError(f"Worker {worker.client.name} exited before initialization"))

    reader = asyncio.create_task(read_ready())
    try:
        await asyncio.wait_for(ready, timeout=timeout)
    finally:
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    # Continue reading responses using the already attached protocol reader.
    worker.reader_task = asyncio.create_task(worker._read_responses())


def _scenario_client_names(scenario: Scenario) -> set[str]:
    """Collect every client referenced by a scenario before worker startup.

    :param scenario: Validated scenario.
    :returns: Set of explicit SIP client names.
    """

    names = {endpoint.client for endpoint in (scenario.endpoints or {}).values()}

    def visit(step: ScenarioStep) -> None:
        """Collect clients from a step and its nested parallel branches.

        :param step: Validated scenario step.
        :returns: None.
        """

        client = step.params.get("client")
        if isinstance(client, str):
            names.add(client)
        elif isinstance(client, list | tuple):
            names.update(str(name) for name in client)
        clients = step.params.get("clients")
        if isinstance(clients, list | tuple):
            names.update(str(name) for name in clients)
        for branch in step.params.get("branches", {}).values():
            for child in branch:
                visit(child)

    for step in scenario.steps:
        visit(step)
    return names


def _call_handle(raw: dict[str, Any]) -> CallHandle:
    """Reconstruct a backend-neutral call handle from worker JSON.

    :param raw: Serialized call fields.
    :returns: Call handle.
    """

    return CallHandle(
        id=str(raw["id"]),
        client_name=str(raw["client_name"]),
        remote_uri=str(raw["remote_uri"]),
        state=str(raw.get("state", "new")),
        media_active=bool(raw.get("media_active", False)),
        video_active=bool(raw.get("video_active", False)),
        metadata=dict(raw.get("metadata", {})),
    )


def _apply_call_update(call: CallHandle, fields: dict[str, Any]) -> None:
    """Apply a worker call-state notification to its controller-side handle.

    :param call: Mutable controller-side call handle.
    :param fields: Worker-reported call state and media flags.
    :returns: None.
    """

    if isinstance(fields.get("state"), str):
        call.state = fields["state"]
    if isinstance(fields.get("media_active"), bool):
        call.media_active = fields["media_active"]
    if isinstance(fields.get("video_active"), bool):
        call.video_active = fields["video_active"]


def _available_local_port(transport: str) -> int:
    """Find an available local socket port matching the SIP transport.

    :param transport: Transport name.
    :returns: Candidate local port.
    """

    kind = socket.SOCK_DGRAM if transport.lower() == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as candidate:
        candidate.bind(("0.0.0.0", 0))
        return int(candidate.getsockname()[1])


def _safe_name(name: str) -> str:
    """Return a filesystem-safe worker directory name.

    :param name: Logical client name.
    :returns: Sanitized name.
    """

    return "".join(character if character.isalnum() or character in "-_" else "_" for character in name)


def _unregistration_confirmed(path: Path) -> bool:
    """Check whether the worker PJSIP log confirms account unregistration.

    :param path: Worker PJSIP log.
    :returns: Whether successful unregistration was logged.
    """

    try:
        return "unregistration success" in path.read_text(encoding="utf-8", errors="replace").casefold()
    except FileNotFoundError:
        return False


def _redact_sensitive_text(contents: str, secret_values: tuple[str, ...]) -> str:
    """Redact SIP authorization headers and known credentials from text.

    :param contents: Log contents or worker error text.
    :param secret_values: Credential strings to remove.
    :returns: Redacted text.
    """

    redacted = re.sub(
        r"(?im)^([^\r\n]*?(?:Proxy-)?Authorization[ \t]*:).*$",
        r"\1 [REDACTED]",
        contents,
    )
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
