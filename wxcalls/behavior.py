"""Endpoint behavior runtime for reactive scenario policies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wxcalls.backends.base import CallHandle
from wxcalls.exceptions import ConfigError, ScenarioError
from wxcalls.exceptions import TimeoutError as WxTimeoutError
from wxcalls.scenario import BehaviorAction, BehaviorDefinition, BehaviorRule, EndpointBehavior, Scenario, ScenarioStep

if TYPE_CHECKING:
    from wxcalls.orchestrator import CallLab

_POLL_INTERVAL_SECONDS = 0.01
_INCOMING_WAIT_SECONDS = 0.25


@dataclass(frozen=True)
class BehaviorEvent:
    """Runtime event delivered to an endpoint behavior.

    :param name: Event name.
    :param endpoint: Endpoint behavior instance name.
    :param call_ref: Optional fully qualified call reference.
    :param call: Optional call handle associated with the event.
    :param state: Optional call state associated with the event.
    :param timer: Optional timer name associated with the event.
    :param trigger_name: Optional scenario-emitted trigger name.
    :param counter: Optional counter name associated with the event.
    :param counter_value: Optional counter value associated with the event.
    """

    name: str
    endpoint: str
    call_ref: str | None = None
    call: CallHandle | None = None
    state: str | None = None
    timer: str | None = None
    trigger_name: str | None = None
    counter: str | None = None
    counter_value: int | None = None


@dataclass
class EndpointRuntime:
    """Mutable runtime state for one endpoint behavior instance.

    :param endpoint: Endpoint behavior assignment.
    :param behavior: Reusable behavior definition.
    :param queue: Event queue processed serially by the endpoint.
    :param state: Current behavior state.
    :param call_refs: Fully qualified call references owned by the endpoint.
    :param seen_states: Last emitted call state by fully qualified call reference.
    :param media_seen: Fully qualified call references whose media-active event was emitted.
    :param timers: Active timer tasks by timer name.
    :param counters: Integer behavior counters by counter name.
    :param state_changed: Condition notified whenever the behavior state changes.
    """

    endpoint: EndpointBehavior
    behavior: BehaviorDefinition
    queue: asyncio.Queue[BehaviorEvent]
    state: str
    call_refs: set[str] = field(default_factory=set)
    seen_states: dict[str, str] = field(default_factory=dict)
    media_seen: set[str] = field(default_factory=set)
    timers: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    state_changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class BehaviorRuntime:
    """Runs endpoint behavior policies alongside scripted scenario steps."""

    def __init__(self, lab: CallLab, scenario: Scenario) -> None:
        """Create a behavior runtime.

        :param lab: Active call lab.
        :param scenario: Parsed scenario with optional endpoint behaviors.
        :returns: None.
        """

        self.lab = lab
        self.scenario = scenario
        self.behaviors = scenario.behaviors or {}
        self.endpoints = scenario.endpoints or {}
        self._runtimes: dict[str, EndpointRuntime] = {}

    async def run_scenario(self, scenario_runner: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Run scenario steps while endpoint behaviors are active.

        :param scenario_runner: Coroutine factory that runs the scripted scenario steps.
        :returns: None.
        :raises ScenarioError: If behavior validation or execution fails.
        """

        self._validate_scenario()
        runtimes = [self._endpoint_runtime(endpoint) for endpoint in self.endpoints.values()]
        self._runtimes = {runtime.endpoint.name: runtime for runtime in runtimes}
        behavior_tasks = [
            asyncio.create_task(self._run_endpoint(runtime), name=f"wxcalls-behavior-{runtime.endpoint.name}")
            for runtime in runtimes
        ]
        scenario_task: asyncio.Task[None] = asyncio.create_task(scenario_runner(), name="wxcalls-scripted-scenario")
        try:
            await self._wait_for_completion(scenario_task, behavior_tasks)
        finally:
            await self._cancel_tasks([scenario_task, *behavior_tasks])
            self._runtimes = {}

    async def wait_endpoint_state(self, endpoint: str, state: str, timeout: float = 30.0) -> None:
        """Wait until an endpoint behavior reaches a state.

        :param endpoint: Endpoint behavior instance name.
        :param state: Desired behavior state.
        :param timeout: Maximum wait in seconds.
        :returns: None.
        :raises ScenarioError: If the endpoint or state is unknown.
        :raises WxTimeoutError: If the state is not reached before timeout.
        """

        runtime = self._runtimes.get(endpoint)
        if runtime is None:
            raise ScenarioError(f"Unknown behavior endpoint: {endpoint}")
        if state not in runtime.behavior.states:
            raise ScenarioError(f"Unknown behavior state {state!r} for endpoint {endpoint!r}")
        try:
            async with asyncio.timeout(timeout):
                async with runtime.state_changed:
                    await runtime.state_changed.wait_for(lambda: runtime.state == state)
        except TimeoutError as exc:
            raise WxTimeoutError(f"Timed out waiting for endpoint {endpoint} to reach behavior state {state}") from exc

    async def emit_trigger(self, endpoint: str, name: str) -> None:
        """Emit a scenario trigger into one endpoint behavior.

        :param endpoint: Endpoint behavior instance name.
        :param name: Trigger name.
        :returns: None.
        :raises ScenarioError: If the endpoint is unknown.
        """

        runtime = self._runtimes.get(endpoint)
        if runtime is None:
            raise ScenarioError(f"Unknown behavior endpoint: {endpoint}")
        await runtime.queue.put(
            BehaviorEvent(
                name="scenario_trigger",
                endpoint=runtime.endpoint.name,
                trigger_name=name,
            )
        )

    def _validate_scenario(self) -> None:
        """Validate runtime-only behavior constraints against the active lab config.

        :returns: None.
        :raises ScenarioError: If endpoint assignments conflict with the lab or scripted steps.
        """

        call_received_clients: set[str] = set()
        for endpoint in self.endpoints.values():
            try:
                self.lab.config.client(endpoint.client)
            except ConfigError as exc:
                raise ScenarioError(
                    f"Endpoint {endpoint.name!r} references unknown client {endpoint.client!r}"
                ) from exc
            if self._behavior_listens_for(endpoint.behavior, "call_received"):
                call_received_clients.add(endpoint.client)

        explicit_incoming_clients = self._expect_incoming_clients(self.scenario.steps)
        conflicts = sorted(call_received_clients & explicit_incoming_clients)
        if conflicts:
            clients = ", ".join(conflicts)
            raise ScenarioError(f"Endpoint behaviors and explicit expect_incoming steps both listen for: {clients}")
        for trigger_endpoint, trigger_name in self._trigger_steps(self.scenario.steps):
            if trigger_endpoint not in self.endpoints:
                raise ScenarioError(f"trigger_behavior references unknown endpoint {trigger_endpoint!r}")
            if not self._behavior_has_trigger(self.endpoints[trigger_endpoint].behavior, trigger_name):
                raise ScenarioError(
                    f"trigger_behavior references unknown trigger {trigger_name!r} for endpoint {trigger_endpoint!r}"
                )

    async def _wait_for_completion(
        self,
        scenario_task: asyncio.Task[None],
        behavior_tasks: list[asyncio.Task[None]],
    ) -> None:
        """Wait until the scripted scenario completes or a behavior fails.

        :param scenario_task: Task running scripted steps.
        :param behavior_tasks: Tasks running endpoint behaviors.
        :returns: None.
        :raises ScenarioError: If a behavior task fails or exits early.
        """

        tasks: set[asyncio.Task[None]] = {scenario_task, *behavior_tasks}
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not scenario_task:
                await task
        if scenario_task in done:
            await scenario_task
            return
        raise ScenarioError("Endpoint behavior task exited unexpectedly")

    async def _cancel_tasks(self, tasks: list[asyncio.Task[None]]) -> None:
        """Cancel unfinished tasks and wait for their cleanup.

        :param tasks: Tasks to cancel.
        :returns: None.
        """

        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _endpoint_runtime(self, endpoint: EndpointBehavior) -> EndpointRuntime:
        """Create mutable runtime state for an endpoint behavior assignment.

        :param endpoint: Endpoint behavior assignment.
        :returns: New endpoint runtime state.
        """

        behavior = self.behaviors[endpoint.behavior]
        return EndpointRuntime(
            endpoint=endpoint,
            behavior=behavior,
            queue=asyncio.Queue(),
            state=behavior.initial_state,
        )

    async def _run_endpoint(self, runtime: EndpointRuntime) -> None:
        """Run one endpoint behavior until the scenario ends.

        :param runtime: Endpoint runtime state.
        :returns: None.
        :raises ScenarioError: If behavior execution fails.
        """

        self.lab.artifacts.record_event(
            "behavior_started",
            endpoint=runtime.endpoint.name,
            client=runtime.endpoint.client,
            behavior=runtime.behavior.name,
            state=runtime.state,
        )
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._run_endpoint_controller(runtime), name=f"{runtime.endpoint.name}-events")
                if self._behavior_listens_for(runtime.endpoint.behavior, "call_received"):
                    task_group.create_task(self._watch_incoming(runtime), name=f"{runtime.endpoint.name}-incoming")
                if self._behavior_listens_for(runtime.endpoint.behavior, "call_state") or self._behavior_listens_for(
                    runtime.endpoint.behavior,
                    "media_active",
                ):
                    task_group.create_task(self._watch_calls(runtime), name=f"{runtime.endpoint.name}-calls")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _exception_summary(exc)
            self.lab.artifacts.record_event(
                "behavior_failed",
                endpoint=runtime.endpoint.name,
                client=runtime.endpoint.client,
                behavior=runtime.behavior.name,
                state=runtime.state,
                error=error,
                error_type=type(exc).__name__,
            )
            raise ScenarioError(
                f"Endpoint behavior {runtime.endpoint.name!r} failed in state {runtime.state!r}: {error}"
            ) from exc
        finally:
            await self._cancel_timers(runtime)

    async def _run_endpoint_controller(self, runtime: EndpointRuntime) -> None:
        """Process one endpoint's behavior events serially.

        :param runtime: Endpoint runtime state.
        :returns: None.
        :raises ScenarioError: If an event action fails.
        """

        await self._run_entry_actions(runtime)
        while True:
            event = await runtime.queue.get()
            await self._handle_event(runtime, event)

    async def _watch_incoming(self, runtime: EndpointRuntime) -> None:
        """Watch incoming calls for an endpoint behavior.

        :param runtime: Endpoint runtime state.
        :returns: None.
        """

        while runtime.endpoint.client not in self.lab.registrations:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        while True:
            try:
                call = await self.lab.backend.wait_for_incoming(
                    client_name=runtime.endpoint.client,
                    timeout=_INCOMING_WAIT_SECONDS,
                )
            except WxTimeoutError:
                continue
            await runtime.queue.put(
                BehaviorEvent(
                    name="call_received",
                    endpoint=runtime.endpoint.name,
                    call=call,
                )
            )

    async def _watch_calls(self, runtime: EndpointRuntime) -> None:
        """Poll behavior-owned calls for state and media events.

        :param runtime: Endpoint runtime state.
        :returns: None.
        """

        while True:
            for call_ref in tuple(runtime.call_refs):
                call = self.lab.calls.get(call_ref)
                if call is None:
                    continue
                await self._emit_call_state_if_changed(runtime, call_ref, call)
                await self._emit_media_active_if_needed(runtime, call_ref, call)
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _emit_call_state_if_changed(
        self,
        runtime: EndpointRuntime,
        call_ref: str,
        call: CallHandle,
    ) -> None:
        """Emit a call-state event if a behavior-owned call changed state.

        :param runtime: Endpoint runtime state.
        :param call_ref: Fully qualified call reference.
        :param call: Call handle to inspect.
        :returns: None.
        """

        previous_state = runtime.seen_states.get(call_ref)
        if previous_state == call.state:
            return
        runtime.seen_states[call_ref] = call.state
        await runtime.queue.put(
            BehaviorEvent(
                name="call_state",
                endpoint=runtime.endpoint.name,
                call_ref=call_ref,
                call=call,
                state=call.state,
            )
        )

    async def _emit_media_active_if_needed(
        self,
        runtime: EndpointRuntime,
        call_ref: str,
        call: CallHandle,
    ) -> None:
        """Emit one media-active event for a behavior-owned call.

        :param runtime: Endpoint runtime state.
        :param call_ref: Fully qualified call reference.
        :param call: Call handle to inspect.
        :returns: None.
        """

        if not call.media_active or call_ref in runtime.media_seen:
            return
        runtime.media_seen.add(call_ref)
        await runtime.queue.put(
            BehaviorEvent(
                name="media_active",
                endpoint=runtime.endpoint.name,
                call_ref=call_ref,
                call=call,
            )
        )

    async def _handle_event(self, runtime: EndpointRuntime, event: BehaviorEvent) -> None:
        """Apply the matching rule for a behavior event.

        :param runtime: Endpoint runtime state.
        :param event: Runtime event to handle.
        :returns: None.
        :raises ScenarioError: If a matching rule action fails.
        """

        self._record_behavior_event(runtime, event)
        rule = self._matching_rule(runtime, event)
        if rule is None:
            return
        self._progress_rule(runtime, event, rule)
        if event.name == "call_received" and event.call is not None and rule.save_call_as is not None:
            self._save_call(
                runtime,
                rule.save_call_as,
                event.call,
                reuse_alias=bool(rule.params.get("reuse_alias", False)),
            )
        if event.name == "timer_expired" and event.timer is not None:
            runtime.timers.pop(event.timer, None)
        counter_preempted = False
        for action in rule.actions:
            if await self._run_action(runtime, action):
                counter_preempted = True
                break
        if rule.next_state is not None and not counter_preempted:
            await self._transition_to(runtime, rule.next_state, trigger=event.name)

    def _record_behavior_event(self, runtime: EndpointRuntime, event: BehaviorEvent) -> None:
        """Record a behavior event artifact.

        :param runtime: Endpoint runtime state.
        :param event: Runtime event to record.
        :returns: None.
        """

        self.lab.artifacts.record_event(
            "behavior_event",
            endpoint=runtime.endpoint.name,
            behavior=runtime.behavior.name,
            state=runtime.state,
            trigger=event.name,
            call=event.call.id if event.call else None,
            call_ref=event.call_ref,
            call_state=event.state,
            timer=event.timer,
            trigger_name=event.trigger_name,
            counter=event.counter,
            counter_value=event.counter_value,
        )

    async def _run_entry_actions(self, runtime: EndpointRuntime) -> None:
        """Run entry actions for the endpoint's current behavior state.

        :param runtime: Endpoint runtime state.
        :returns: None.
        :raises ScenarioError: If an entry action fails.
        """

        state = runtime.behavior.states[runtime.state]
        if state.entry_actions:
            actions = ", ".join(action.action for action in state.entry_actions)
            self._progress_behavior(
                runtime,
                event="entry",
                next_state=runtime.state,
                action=actions,
            )
        for action in state.entry_actions:
            if await self._run_action(runtime, action):
                break

    async def _transition_to(self, runtime: EndpointRuntime, next_state: str, trigger: str) -> None:
        """Move an endpoint behavior to another state and run entry actions.

        :param runtime: Endpoint runtime state.
        :param next_state: Destination behavior state.
        :param trigger: Event name that caused the transition.
        :returns: None.
        """

        previous_state = runtime.state
        runtime.state = next_state
        self.lab.artifacts.record_event(
            "behavior_transition",
            endpoint=runtime.endpoint.name,
            behavior=runtime.behavior.name,
            from_state=previous_state,
            to_state=runtime.state,
            trigger=trigger,
        )
        async with runtime.state_changed:
            runtime.state_changed.notify_all()
        await self._run_entry_actions(runtime)

    def _progress_rule(self, runtime: EndpointRuntime, event: BehaviorEvent, rule: BehaviorRule) -> None:
        """Report one behavior rule match to the human progress stream.

        :param runtime: Endpoint runtime state.
        :param event: Runtime event matched by the rule.
        :param rule: Matched behavior rule.
        :returns: None.
        """

        actions = ", ".join(action.action for action in rule.actions) or "-"
        self._progress_behavior(
            runtime,
            event=self._event_label(event),
            next_state=rule.next_state or runtime.state,
            action=actions,
        )

    def _progress_behavior(self, runtime: EndpointRuntime, event: str, next_state: str, action: str) -> None:
        """Report behavior state-machine progress using a stable compact format.

        :param runtime: Endpoint runtime state.
        :param event: Event label responsible for this behavior activity.
        :param next_state: State the behavior will remain in or transition to.
        :param action: Action name or comma-separated action names.
        :returns: None.
        """

        self.lab._progress(
            f"behavior {runtime.endpoint.name}/{runtime.behavior.name} "
            f"state={runtime.state} event={event} next_state={next_state} action={action}"
        )

    def _event_label(self, event: BehaviorEvent) -> str:
        """Return a compact event label for progress output.

        :param event: Runtime event.
        :returns: Event name with its most useful discriminator appended.
        """

        if event.name == "scenario_trigger" and event.trigger_name:
            return f"{event.name}:{event.trigger_name}"
        if event.name == "call_state" and event.state:
            return f"{event.name}:{event.state}"
        if event.name == "timer_expired" and event.timer:
            return f"{event.name}:{event.timer}"
        if event.name == "counter_reached" and event.counter and event.counter_value is not None:
            return f"{event.name}:{event.counter}={event.counter_value}"
        if event.name == "media_active" and event.call_ref:
            return f"{event.name}:{event.call_ref}"
        return event.name

    def _matching_rule(self, runtime: EndpointRuntime, event: BehaviorEvent) -> BehaviorRule | None:
        """Return the first active behavior rule matching an event.

        :param runtime: Endpoint runtime state.
        :param event: Runtime event to match.
        :returns: Matching rule, if any.
        """

        state = runtime.behavior.states[runtime.state]
        for rule in state.rules:
            if rule.event != event.name:
                continue
            if self._rule_matches_event(runtime, rule, event):
                return rule
        return None

    def _rule_matches_event(self, runtime: EndpointRuntime, rule: BehaviorRule, event: BehaviorEvent) -> bool:
        """Return whether a rule's event parameters match an event.

        :param runtime: Endpoint runtime state.
        :param rule: Candidate behavior rule.
        :param event: Runtime event.
        :returns: ``True`` when the rule matches.
        """

        if rule.event == "call_received" and "from_uri" in rule.params:
            return event.call is not None and event.call.remote_uri == str(rule.params["from_uri"])
        if rule.event == "registered":
            return True
        if rule.event == "scenario_trigger":
            return event.trigger_name == str(rule.params["name"])
        if rule.event == "call_state":
            return event.state == str(rule.params["state"]) and self._call_param_matches(runtime, rule, event)
        if rule.event == "media_active":
            return self._call_param_matches(runtime, rule, event)
        if rule.event == "timer_expired":
            return event.timer == str(rule.params["timer"])
        if rule.event == "counter_reached":
            return event.counter == str(rule.params["counter"]) and event.counter_value == int(rule.params["value"])
        return True

    def _call_param_matches(self, runtime: EndpointRuntime, rule: BehaviorRule, event: BehaviorEvent) -> bool:
        """Return whether an optional rule call parameter matches an event call reference.

        :param runtime: Endpoint runtime state.
        :param rule: Candidate behavior rule.
        :param event: Runtime event with an optional call reference.
        :returns: ``True`` when no call filter exists or the filter matches.
        """

        if "call" not in rule.params:
            return True
        return self._scope_call_ref(runtime, str(rule.params["call"])) == event.call_ref

    async def _run_action(self, runtime: EndpointRuntime, action: BehaviorAction) -> bool:
        """Run one behavior action.

        :param runtime: Endpoint runtime state.
        :param action: Behavior action to execute.
        :returns: ``True`` if a counter event preempted the current rule's transition.
        :raises ScenarioError: If the action fails.
        """

        counter_preempted = False
        self.lab.artifacts.record_event(
            "behavior_action_started",
            endpoint=runtime.endpoint.name,
            behavior=runtime.behavior.name,
            state=runtime.state,
            action=action.action,
            index=action.index,
        )
        try:
            if action.action == "start_timer":
                self._start_timer(runtime, str(action.params["name"]), float(action.params["seconds"]))
            elif action.action == "cancel_timer":
                self._cancel_timer(runtime, str(action.params["name"]))
            elif action.action in {"set_counter", "increment_counter", "decrement_counter"}:
                counter_preempted = await self._run_counter_action(runtime, action)
            else:
                params = self._scope_action_params(runtime, action)
                self._ensure_call_alias_available(runtime, action.action, params)
                await self.lab.run_behavior_action(action.action, params)
                self._remember_action_outputs(runtime, action.action, params)
                if action.action == "register":
                    await runtime.queue.put(BehaviorEvent(name="registered", endpoint=runtime.endpoint.name))
        except Exception as exc:
            raise ScenarioError(
                f"endpoint {runtime.endpoint.name!r} action {action.index} ({action.action}) failed: {exc}"
            ) from exc
        finally:
            self.lab.artifacts.record_event(
                "behavior_action_finished",
                endpoint=runtime.endpoint.name,
                behavior=runtime.behavior.name,
                state=runtime.state,
                action=action.action,
                index=action.index,
            )
        return counter_preempted

    async def _run_counter_action(self, runtime: EndpointRuntime, action: BehaviorAction) -> bool:
        """Run one behavior counter action.

        :param runtime: Endpoint runtime state.
        :param action: Counter action to execute.
        :returns: ``True`` if a matching ``counter_reached`` rule ran.
        :raises ScenarioError: If a counter mutation is invalid.
        """

        name = str(action.params["name"])
        previous_value = runtime.counters.get(name)
        if action.action == "set_counter":
            new_value = int(action.params["value"])
        else:
            if previous_value is None:
                raise ScenarioError(f"Counter {name!r} has not been set")
            delta = int(action.params.get("by", 1))
            if action.action == "decrement_counter":
                delta = -delta
            new_value = previous_value + delta
        runtime.counters[name] = new_value
        self.lab.artifacts.record_event(
            "behavior_counter_updated",
            endpoint=runtime.endpoint.name,
            behavior=runtime.behavior.name,
            state=runtime.state,
            action=action.action,
            counter=name,
            previous_value=previous_value,
            value=new_value,
        )
        return await self._handle_counter_reached(runtime, name, new_value)

    async def _handle_counter_reached(self, runtime: EndpointRuntime, counter: str, value: int) -> bool:
        """Run a matching state-local ``counter_reached`` rule, if present.

        :param runtime: Endpoint runtime state.
        :param counter: Counter name that was updated.
        :param value: New counter value.
        :returns: ``True`` if a matching rule ran and should preempt the outer transition.
        :raises ScenarioError: If a matching rule action fails.
        """

        event = BehaviorEvent(
            name="counter_reached",
            endpoint=runtime.endpoint.name,
            counter=counter,
            counter_value=value,
        )
        rule = self._matching_rule(runtime, event)
        if rule is None:
            return False
        self._record_behavior_event(runtime, event)
        self._progress_rule(runtime, event, rule)
        nested_preempted = False
        for action in rule.actions:
            if await self._run_action(runtime, action):
                nested_preempted = True
                break
        if rule.next_state is not None and not nested_preempted:
            await self._transition_to(runtime, rule.next_state, trigger=event.name)
        return True

    def _scope_action_params(self, runtime: EndpointRuntime, action: BehaviorAction) -> dict[str, Any]:
        """Rewrite behavior-local call aliases into fully qualified references.

        :param runtime: Endpoint runtime state.
        :param action: Behavior action.
        :returns: Scoped action parameters.
        """

        params = dict(action.params)
        for key in ("call", "primary_call", "consult_call", "playback_call", "recording_call"):
            if key in params:
                params[key] = self._scope_call_ref(runtime, str(params[key]))
        if "calls" in params:
            params["calls"] = [self._scope_call_ref(runtime, str(call_ref)) for call_ref in _as_list(params["calls"])]
        if action.action == "register" and "client" not in params and "clients" not in params:
            params["client"] = runtime.endpoint.client
        if action.action in {"call", "consult_call"} and "client" not in params:
            params["client"] = runtime.endpoint.client
        if action.action in {"call", "consult_call"}:
            default_ref = "consult_call" if action.action == "consult_call" else "call"
            params["save_as"] = self._scope_output_ref(runtime, str(params.get("save_as", default_ref)))
        if action.action in {"record", "record_during_playback"}:
            params["save_as"] = self._scope_output_ref(runtime, str(params.get("save_as", "recording")))
        return params

    def _ensure_call_alias_available(self, runtime: EndpointRuntime, action: str, params: dict[str, Any]) -> None:
        """Reject behavior call actions that would overwrite a scoped call alias.

        :param runtime: Endpoint runtime state.
        :param action: Action name.
        :param params: Scoped action parameters.
        :returns: None.
        :raises ScenarioError: If the behavior-scoped call alias already exists.
        """

        if action not in {"call", "consult_call"}:
            return
        call_ref = str(params["save_as"])
        existing_call = self.lab.calls.get(call_ref)
        if existing_call is None:
            return
        if bool(params.get("reuse_alias", False)):
            self._prepare_call_alias_reuse(runtime, call_ref, existing_call)
            return
        raise ScenarioError(f"Behavior endpoint {runtime.endpoint.name!r} call alias already exists: {call_ref}")

    def _remember_action_outputs(self, runtime: EndpointRuntime, action: str, params: dict[str, Any]) -> None:
        """Track behavior-owned calls created by behavior actions.

        :param runtime: Endpoint runtime state.
        :param action: Action name.
        :param params: Scoped action parameters.
        :returns: None.
        """

        if action not in {"call", "consult_call"}:
            return
        call_ref = str(params["save_as"])
        if call_ref in self.lab.calls:
            runtime.call_refs.add(call_ref)

    def _save_call(
        self,
        runtime: EndpointRuntime,
        alias: str,
        call: CallHandle,
        reuse_alias: bool = False,
    ) -> str:
        """Save an incoming call under a behavior-scoped alias.

        :param runtime: Endpoint runtime state.
        :param alias: Behavior-local call alias.
        :param call: Incoming call handle.
        :param reuse_alias: Whether a disconnected existing call alias may be replaced.
        :returns: Fully qualified call reference.
        :raises ScenarioError: If the alias already exists.
        """

        call_ref = self._scope_output_ref(runtime, alias)
        existing_call = self.lab.calls.get(call_ref)
        if existing_call is not None:
            if not reuse_alias:
                raise ScenarioError(
                    f"Behavior endpoint {runtime.endpoint.name!r} call alias already exists: {call_ref}"
                )
            self._prepare_call_alias_reuse(runtime, call_ref, existing_call)
        self.lab.calls[call_ref] = call
        runtime.call_refs.add(call_ref)
        runtime.seen_states[call_ref] = call.state
        self.lab.artifacts.record_event(
            "incoming_call_seen",
            call=call.id,
            client=call.client_name,
            behavior_endpoint=runtime.endpoint.name,
            save_as=call_ref,
        )
        self.lab._progress(f"call received: {call.client_name} <- {call.remote_uri} ({call_ref})")
        return call_ref

    def _prepare_call_alias_reuse(
        self,
        runtime: EndpointRuntime,
        call_ref: str,
        existing_call: CallHandle,
    ) -> None:
        """Prepare a behavior-owned call alias to point at a new call leg.

        :param runtime: Endpoint runtime state.
        :param call_ref: Fully qualified behavior-owned call alias.
        :param existing_call: Existing call handle currently stored under the alias.
        :returns: None.
        :raises ScenarioError: If the existing alias still points at an active call.
        """

        if existing_call.state != "disconnected":
            raise ScenarioError(
                f"Behavior endpoint {runtime.endpoint.name!r} cannot reuse active call alias "
                f"{call_ref}: existing call is {existing_call.state}"
            )
        # Treat the replacement leg as a fresh call for polling purposes. Without
        # clearing these sets, a loop could miss the next connected/media events.
        runtime.seen_states.pop(call_ref, None)
        runtime.media_seen.discard(call_ref)
        self.lab.artifacts.record_event(
            "behavior_call_alias_reused",
            endpoint=runtime.endpoint.name,
            behavior=runtime.behavior.name,
            state=runtime.state,
            call_alias=call_ref,
            previous_call=existing_call.id,
        )

    def _start_timer(self, runtime: EndpointRuntime, name: str, seconds: float) -> None:
        """Start or restart a behavior-scoped timer.

        :param runtime: Endpoint runtime state.
        :param name: Timer name.
        :param seconds: Timer duration.
        :returns: None.
        """

        self._cancel_timer(runtime, name)
        task = asyncio.create_task(self._run_timer(runtime, name, seconds), name=f"{runtime.endpoint.name}-{name}")
        runtime.timers[name] = task

    def _cancel_timer(self, runtime: EndpointRuntime, name: str) -> None:
        """Cancel a behavior-scoped timer if it is active.

        :param runtime: Endpoint runtime state.
        :param name: Timer name.
        :returns: None.
        """

        task = runtime.timers.pop(name, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_timer(self, runtime: EndpointRuntime, name: str, seconds: float) -> None:
        """Sleep for a timer duration and emit a timer event.

        :param runtime: Endpoint runtime state.
        :param name: Timer name.
        :param seconds: Timer duration.
        :returns: None.
        """

        await asyncio.sleep(seconds)
        await runtime.queue.put(
            BehaviorEvent(
                name="timer_expired",
                endpoint=runtime.endpoint.name,
                timer=name,
            )
        )

    async def _cancel_timers(self, runtime: EndpointRuntime) -> None:
        """Cancel all timers owned by an endpoint behavior.

        :param runtime: Endpoint runtime state.
        :returns: None.
        """

        tasks = list(runtime.timers.values())
        runtime.timers.clear()
        await self._cancel_tasks(tasks)

    def _scope_call_ref(self, runtime: EndpointRuntime, value: str) -> str:
        """Resolve a behavior-local call reference into a scenario call reference.

        :param runtime: Endpoint runtime state.
        :param value: Behavior-local or global call reference.
        :returns: Fully qualified call reference when a local call exists.
        """

        if "." in value:
            return value
        scoped = self._scope_output_ref(runtime, value)
        if scoped in self.lab.calls:
            return scoped
        return value

    def _scope_output_ref(self, runtime: EndpointRuntime, value: str) -> str:
        """Return a behavior-scoped output reference unless already qualified.

        :param runtime: Endpoint runtime state.
        :param value: Local or qualified alias.
        :returns: Qualified alias.
        """

        if "." in value:
            return value
        return f"{runtime.endpoint.name}.{value}"

    def _behavior_listens_for(self, behavior_name: str, event_name: str) -> bool:
        """Return whether a behavior defines a rule for an event.

        :param behavior_name: Behavior definition name.
        :param event_name: Event name.
        :returns: ``True`` when at least one state listens for the event.
        """

        behavior = self.behaviors[behavior_name]
        return any(rule.event == event_name for state in behavior.states.values() for rule in state.rules)

    def _behavior_has_trigger(self, behavior_name: str, trigger_name: str) -> bool:
        """Return whether a behavior defines a named scenario trigger rule.

        :param behavior_name: Behavior definition name.
        :param trigger_name: Scenario trigger name.
        :returns: ``True`` when the behavior has a matching trigger rule.
        """

        behavior = self.behaviors[behavior_name]
        return any(
            rule.event == "scenario_trigger" and rule.params.get("name") == trigger_name
            for state in behavior.states.values()
            for rule in state.rules
        )

    def _expect_incoming_clients(self, steps: tuple[ScenarioStep, ...]) -> set[str]:
        """Return clients consumed by explicit ``expect_incoming`` scenario steps.

        :param steps: Scenario steps to inspect recursively.
        :returns: Set of logical client names.
        """

        clients: set[str] = set()
        for step in steps:
            if step.action == "expect_incoming":
                clients.add(str(step.params["client"]))
            if step.action == "parallel":
                branches = step.params["branches"]
                for branch_steps in branches.values():
                    clients.update(self._expect_incoming_clients(branch_steps))
        return clients

    def _trigger_steps(self, steps: tuple[ScenarioStep, ...]) -> list[tuple[str, str]]:
        """Return behavior triggers emitted by explicit scenario steps.

        :param steps: Scenario steps to inspect recursively.
        :returns: ``(endpoint, trigger_name)`` tuples from ``trigger_behavior`` steps.
        """

        triggers: list[tuple[str, str]] = []
        for step in steps:
            if step.action == "trigger_behavior":
                triggers.append((str(step.params["endpoint"]), str(step.params["name"])))
            if step.action == "parallel":
                branches = step.params["branches"]
                for branch_steps in branches.values():
                    triggers.extend(self._trigger_steps(branch_steps))
        return triggers


def _as_list(value: Any) -> list[Any]:
    """Normalize a scalar, tuple, list, or missing value into a list.

    :param value: Source value.
    :returns: List representation.
    """

    if isinstance(value, list | tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def _exception_summary(exc: BaseException) -> str:
    """Return a concise message for nested behavior task exceptions.

    :param exc: Exception or exception group to summarize.
    :returns: Human-readable exception message.
    """

    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_exception_summary(nested) for nested in exc.exceptions)
    return str(exc)
