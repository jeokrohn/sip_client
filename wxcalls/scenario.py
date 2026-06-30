"""YAML scenario schema and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from wxcalls.exceptions import ScenarioError

SUPPORTED_BEHAVIOR_EVENTS = frozenset(
    {
        "registered",
        "scenario_trigger",
        "call_received",
        "call_state",
        "media_active",
        "timer_expired",
        "counter_reached",
    }
)
BEHAVIOR_ONLY_ACTIONS = frozenset(
    {"start_timer", "cancel_timer", "set_counter", "increment_counter", "decrement_counter"}
)
DISALLOWED_BEHAVIOR_ACTIONS = frozenset(
    {"parallel", "expect_incoming", "video_smoke", "wait_behavior_state", "trigger_behavior"}
)


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
        "wait_behavior_state": ("endpoint", "state"),
        "trigger_behavior": ("endpoint", "name"),
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
class BehaviorAction:
    """One action executed by an endpoint behavior rule.

    :param action: Behavior or reused scenario action name.
    :param params: Action-specific parameters.
    :param index: One-based position in the rule action list.
    """

    action: str
    params: dict[str, Any]
    index: int

    @classmethod
    def from_raw(cls, raw: Any, index: int, context: str, allow_register: bool = False) -> BehaviorAction:
        """Validate and create a behavior action from raw YAML.

        :param raw: Raw YAML value.
        :param index: One-based action index within the rule.
        :param context: Human-readable location for error messages.
        :param allow_register: Whether ``register`` is allowed in this action context.
        :returns: Validated behavior action.
        :raises ScenarioError: If the action is malformed or unsupported.
        """

        location = f"{context} action {index}"
        if not isinstance(raw, dict):
            raise ScenarioError(f"{location} must be a mapping")
        action = raw.get("action", raw.get("type"))
        if not action:
            raise ScenarioError(f"{location} requires an action")
        action = str(action)
        params = {str(key): value for key, value in raw.items() if key not in {"action", "type"}}
        _validate_behavior_action_alias_reuse(action, params, location)

        if action == "register":
            if not allow_register:
                raise ScenarioError(
                    f"{location} action 'register' is only supported in endpoint behavior entry actions"
                )
            _validate_common_fields(action, params, location, require_register_client=False)
            return cls(action=action, params=params, index=index)
        if action in BEHAVIOR_ONLY_ACTIONS:
            _validate_behavior_only_action(action, params, location)
            return cls(action=action, params=params, index=index)
        if action in DISALLOWED_BEHAVIOR_ACTIONS:
            raise ScenarioError(f"{location} action {action!r} is not supported inside endpoint behaviors")
        if action not in ScenarioStep.REQUIRED_BY_ACTION:
            supported = ", ".join(sorted(_supported_behavior_actions(allow_register=allow_register)))
            raise ScenarioError(f"{location} has unsupported action {action!r}; supported: {supported}")

        if action in {"call", "consult_call"} and "client" not in params:
            missing = [
                field
                for field in ScenarioStep.REQUIRED_BY_ACTION[action]
                if field != "client" and (field not in params or params[field] in (None, ""))
            ]
            if missing:
                raise ScenarioError(f"{location} action {action!r} missing required field(s): {', '.join(missing)}")
            _validate_common_fields(action, params, location)
            return cls(action=action, params=params, index=index)

        step = ScenarioStep.from_raw(raw, index=index, context=location)
        return cls(action=step.action, params=step.params, index=index)


@dataclass(frozen=True)
class BehaviorRule:
    """One event-condition-action rule in an endpoint behavior state.

    :param event: Event name that triggers the rule.
    :param params: Event-specific matching parameters.
    :param actions: Actions to run when the rule matches.
    :param next_state: Optional state to transition to after actions.
    :param save_call_as: Optional behavior-local alias for received calls.
    """

    event: str
    params: dict[str, Any]
    actions: tuple[BehaviorAction, ...]
    next_state: str | None = None
    save_call_as: str | None = None

    @classmethod
    def from_raw(cls, event: str, raw: Any, context: str) -> BehaviorRule:
        """Validate and create a behavior rule from raw YAML.

        :param event: Event name from the state's ``on`` mapping.
        :param raw: Raw rule mapping.
        :param context: Human-readable location for error messages.
        :returns: Validated behavior rule.
        :raises ScenarioError: If the rule is malformed.
        """

        if event not in SUPPORTED_BEHAVIOR_EVENTS:
            supported = ", ".join(sorted(SUPPORTED_BEHAVIOR_EVENTS))
            raise ScenarioError(f"{context} has unsupported event {event!r}; supported: {supported}")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ScenarioError(f"{context} event {event!r} must be a mapping")

        actions_raw = raw.get("actions", [])
        if not isinstance(actions_raw, list):
            raise ScenarioError(f"{context} event {event!r} actions must be a list")
        actions = tuple(
            BehaviorAction.from_raw(action, index, f"{context} event {event!r}")
            for index, action in enumerate(actions_raw, start=1)
        )
        next_state = _optional_non_empty_string(raw.get("next_state"), f"{context} event {event!r} next_state")
        save_call_as = _optional_non_empty_string(raw.get("save_call_as"), f"{context} event {event!r} save_call_as")
        params = {str(key): value for key, value in raw.items() if key not in {"actions", "next_state", "save_call_as"}}
        _validate_behavior_event_params(event, params, save_call_as, context)
        return cls(event=event, params=params, actions=actions, next_state=next_state, save_call_as=save_call_as)


@dataclass(frozen=True)
class BehaviorState:
    """One named state in an endpoint behavior definition.

    :param name: State name.
    :param entry_actions: Actions run whenever the behavior enters this state.
    :param rules: Event rules active while the behavior is in this state.
    """

    name: str
    entry_actions: tuple[BehaviorAction, ...]
    rules: tuple[BehaviorRule, ...]

    @classmethod
    def from_raw(cls, name: str, raw: Any, context: str) -> BehaviorState:
        """Validate and create a behavior state from raw YAML.

        :param name: State name.
        :param raw: Raw state mapping.
        :param context: Human-readable location for error messages.
        :returns: Validated behavior state.
        :raises ScenarioError: If the state is malformed.
        """

        if not isinstance(raw, dict):
            raise ScenarioError(f"{context} state {name!r} must be a mapping")
        entry_raw = raw.get("entry", [])
        if not isinstance(entry_raw, list):
            raise ScenarioError(f"{context} state {name!r} entry must be a list")
        entry_actions = tuple(
            BehaviorAction.from_raw(
                entry_action,
                entry_index,
                f"{context} state {name!r} entry",
                allow_register=True,
            )
            for entry_index, entry_action in enumerate(entry_raw, start=1)
        )
        # PyYAML follows YAML 1.1 booleans, where an unquoted ``on`` key is
        # parsed as ``True``. Accept both forms so scenario authors can use the
        # natural state-machine spelling.
        on_raw = raw.get("on", raw.get(True, {}))
        if not isinstance(on_raw, dict):
            raise ScenarioError(f"{context} state {name!r} on must be a mapping")
        if not entry_actions and not on_raw:
            raise ScenarioError(f"{context} state {name!r} requires entry actions or a non-empty on mapping")
        rules = tuple(
            BehaviorRule.from_raw(str(event), rule_raw, f"{context} state {name!r}")
            for event, rule_raw in on_raw.items()
        )
        return cls(name=name, entry_actions=entry_actions, rules=rules)


@dataclass(frozen=True)
class BehaviorDefinition:
    """Reusable endpoint behavior definition.

    :param name: Behavior name.
    :param initial_state: State where endpoints start.
    :param states: Named behavior states.
    """

    name: str
    initial_state: str
    states: dict[str, BehaviorState]

    @classmethod
    def from_raw(cls, name: str, raw: Any) -> BehaviorDefinition:
        """Validate and create a behavior definition from raw YAML.

        :param name: Behavior name.
        :param raw: Raw behavior mapping.
        :returns: Validated behavior definition.
        :raises ScenarioError: If the behavior is malformed.
        """

        context = f"Behavior {name!r}"
        if not name:
            raise ScenarioError("Behavior names must be non-empty")
        if not isinstance(raw, dict):
            raise ScenarioError(f"{context} must be a mapping")
        initial_state = _required_string(raw.get("initial_state"), f"{context} initial_state")
        states_raw = raw.get("states")
        if not isinstance(states_raw, dict) or not states_raw:
            raise ScenarioError(f"{context} requires a non-empty states mapping")

        states: dict[str, BehaviorState] = {}
        for state_name, state_raw in states_raw.items():
            state_key = str(state_name).strip()
            if not state_key:
                raise ScenarioError(f"{context} state names must be non-empty")
            states[state_key] = BehaviorState.from_raw(state_key, state_raw, context)
        if initial_state not in states:
            raise ScenarioError(f"{context} initial_state {initial_state!r} is not defined")
        for state in states.values():
            for rule in state.rules:
                if rule.next_state is not None and rule.next_state not in states:
                    raise ScenarioError(
                        f"{context} state {state.name!r} event {rule.event!r} "
                        f"references unknown next_state {rule.next_state!r}"
                    )
        return cls(name=name, initial_state=initial_state, states=states)


@dataclass(frozen=True)
class EndpointBehavior:
    """Assignment of a reusable behavior to a configured endpoint.

    :param name: Endpoint behavior instance name.
    :param client: Logical lab client controlled by this endpoint behavior.
    :param behavior: Name of the assigned behavior definition.
    """

    name: str
    client: str
    behavior: str

    @classmethod
    def from_raw(cls, name: str, raw: Any, behavior_names: set[str]) -> EndpointBehavior:
        """Validate and create an endpoint behavior assignment.

        :param name: Endpoint behavior instance name.
        :param raw: Raw endpoint mapping.
        :param behavior_names: Defined behavior names.
        :returns: Validated endpoint behavior assignment.
        :raises ScenarioError: If the assignment is malformed.
        """

        context = f"Endpoint {name!r}"
        if not name:
            raise ScenarioError("Endpoint names must be non-empty")
        if not isinstance(raw, dict):
            raise ScenarioError(f"{context} must be a mapping")
        client = _required_string(raw.get("client"), f"{context} client")
        behavior = _required_string(raw.get("behavior"), f"{context} behavior")
        if behavior not in behavior_names:
            raise ScenarioError(f"{context} references unknown behavior {behavior!r}")
        return cls(name=name, client=client, behavior=behavior)


@dataclass(frozen=True)
class Scenario:
    """A validated call-flow scenario.

    :param name: Human-readable scenario name.
    :param steps: Ordered validated scenario steps.
    :param source: Source file path, if loaded from disk.
    :param behaviors: Reusable endpoint behavior definitions.
    :param endpoints: Endpoint behavior assignments.
    """

    name: str
    steps: tuple[ScenarioStep, ...]
    source: Path | None = None
    behaviors: dict[str, BehaviorDefinition] | None = None
    endpoints: dict[str, EndpointBehavior] | None = None


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
    behaviors = _parse_behaviors(raw.get("behaviors", {}))
    endpoints = _parse_endpoints(raw.get("endpoints", {}), set(behaviors))
    name = str(raw.get("name") or (source.stem if source else "scenario"))
    return Scenario(name=name, steps=steps, source=source, behaviors=behaviors, endpoints=endpoints)


def _parse_behaviors(raw: Any) -> dict[str, BehaviorDefinition]:
    """Parse scenario endpoint behavior definitions.

    :param raw: Raw ``behaviors`` YAML value.
    :returns: Mapping of behavior names to validated definitions.
    :raises ScenarioError: If the behavior mapping is malformed.
    """

    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ScenarioError("Scenario behaviors must be a mapping")
    behaviors: dict[str, BehaviorDefinition] = {}
    for behavior_name, behavior_raw in raw.items():
        name = str(behavior_name).strip()
        behaviors[name] = BehaviorDefinition.from_raw(name, behavior_raw)
    return behaviors


def _parse_endpoints(raw: Any, behavior_names: set[str]) -> dict[str, EndpointBehavior]:
    """Parse scenario endpoint behavior assignments.

    :param raw: Raw ``endpoints`` YAML value.
    :param behavior_names: Defined behavior names.
    :returns: Mapping of endpoint names to validated assignments.
    :raises ScenarioError: If the endpoint mapping is malformed.
    """

    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ScenarioError("Scenario endpoints must be a mapping")
    endpoints: dict[str, EndpointBehavior] = {}
    for endpoint_name, endpoint_raw in raw.items():
        name = str(endpoint_name).strip()
        endpoints[name] = EndpointBehavior.from_raw(name, endpoint_raw, behavior_names)
    return endpoints


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


def _validate_common_fields(
    action: str,
    params: dict[str, Any],
    location: str,
    require_register_client: bool = True,
) -> None:
    """Validate fields shared by multiple scenario actions.

    :param action: Scenario action name.
    :param params: Step parameters after action/type removal.
    :param location: Human-readable step location for error messages.
    :param require_register_client: Whether register steps must name client(s).
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
    if action == "register" and require_register_client and "client" not in params and "clients" not in params:
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
    if "reuse_alias" in params and not isinstance(params["reuse_alias"], bool):
        raise ScenarioError(f"{location} action {action!r} reuse_alias must be boolean")
    min_reregistrations = params.get("min_reregistrations")
    if min_reregistrations is not None and (not isinstance(min_reregistrations, int) or min_reregistrations <= 0):
        raise ScenarioError(f"{location} action {action!r} min_reregistrations must be a positive integer")
    if action == "hangup" and "call" not in params and "calls" not in params:
        raise ScenarioError(f"{location} action 'hangup' requires call or calls")
    if action == "trigger_behavior":
        params["endpoint"] = _required_string(params.get("endpoint"), f"{location} action 'trigger_behavior' endpoint")
        params["name"] = _required_string(params.get("name"), f"{location} action 'trigger_behavior' name")


def _validate_behavior_action_alias_reuse(action: str, params: dict[str, Any], location: str) -> None:
    """Validate behavior action alias-reuse fields.

    :param action: Behavior action name.
    :param params: Action parameters.
    :param location: Human-readable action location.
    :returns: None.
    :raises ScenarioError: If alias reuse is malformed or attached to an unsupported action.
    """

    if "reuse_alias" not in params:
        return
    if action not in {"call", "consult_call"}:
        raise ScenarioError(f"{location} action {action!r} reuse_alias is only supported for call actions")
    if not isinstance(params["reuse_alias"], bool):
        raise ScenarioError(f"{location} action {action!r} reuse_alias must be boolean")


def _validate_behavior_only_action(action: str, params: dict[str, Any], location: str) -> None:
    """Validate an action that is only available inside endpoint behaviors.

    :param action: Behavior-only action name.
    :param params: Action parameters.
    :param location: Human-readable action location.
    :returns: None.
    :raises ScenarioError: If the action is malformed.
    """

    name = _required_string(params.get("name"), f"{location} name")
    if name != params.get("name"):
        params["name"] = name
    if action in {"set_counter", "increment_counter", "decrement_counter"}:
        _validate_counter_action(action, params, location)
        return
    if action == "start_timer":
        seconds = params.get("seconds")
        if seconds is None:
            raise ScenarioError(f"{location} action 'start_timer' missing required field(s): seconds")
        if not isinstance(seconds, int | float) or seconds <= 0:
            raise ScenarioError(f"{location} action 'start_timer' seconds must be a positive number")


def _validate_counter_action(action: str, params: dict[str, Any], location: str) -> None:
    """Validate a behavior counter action.

    :param action: Counter action name.
    :param params: Counter action parameters.
    :param location: Human-readable action location.
    :returns: None.
    :raises ScenarioError: If the counter action is malformed.
    """

    if action == "set_counter":
        _reject_unsupported_fields(params, {"name", "value"}, location)
        if "value" not in params:
            raise ScenarioError(f"{location} action 'set_counter' missing required field(s): value")
        if not _is_integer(params["value"]):
            raise ScenarioError(f"{location} action 'set_counter' value must be an integer")
        return

    _reject_unsupported_fields(params, {"name", "by"}, location)
    if "by" not in params:
        params["by"] = 1
    if not _is_integer(params["by"]) or int(params["by"]) <= 0:
        raise ScenarioError(f"{location} action {action!r} by must be a positive integer")


def _validate_behavior_event_params(
    event: str,
    params: dict[str, Any],
    save_call_as: str | None,
    context: str,
) -> None:
    """Validate event-specific behavior rule parameters.

    :param event: Behavior event name.
    :param params: Event matching parameters.
    :param save_call_as: Optional call alias from the rule.
    :param context: Human-readable rule location.
    :returns: None.
    :raises ScenarioError: If event parameters are malformed.
    """

    if event == "call_received" and save_call_as is None:
        raise ScenarioError(f"{context} event 'call_received' requires save_call_as")
    if "reuse_alias" in params:
        if event != "call_received":
            raise ScenarioError(f"{context} event {event!r} reuse_alias is only supported for call_received")
        if not isinstance(params["reuse_alias"], bool):
            raise ScenarioError(f"{context} event {event!r} reuse_alias must be boolean")
    if event == "registered" and params:
        unsupported = ", ".join(sorted(params))
        raise ScenarioError(f"{context} event 'registered' does not support field(s): {unsupported}")
    if event == "scenario_trigger":
        params["name"] = _required_string(params.get("name"), f"{context} event 'scenario_trigger' name")
    if event == "call_state":
        params["state"] = _required_string(params.get("state"), f"{context} event 'call_state' state")
    if event == "timer_expired":
        params["timer"] = _required_string(params.get("timer"), f"{context} event 'timer_expired' timer")
    if event == "media_active" and "call" in params:
        params["call"] = _required_string(params.get("call"), f"{context} event 'media_active' call")
    if event == "counter_reached":
        _reject_unsupported_fields(params, {"counter", "value"}, f"{context} event 'counter_reached'")
        params["counter"] = _required_string(params.get("counter"), f"{context} event 'counter_reached' counter")
        if "value" not in params:
            raise ScenarioError(f"{context} event 'counter_reached' value is required")
        if not _is_integer(params["value"]):
            raise ScenarioError(f"{context} event 'counter_reached' value must be an integer")


def _required_string(value: Any, location: str) -> str:
    """Return a required non-empty string value.

    :param value: Raw value.
    :param location: Human-readable field location.
    :returns: Stripped string.
    :raises ScenarioError: If the value is missing or empty.
    """

    if value is None:
        raise ScenarioError(f"{location} is required")
    text = str(value).strip()
    if not text:
        raise ScenarioError(f"{location} must be non-empty")
    return text


def _reject_unsupported_fields(params: dict[str, Any], allowed: set[str], location: str) -> None:
    """Reject fields that are not supported in a mapping.

    :param params: Parsed parameter mapping.
    :param allowed: Supported field names.
    :param location: Human-readable mapping location.
    :returns: None.
    :raises ScenarioError: If unsupported fields are present.
    """

    unsupported = sorted(set(params) - allowed)
    if unsupported:
        raise ScenarioError(f"{location} unsupported field(s): {', '.join(unsupported)}")


def _is_integer(value: Any) -> bool:
    """Return whether a YAML value should be treated as an integer.

    :param value: Raw YAML value.
    :returns: ``True`` for integers except booleans.
    """

    return isinstance(value, int) and not isinstance(value, bool)


def _optional_non_empty_string(value: Any, location: str) -> str | None:
    """Return an optional non-empty string value.

    :param value: Raw value.
    :param location: Human-readable field location.
    :returns: Stripped string or ``None``.
    :raises ScenarioError: If the value is present but empty.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ScenarioError(f"{location} must be non-empty")
    return text


def _supported_behavior_actions(allow_register: bool = False) -> set[str]:
    """Return the action names supported inside endpoint behaviors.

    :param allow_register: Whether to include entry-only ``register``.
    :returns: Set of supported behavior action names.
    """

    supported = (set(ScenarioStep.REQUIRED_BY_ACTION) - DISALLOWED_BEHAVIOR_ACTIONS - {"register"}) | set(
        BEHAVIOR_ONLY_ACTIONS
    )
    if allow_register:
        supported.add("register")
    return supported
