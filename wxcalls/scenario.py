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
        # The scenario schema is deliberately action-centric: each action owns the
        # minimal fields needed by its orchestrator step handler.
        "register": (),
        "call": ("client", "target"),
        "expect_incoming": ("client",),
        "answer": ("call",),
        "reject": ("call",),
        "wait_state": ("call", "state"),
        "wait_media": ("call",),
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
        "parallel": ("branches",),
        "record_during_playback": ("playback_call", "recording_call"),
    }

    @classmethod
    def from_raw(cls, raw: Any, index: int, context: str | None = None) -> ScenarioStep:
        """Validate and create a scenario step from raw YAML.

        :param raw: Raw YAML value.
        :param index: One-based step index.
        :param context: Optional human-readable location for nested steps.
        :returns: Validated scenario step.
        :raises ScenarioError: If the step is malformed.
        """

        location = context or f"Step {index}"
        if not isinstance(raw, dict):
            raise ScenarioError(f"{location} must be a mapping")
        action = raw.get("action", raw.get("type"))
        if not action:
            raise ScenarioError(f"{location} requires an action")
        action = str(action)
        required = cls.REQUIRED_BY_ACTION.get(action)
        if required is None:
            supported = ", ".join(sorted(cls.REQUIRED_BY_ACTION))
            raise ScenarioError(f"{location} has unsupported action {action!r}; supported: {supported}")
        params = {str(key): value for key, value in raw.items() if key not in {"action", "type"}}
        missing = [field for field in required if field not in params or params[field] in (None, "")]
        if missing:
            raise ScenarioError(f"{location} action {action!r} missing required field(s): {', '.join(missing)}")
        if action == "parallel":
            params["branches"] = _parse_parallel_branches(params["branches"], location)
        _validate_common_fields(action, params, location)
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
    steps = tuple(ScenarioStep.from_raw(step, index) for index, step in enumerate(steps_raw, start=1))
    name = str(raw.get("name") or (source.stem if source else "scenario"))
    return Scenario(name=name, steps=steps, source=source)


def _parse_parallel_branches(raw: Any, location: str) -> dict[str, tuple[ScenarioStep, ...]]:
    """Validate and create nested scenario steps for a ``parallel`` action.

    :param raw: Raw branch mapping from YAML.
    :param location: Human-readable parent step location for error messages.
    :returns: Mapping of branch names to validated branch steps.
    :raises ScenarioError: If branches are malformed.
    """

    if not isinstance(raw, dict) or not raw:
        raise ScenarioError(f"{location} action 'parallel' branches must be a non-empty mapping")

    branches: dict[str, tuple[ScenarioStep, ...]] = {}
    for branch_name, steps_raw in raw.items():
        name = str(branch_name).strip()
        if not name:
            raise ScenarioError(f"{location} action 'parallel' branch names must be non-empty")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ScenarioError(f"{location} action 'parallel' branch {name!r} must be a non-empty steps list")
        branches[name] = tuple(
            ScenarioStep.from_raw(
                branch_step,
                step_index,
                context=f"{location} branch {name!r} step {step_index}",
            )
            for step_index, branch_step in enumerate(steps_raw, start=1)
        )
    return branches


def _validate_common_fields(action: str, params: dict[str, Any], location: str) -> None:
    """Validate fields shared by multiple scenario actions.

    :param action: Scenario action name.
    :param params: Step parameters after action/type removal.
    :param location: Human-readable step location for error messages.
    :returns: None.
    :raises ScenarioError: If shared fields are invalid.
    """

    timeout = params.get("timeout")
    if timeout is not None and (not isinstance(timeout, int | float) or timeout <= 0):
        raise ScenarioError(f"{location} action {action!r} timeout must be a positive number")
    seconds = params.get("seconds")
    if seconds is not None and (not isinstance(seconds, int | float) or seconds <= 0):
        raise ScenarioError(f"{location} action {action!r} seconds must be a positive number")
    for roll_name in ("pre_roll", "post_roll"):
        roll = params.get(roll_name)
        if roll is not None and (not isinstance(roll, int | float) or roll < 0):
            raise ScenarioError(f"{location} action {action!r} {roll_name} must be a non-negative number")
    if action == "register" and "client" not in params and "clients" not in params:
        raise ScenarioError(f"{location} action 'register' requires client or clients")
    if action == "record_during_playback":
        has_text = bool(params.get("text"))
        has_path = bool(params.get("path"))
        if has_text == has_path:
            raise ScenarioError(f"{location} action 'record_during_playback' requires exactly one of text or path")
        if "assert_marker" in params and not isinstance(params["assert_marker"], bool):
            raise ScenarioError(f"{location} action {action!r} assert_marker must be boolean")
    stay_registered_for = params.get("stay_registered_for")
    if stay_registered_for is not None and (
        not isinstance(stay_registered_for, int | float) or stay_registered_for <= 0
    ):
        raise ScenarioError(f"{location} action {action!r} stay_registered_for must be a positive number")
    if "stay_registered" in params and not isinstance(params["stay_registered"], bool):
        raise ScenarioError(f"{location} action {action!r} stay_registered must be boolean")
    if "require_reregistration" in params and not isinstance(params["require_reregistration"], bool):
        raise ScenarioError(f"{location} action {action!r} require_reregistration must be boolean")
    if "use_target_extension" in params and not isinstance(params["use_target_extension"], bool):
        raise ScenarioError(f"{location} action {action!r} use_target_extension must be boolean")
    min_reregistrations = params.get("min_reregistrations")
    if min_reregistrations is not None and (not isinstance(min_reregistrations, int) or min_reregistrations <= 0):
        raise ScenarioError(f"{location} action {action!r} min_reregistrations must be a positive integer")
    if action == "hangup" and "call" not in params and "calls" not in params:
        raise ScenarioError(f"{location} action 'hangup' requires call or calls")
