"""YAML scenario schema and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from wxcalls.exceptions import ScenarioError


@dataclass(frozen=True)
class ScenarioStep:
    """One validated scenario step.

    :param action: Step action name.
    :param params: Step-specific parameters.
    :param index: One-based position in the scenario document.
    """

    action: str
    params: dict[str, Any]
    index: int

    REQUIRED_BY_ACTION: ClassVar[dict[str, tuple[str, ...]]] = {
        "register": (),
        "call": ("client", "target"),
        "expect_incoming": ("client",),
        "answer": ("call",),
        "reject": ("call",),
        "wait_state": ("call", "state"),
        "play_tts": ("call", "text"),
        "play_wav": ("call", "path"),
        "record": ("call",),
        "assert_marker": ("recording", "marker"),
        "hold": ("call",),
        "resume": ("call",),
        "consult_call": ("client", "target"),
        "attended_transfer": ("primary_call", "consult_call"),
        "hangup": (),
        "video_smoke": ("client", "target"),
    }

    @classmethod
    def from_raw(cls, raw: Any, index: int) -> ScenarioStep:
        """Validate and create a scenario step from raw YAML.

        :param raw: Raw YAML value.
        :param index: One-based step index.
        :returns: Validated scenario step.
        :raises ScenarioError: If the step is malformed.
        """

        if not isinstance(raw, dict):
            raise ScenarioError(f"Step {index} must be a mapping")
        action = raw.get("action", raw.get("type"))
        if not action:
            raise ScenarioError(f"Step {index} requires an action")
        action = str(action)
        required = cls.REQUIRED_BY_ACTION.get(action)
        if required is None:
            supported = ", ".join(sorted(cls.REQUIRED_BY_ACTION))
            raise ScenarioError(
                f"Step {index} has unsupported action {action!r}; supported: {supported}"
            )
        params = {str(key): value for key, value in raw.items() if key not in {"action", "type"}}
        missing = [
            field for field in required if field not in params or params[field] in (None, "")
        ]
        if missing:
            raise ScenarioError(
                f"Step {index} action {action!r} missing required field(s): {', '.join(missing)}"
            )
        _validate_common_fields(action, params, index)
        return cls(action=action, params=params, index=index)


@dataclass(frozen=True)
class Scenario:
    """A validated call-flow scenario.

    :param name: Human-readable scenario name.
    :param steps: Ordered validated scenario steps.
    :param source: Source file path, if loaded from disk.
    """

    name: str
    steps: tuple[ScenarioStep, ...]
    source: Path | None = None


def load_scenario(path: str | Path) -> Scenario:
    """Load and validate a YAML scenario file.

    :param path: Scenario YAML path.
    :returns: Parsed scenario.
    :raises ScenarioError: If the document is invalid.
    """

    scenario_path = Path(path)
    try:
        raw = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ScenarioError(f"Scenario file not found: {scenario_path}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"Invalid YAML in scenario file {scenario_path}: {exc}") from exc
    return parse_scenario(raw, source=scenario_path)


def parse_scenario(raw: Any, source: Path | None = None) -> Scenario:
    """Parse a scenario from raw YAML data.

    :param raw: Raw parsed YAML data.
    :param source: Optional source path.
    :returns: Parsed scenario.
    :raises ScenarioError: If the document is invalid.
    """

    if not isinstance(raw, dict):
        raise ScenarioError("Scenario root must be a mapping")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScenarioError("Scenario requires a non-empty steps list")
    steps = tuple(
        ScenarioStep.from_raw(step, index) for index, step in enumerate(steps_raw, start=1)
    )
    name = str(raw.get("name") or (source.stem if source else "scenario"))
    return Scenario(name=name, steps=steps, source=source)


def _validate_common_fields(action: str, params: dict[str, Any], index: int) -> None:
    timeout = params.get("timeout")
    if timeout is not None and (not isinstance(timeout, int | float) or timeout <= 0):
        raise ScenarioError(f"Step {index} action {action!r} timeout must be a positive number")
    seconds = params.get("seconds")
    if seconds is not None and (not isinstance(seconds, int | float) or seconds <= 0):
        raise ScenarioError(f"Step {index} action {action!r} seconds must be a positive number")
    if action == "register" and "client" not in params and "clients" not in params:
        raise ScenarioError(f"Step {index} action 'register' requires client or clients")
    if action == "hangup" and "call" not in params and "calls" not in params:
        raise ScenarioError(f"Step {index} action 'hangup' requires call or calls")
