"""Local live dashboard for scenario worker activity."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any

_PAGE = files("wxcalls").joinpath("dashboard.html").read_text(encoding="utf-8")


class WorkerDashboard:
    """Serve a local dashboard and retain the latest worker state in memory.

    :param host: Local interface to bind.
    :param port: TCP port to serve.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Initialize dashboard state before a run starts.

        :param host: Local interface to bind.
        :param port: TCP port to serve.
        :returns: None.
        """

        self.host = host
        self.port = port
        self.scenario = ""
        self.status = "Waiting for run"
        self.workers: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the local HTTP server in a daemon thread.

        :returns: None.
        :raises OSError: If the requested address is unavailable.
        """

        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            """Serve the dashboard page and current state snapshot."""

            def do_GET(self) -> None:
                """Return dashboard HTML or its JSON state endpoint.

                :returns: None.
                """

                if self.path == "/api/state":
                    body = json.dumps(dashboard.snapshot()).encode()
                    content_type = "application/json; charset=utf-8"
                elif self.path == "/" or self.path.startswith("/?"):
                    body = _PAGE.encode()
                    content_type = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                """Keep routine browser polling out of the console log.

                :param format: Log message format.
                :param args: Format values.
                :returns: None.
                """

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the HTTP server and release its local port.

        :returns: None.
        """

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def publish(self, event: str, fields: dict[str, Any]) -> None:
        """Update dashboard state from an orchestrator or worker event.

        :param event: Event name.
        :param fields: Event fields.
        :returns: None.
        """

        now = datetime.now(UTC).isoformat()
        with self._lock:
            if event == "scenario_started":
                self.scenario = str(fields.get("name", "Scenario"))
                self.status = "Running"
            elif event == "scenario_finished":
                self.status = "Completed"
                for worker in self.workers.values():
                    if worker["status"] not in {"failed", "completed"}:
                        worker["status"] = "completed"
                        worker["state_since"] = now
            elif event == "scenario_failed":
                self.status = "Failed"
            client = str(fields.get("client", fields.get("endpoint", "")))
            if event == "scenario_state" and client:
                worker = self.workers.setdefault(client, self._new_worker(client))
                state = str(fields.get("state", ""))
                if state and state != worker.get("scenario_state"):
                    worker["state_history"].append(state)
                worker["scenario_state"] = state
                worker["activity"] = str(fields.get("activity", ""))
                worker["state_since"] = now
                worker["status"] = "running"
                worker["has_behavior_state"] = True
            elif (
                event
                in {
                    "worker_starting",
                    "worker_ready",
                    "worker_action",
                    "call_update",
                    "worker_failed",
                    "worker_stopped",
                }
                and client
            ):
                worker = self.workers.setdefault(client, self._new_worker(client))
                worker["state_since"] = now
                if event == "worker_starting":
                    worker.update(status="starting", activity="Starting endpoint")
                elif event == "worker_ready":
                    worker.update(
                        status="running",
                        activity="Worker ready",
                        pid=fields.get("pid"),
                        local_port=fields.get("local_port"),
                    )
                elif event == "worker_action":
                    action = str(fields.get("action", ""))
                    worker["activity"] = action
                    worker["status"] = (
                        "waiting"
                        if fields.get("phase") == "started" and action.startswith(("wait", "stay"))
                        else "running"
                    )
                    worker["waiting_for"] = action if worker["status"] == "waiting" else ""
                    worker["wait_limit"] = fields.get("wait_limit") if worker["status"] == "waiting" else None
                    if not worker.get("has_behavior_state"):
                        if action and action != worker.get("scenario_state"):
                            worker["state_history"].append(action)
                        worker["scenario_state"] = action
                        worker["state_since"] = now
                elif event == "call_update":
                    worker["activity"] = f"Call {fields.get('state', 'updated')}"
                elif event == "worker_failed":
                    worker.update(
                        status="failed",
                        attention=f"Worker exited (code {fields.get('exit_code', '?')})",
                        activity=str(fields.get("last_state", "")),
                    )
                elif event == "worker_stopped" and self.status == "Running":
                    worker["status"] = "completed"

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the current run.

        :returns: Scenario and worker state.
        """

        with self._lock:
            now = datetime.now(UTC)
            rows = []
            for worker in self.workers.values():
                row = dict(worker)
                row.pop("has_behavior_state", None)
                try:
                    age = (now - datetime.fromisoformat(str(worker["state_since"]))).total_seconds()
                except (KeyError, ValueError):
                    age = 0
                if not row.get("attention") and row["status"] == "waiting" and row.get("wait_limit"):
                    if age >= float(row["wait_limit"]):
                        row["attention"] = f"Wait limit exceeded · {int(row['wait_limit'])}s"
                elif not row.get("attention") and row["status"] == "running" and age >= 120:
                    row["attention"] = "No progress · 120s"
                rows.append(row)
            return {"scenario": self.scenario, "status": self.status, "workers": rows}

    @staticmethod
    def _new_worker(client: str) -> dict[str, Any]:
        """Create an initial row for a worker that emits an early event.

        :param client: Worker name.
        :returns: Mutable worker row.
        """

        return {
            "client": client,
            "status": "starting",
            "scenario_state": "",
            "state_history": [],
            "activity": "",
            "waiting_for": "",
            "state_since": datetime.now(UTC).isoformat(),
        }
