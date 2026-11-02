"""Child process hosting one PJSUA2 endpoint and its SIP account(s)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from wxcalls.backends.base import CallHandle
from wxcalls.backends.pjsua2 import Pjsua2SipBackend
from wxcalls.config import LabConfig, SipClientConfig, SipCredentials


def _reserve_protocol_stdout() -> Any:
    """Reserve stdout for JSON messages and redirect native logs to stderr.

    :returns: Line-buffered protocol output stream.
    :side effect: Redirects file descriptor 1 so PJSIP console logs cannot corrupt protocol output.
    """

    protocol_fd = os.dup(sys.stdout.fileno())
    output = os.fdopen(protocol_fd, "w", buffering=1, encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return output


async def _read_message() -> dict[str, Any] | None:
    """Read one JSON object from stdin without blocking the asyncio loop.

    :returns: Decoded JSON object, or ``None`` at EOF.
    :raises ValueError: If the input is not a JSON object.
    """

    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return None
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Worker protocol messages must be JSON objects")
    return message


def _emit(output: Any, message: dict[str, Any]) -> None:
    """Write one JSON-lines protocol message.

    :param output: Protocol output stream.
    :param message: JSON-serializable message.
    :returns: None.
    """

    output.write(json.dumps(message, separators=(",", ":")) + "\n")
    output.flush()


def _call_from_raw(raw: dict[str, Any]) -> CallHandle:
    """Create a backend-neutral call handle from a wire mapping.

    :param raw: Serialized call handle fields.
    :returns: Reconstructed call handle.
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


async def _report_call_updates(backend: Pjsua2SipBackend, output: Any) -> None:
    """Send call state and media changes to the supervising controller.

    :param backend: Initialized PJSUA2 backend.
    :param output: JSON-lines protocol output stream.
    :returns: None.
    :side effect: Emits protocol events when a call handle changes state or media.
    """

    last_updates: dict[str, tuple[str, bool, bool]] = {}
    while True:
        for call_id, call in tuple(backend.call_handles.items()):
            update = (call.state, call.media_active, call.video_active)
            if last_updates.get(call_id) == update:
                continue
            last_updates[call_id] = update
            _emit(
                output,
                {
                    "type": "event",
                    "event": "call_update",
                    "call_id": call_id,
                    "state": call.state,
                    "media_active": call.media_active,
                    "video_active": call.video_active,
                },
            )
        await asyncio.sleep(0.05)


async def _execute(
    backend: Pjsua2SipBackend,
    client: SipClientConfig,
    credentials: SipCredentials,
    action: str,
    payload: dict[str, Any],
) -> Any:
    """Execute one backend operation for this worker's client.

    :param backend: Initialized PJSUA2 backend.
    :param client: Worker-owned SIP client.
    :param credentials: Worker-owned SIP credentials.
    :param action: Backend operation name.
    :param payload: Operation arguments.
    :returns: JSON-serializable operation result.
    :raises ValueError: If the operation is unsupported.
    """

    name = client.name
    if action == "register_client":
        return asdict(
            await backend.register_client(
                client,
                credentials,
                timeout=float(payload.get("timeout_seconds", 30)),
            )
        )
    if action == "stay_registered":
        return asdict(
            await backend.stay_registered(
                name,
                float(payload["seconds"]),
                bool(payload.get("require_reregistration", False)),
                int(payload.get("min_reregistrations", 1)),
            )
        )
    if action == "place_call":
        call = await backend.place_call(
            name,
            str(payload["target_uri"]),
            timeout=float(payload.get("timeout", 30)),
            video=bool(payload.get("video", False)),
            caller_id=payload.get("caller_id"),
            pai_caller_id=payload.get("pai_caller_id"),
        )
        return asdict(call)
    if action == "wait_for_incoming":
        call = await backend.wait_for_incoming(
            name,
            timeout=float(payload.get("operation_timeout", 30)),
            from_uri=payload.get("from_uri"),
        )
        return asdict(call)
    if action in {"answer", "reject", "wait_call_state", "hangup", "hold", "resume", "play_wav", "record_wav"}:
        call = _call_from_raw(payload["call"])
        if action == "answer":
            await backend.answer(call, status_code=int(payload.get("status_code", 200)))
            return None
        if action == "reject":
            await backend.reject(call, status_code=int(payload.get("status_code", 486)))
            return None
        if action == "wait_call_state":
            await backend.wait_call_state(call, str(payload["state"]), timeout=float(payload.get("timeout", 30)))
            return None
        if action == "hangup":
            await backend.hangup(call)
            return None
        if action == "hold":
            await backend.hold(call)
            return None
        if action == "resume":
            await backend.resume(call)
            return None
        if action == "play_wav":
            await backend.play_wav(call, Path(payload["path"]))
            return None
        await backend.record_wav(call, Path(payload["output_path"]), float(payload["seconds"]))
        return None
    if action == "attended_transfer":
        await backend.attended_transfer(
            _call_from_raw(payload["primary_call"]), _call_from_raw(payload["consult_call"])
        )
        return None
    if action == "video_smoke":
        return asdict(
            await backend.video_smoke(
                name,
                str(payload["target_uri"]),
                timeout=float(payload.get("operation_timeout", 30)),
            )
        )
    if action == "shutdown":
        return None
    raise ValueError(f"Unsupported worker operation: {action}")


async def _wait_for_unregistration(log_path: Path, timeout: float = 12) -> bool:
    """Wait for PJSIP to log successful account unregistration.

    :param log_path: PJSIP log path for this process.
    :param timeout: Maximum wait in seconds.
    :returns: Whether an unregistration success was observed.
    """

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if "unregistration success" in log_path.read_text(encoding="utf-8", errors="replace").casefold():
                return True
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.1)
    return False


async def _run() -> int:
    """Initialize one endpoint process and serve worker commands.

    :returns: Process exit code.
    :side effect: Starts PJSUA2, binds one signaling transport, and processes controller commands.
    """

    output = _reserve_protocol_stdout()
    startup = await _read_message()
    if startup is None or startup.get("action") != "start":
        raise ValueError("The first worker message must be a start command")
    client = SipClientConfig(**startup["client"])
    credentials = SipCredentials(**startup["credentials"])
    config = LabConfig(clients=(client,), dns_nameservers=tuple(startup.get("dns_nameservers", ())))
    log_path = Path(startup["pjsip_log_path"])
    backend = Pjsua2SipBackend()
    account_registered = False
    call_updates_task: asyncio.Task[None] | None = None
    try:
        await backend.initialize(config, pjsip_log_path=log_path)
        _emit(output, {"type": "event", "event": "ready", "client": client.name, "pid": os.getpid()})
        call_updates_task = asyncio.create_task(_report_call_updates(backend, output))
        pending: set[asyncio.Task[None]] = set()

        async def handle(message: dict[str, Any]) -> None:
            """Run one operation and return a correlated response.

            :param message: Command received from the supervisor.
            :returns: None.
            """

            nonlocal account_registered
            request_id = str(message.get("id", ""))
            action = str(message.get("action", ""))
            try:
                if action == "register_client":
                    # Track even a timed-out registration attempt so shutdown can
                    # cancel a late registration response before destroying PJSUA.
                    account_registered = True
                result = await _execute(backend, client, credentials, action, message)
                _emit(output, {"type": "response", "id": request_id, "ok": True, "result": result})
            except Exception as exc:
                _emit(
                    output,
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )

        while True:
            message = await _read_message()
            if message is None:
                break
            action = str(message.get("action", ""))
            if action == "shutdown":
                break
            task = asyncio.create_task(handle(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
        pending_tasks = tuple(pending)
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        return 0
    except Exception as exc:
        _emit(output, {"type": "event", "event": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if call_updates_task is not None:
            call_updates_task.cancel()
            with suppress(asyncio.CancelledError):
                await call_updates_task
        if account_registered:
            account = backend.accounts.get(client.name)
            if account is not None:
                with suppress(Exception):
                    account.setRegistration(False)
                    _emit(
                        output,
                        {
                            "type": "event",
                            "event": "shutdown_unregistration",
                            "client": client.name,
                            "success": await _wait_for_unregistration(log_path),
                        },
                    )
        await backend.shutdown()


def main() -> int:
    """Run the worker event loop.

    :returns: Child process exit code.
    """

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
