"""Tests for the experimental process-per-endpoint runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wxcalls.backends.base import CallHandle
from wxcalls.backends.fake import FakeSipBackend
from wxcalls.cli import _build_parser
from wxcalls.config import LabConfig, SipClientConfig
from wxcalls.exceptions import BackendError
from wxcalls.exceptions import TimeoutError as WxTimeoutError
from wxcalls.orchestrator import CallLab
from wxcalls.scenario import parse_scenario
from wxcalls.workers.backend import WorkerSipBackend, _scenario_client_names, _Worker


class _FakeStdin:
    """Pipe writer that simulates a JSON-lines worker response."""

    def __init__(self, process: _FakeProcess) -> None:
        """Bind the writer to its fake child process.

        :param process: Process state to update when commands are written.
        :returns: None.
        """

        self.process = process

    def write(self, data: bytes) -> None:
        """Decode a controller command and respond or simulate a crash.

        :param data: Encoded JSON-lines command.
        :returns: None.
        """

        message = json.loads(data)
        if message["action"] == "crash":
            self.process.returncode = 17
            self.process.stdout.feed_eof()
            if not self.process.waiter.done():
                self.process.waiter.set_result(17)
            return
        if message["action"] == "timeout":
            response = {
                "type": "response",
                "id": message["id"],
                "ok": False,
                "error_type": "TimeoutError",
                "error": "injected incoming-call timeout",
            }
            self.process.stdout.feed_data((json.dumps(response) + "\n").encode())
            return
        if message["action"] == "call_update":
            update = {
                "type": "event",
                "event": "call_update",
                "call_id": "call-1",
                "state": "connected",
                "media_active": True,
                "video_active": False,
            }
            self.process.stdout.feed_data((json.dumps(update) + "\n").encode())
        response = {"type": "response", "id": message["id"], "ok": True, "result": {"action": message["action"]}}
        self.process.stdout.feed_data((json.dumps(response) + "\n").encode())

    async def drain(self) -> None:
        """Match the asyncio subprocess stream-writer interface.

        :returns: None.
        """


class _FakeProcess:
    """Minimal subprocess substitute for worker protocol tests."""

    def __init__(self) -> None:
        """Create in-memory stdin/stdout pipes and process status.

        :returns: None.
        """

        self.stdout = asyncio.StreamReader()
        self.waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.stdin = _FakeStdin(self)
        self.returncode: int | None = None
        self.pid = 4321

    async def wait(self) -> int:
        """Wait for simulated process termination.

        :returns: Child exit status.
        """

        return await self.waiter

    def terminate(self) -> None:
        """Terminate the simulated process.

        :returns: None.
        """

    def kill(self) -> None:
        """Kill the simulated process.

        :returns: None.
        """


class _LifecycleBackend(FakeSipBackend):
    """Fake backend recording experimental scenario lifecycle calls."""

    def __init__(self) -> None:
        """Initialize lifecycle event storage.

        :returns: None.
        """

        super().__init__()
        self.lifecycle: list[str] = []

    async def begin_scenario(self, scenario: object) -> None:
        """Record worker startup for a scenario.

        :param scenario: Scenario entering execution.
        :returns: None.
        """

        self.lifecycle.append("begin")

    async def end_scenario(self, scenario: object) -> None:
        """Record worker cleanup for a scenario.

        :param scenario: Scenario leaving execution.
        :returns: None.
        """

        self.lifecycle.append("end")


class _FailingMonitorBackend(_LifecycleBackend):
    """Lifecycle backend that reports an asynchronous worker failure."""

    async def wait_for_failure(self) -> None:
        """Raise after a brief delay to simulate a worker exiting mid-scenario.

        :returns: None.
        :raises BackendError: Always, to simulate a failed endpoint process.
        """

        await asyncio.sleep(0.01)
        raise BackendError("injected worker failure")


class _FailingCallLab(CallLab):
    """Call lab that injects a scenario failure for cleanup validation."""

    async def _run_scenario_steps(self, scenario: object) -> None:
        """Fail scenario execution after worker startup.

        :param scenario: Scenario being executed.
        :returns: None.
        :raises RuntimeError: Always, to exercise the cleanup path.
        """

        raise RuntimeError("injected scenario failure")


class _BlockingCallLab(CallLab):
    """Call lab whose scenario blocks until a worker failure cancels it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Create a blocking lab and initialize cancellation evidence.

        :param args: Positional base class arguments.
        :param kwargs: Keyword base class arguments.
        :returns: None.
        """

        super().__init__(*args, **kwargs)
        self.scenario_cancelled = False

    async def _run_scenario_steps(self, scenario: object) -> None:
        """Block scenario progress until the monitor cancels it.

        :param scenario: Scenario being executed.
        :returns: None.
        :raises asyncio.CancelledError: When a worker failure interrupts execution.
        """

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.scenario_cancelled = True
            raise


def test_workers_cli_parses_experimental_run_options() -> None:
    """Verify the experimental runner has its own nested CLI command.

    :returns: None.
    """

    args = _build_parser().parse_args(["workers", "run", "-c", "config.yml", "--env-file", "test.env", "scenario.yml"])

    assert args.command == "workers"
    assert args.worker_command == "run"
    assert args.config == "config.yml"
    assert args.env_file == "test.env"
    assert args.scenarios == ["scenario.yml"]

    existing_args = _build_parser().parse_args(["run", "-c", "config.yml", "--backend", "fake", "scenario.yml"])
    assert existing_args.command == "run"
    assert existing_args.backend == "fake"


def test_scenario_worker_selection_includes_endpoints_and_nested_steps() -> None:
    """Collect all clients that can be accessed by scenario execution.

    :returns: None.
    """

    scenario = parse_scenario(
        {
            "name": "workers",
            "behaviors": {
                "idle": {
                    "initial_state": "idle",
                    "states": {"idle": {"entry": [{"action": "set_counter", "name": "count", "value": 0}]}},
                },
            },
            "endpoints": {"endpoint_a": {"client": "alice", "behavior": "idle"}},
            "steps": [
                {"action": "register", "client": ["bob"]},
                {"action": "register", "clients": ["dave"]},
                {
                    "action": "parallel",
                    "branches": {"inbound": [{"action": "expect_incoming", "client": "carol"}]},
                },
            ],
        }
    )

    assert _scenario_client_names(scenario) == {"alice", "bob", "carol", "dave"}


def test_worker_protocol_routes_command_response_and_reports_crash(tmp_path: Path) -> None:
    """Verify JSON response routing and worker crash evidence.

    :param tmp_path: Temporary location for worker diagnostics.
    :returns: None.
    """

    async def exercise() -> None:
        """Run protocol requests against an in-memory child process.

        :returns: None.
        """

        process = _FakeProcess()
        events: list[tuple[str, dict[str, Any]]] = []

        def record_event(event: str, fields: dict[str, Any]) -> None:
            """Capture and forward worker failure events to the backend monitor.

            :param event: Lifecycle event name.
            :param fields: Event details.
            :returns: None.
            """

            events.append((event, fields))
            backend._worker_event(event, fields)

        backend = WorkerSipBackend()
        call = CallHandle("call-1", "alice", "sip:bob@example.test", state="calling")
        backend.call_handles[call.id] = call
        worker = _Worker(
            SipClientConfig("alice", "sip:alice@example.test", "sip:example.test", "USER", "PASS"),
            process,  # type: ignore[arg-type]
            tmp_path / "pjsip.log",
            tmp_path / "stderr.log",
            ("secret-user", "secret-pass"),
            record_event,
        )
        worker.reader_task = asyncio.create_task(worker._read_responses())

        result = await worker.request("register_client", endpoint="alice")
        assert result == {"action": "register_client"}
        await worker.request("call_update")
        assert call.state == "connected"
        assert call.media_active

        with pytest.raises(WxTimeoutError, match="injected incoming-call timeout"):
            await worker.request("timeout")

        with pytest.raises(BackendError, match="exited with code 17"):
            await worker.request("crash", timeout=1)
        await worker.reader_task
        with pytest.raises(BackendError, match="Worker alice exited unexpectedly with code 17"):
            await backend.wait_for_failure()
        assert events[0] == (
            "call_update",
            {
                "call_id": "call-1",
                "state": "connected",
                "media_active": True,
                "video_active": False,
            },
        )
        assert events[1] == (
            "worker_failed",
            {
                "client": "alice",
                "pid": 4321,
                "exit_code": 17,
                "last_action": "crash",
                "last_state": "crash:in_progress",
                "pjsip_log": str(tmp_path / "pjsip.log"),
            },
        )

    asyncio.run(exercise())


def test_worker_log_redaction_removes_credentials_and_auth_headers(tmp_path: Path) -> None:
    """Ensure worker evidence does not retain raw or digest credentials.

    :param tmp_path: Temporary location for worker diagnostics.
    :returns: None.
    """

    async def exercise() -> None:
        """Create and redact test log files.

        :returns: None.
        """

        process = _FakeProcess()
        pjsip_path = tmp_path / "pjsip.log"
        stderr_path = tmp_path / "stderr.log"
        for path in (pjsip_path, stderr_path):
            path.write_text(
                'Authorization: Digest username="secret-user", response="digest-proof"\nsecret-pass\n',
                encoding="utf-8",
            )
        worker = _Worker(
            SipClientConfig("alice", "sip:alice@example.test", "sip:example.test", "USER", "PASS"),
            process,  # type: ignore[arg-type]
            pjsip_path,
            stderr_path,
            ("secret-user", "secret-pass"),
            lambda event, fields: None,
        )
        worker._redact_logs()

        for path in (pjsip_path, stderr_path):
            contents = path.read_text(encoding="utf-8")
            assert "secret-user" not in contents
            assert "secret-pass" not in contents
            assert "digest-proof" not in contents

    asyncio.run(exercise())


def test_call_lab_ends_worker_scenario_when_execution_fails(tmp_path: Path) -> None:
    """Always stop scenario workers when a scenario action raises.

    :param tmp_path: Temporary artifact directory.
    :returns: None.
    """

    config = LabConfig(
        clients=(SipClientConfig("alice", "sip:alice@example.test", "sip:example.test", "USER", "PASS"),),
        artifacts_dir=tmp_path,
    )
    backend = _LifecycleBackend()
    lab = _FailingCallLab(config, backend=backend)
    scenario = parse_scenario({"name": "failure", "steps": [{"action": "hangup", "call": "missing"}]})

    async def exercise() -> None:
        """Run failing scenario and check lifecycle ordering.

        :returns: None.
        """

        with pytest.raises(RuntimeError, match="injected scenario failure"):
            await lab.run_scenario(scenario)

    asyncio.run(exercise())
    assert backend.lifecycle == ["begin", "end"]


def test_call_lab_fails_and_cancels_scenario_when_worker_exits(tmp_path: Path) -> None:
    """Propagate asynchronous worker failures and clean up scenario tasks.

    :param tmp_path: Temporary artifact directory.
    :returns: None.
    """

    config = LabConfig(
        clients=(SipClientConfig("alice", "sip:alice@example.test", "sip:example.test", "USER", "PASS"),),
        artifacts_dir=tmp_path,
    )
    backend = _FailingMonitorBackend()
    lab = _BlockingCallLab(config, backend=backend)
    scenario = parse_scenario({"name": "failure", "steps": [{"action": "hangup", "call": "missing"}]})

    async def exercise() -> None:
        """Run until the simulated worker failure is observed.

        :returns: None.
        """

        with pytest.raises(BackendError, match="injected worker failure"):
            await lab.run_scenario(scenario)

    asyncio.run(exercise())
    assert lab.scenario_cancelled
    assert backend.lifecycle == ["begin", "end"]
