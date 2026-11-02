"""Tests for the local worker dashboard state model."""

from wxcalls.dashboard import WorkerDashboard


def test_dashboard_tracks_worker_lifecycle_and_behavior_state() -> None:
    """Keep the latest scenario state and worker status in the dashboard snapshot.

    :returns: None.
    """

    dashboard = WorkerDashboard()
    dashboard.publish("scenario_started", {"name": "call-flow"})
    dashboard.publish("worker_starting", {"client": "alice"})
    dashboard.publish("worker_ready", {"client": "alice", "pid": 42, "local_port": 5061})
    dashboard.publish(
        "scenario_state",
        {"endpoint": "alice", "state": "connected", "activity": "caller · call_state · -"},
    )

    snapshot = dashboard.snapshot()

    assert snapshot["scenario"] == "call-flow"
    assert snapshot["status"] == "Running"
    assert snapshot["workers"] == [
        {
            "client": "alice",
            "status": "running",
            "scenario_state": "connected",
            "state_history": ["connected"],
            "activity": "caller · call_state · -",
            "waiting_for": "",
            "state_since": snapshot["workers"][0]["state_since"],
            "pid": 42,
            "local_port": 5061,
        }
    ]
