"""Async scenario orchestration over a SIP backend."""

from __future__ import annotations

import asyncio
import re
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from wxcalls.artifacts import ArtifactWriter
from wxcalls.backends.base import CallHandle, RegistrationResult, SipBackend
from wxcalls.backends.fake import FakeSipBackend
from wxcalls.backends.pjsua2 import Pjsua2SipBackend
from wxcalls.behavior import BehaviorRuntime
from wxcalls.config import LabConfig, load_config
from wxcalls.exceptions import BackendError, ConfigError, ScenarioError
from wxcalls.exceptions import TimeoutError as WxTimeoutError
from wxcalls.media import MediaAsset, MediaFactory, detect_marker
from wxcalls.scenario import Scenario, ScenarioStep, load_scenario

_RECORD_DURING_PLAYBACK_GRACE_SECONDS = 10.0


class CallLab:
    """Coordinates clients, call handles, media, and scenario execution."""

    def __init__(
        self,
        config: LabConfig,
        backend: SipBackend | None = None,
        artifact_writer: ArtifactWriter | None = None,
        media_factory: MediaFactory | None = None,
        progress_reporter: Callable[[str], None] | None = None,
    ) -> None:
        """Create a call lab.

        :param config: Lab configuration.
        :param backend: SIP backend. Defaults to the fake backend.
        :param artifact_writer: Artifact writer for run outputs.
        :param media_factory: Media generator.
        :param progress_reporter: Optional callback for human-readable progress lines.
        :returns: None.
        """

        self.config = config
        self.backend = backend or FakeSipBackend()
        self.artifacts = artifact_writer or ArtifactWriter(config.artifacts_dir)
        self.media_factory = media_factory or MediaFactory(self.artifacts.path_for("media", "generated", ""))
        self.progress_reporter = progress_reporter
        # Scenario steps refer to calls and recordings by logical names; these maps
        # hold the backend handles and artifact paths behind those names.
        self.calls: dict[str, CallHandle] = {}
        self.registrations: dict[str, RegistrationResult] = {}
        self.recordings: dict[str, Path] = {}
        self._behavior_runtime: BehaviorRuntime | None = None
        self._reported_established_call_ids: set[str] = set()
        self._reported_ended_call_ids: set[str] = set()

    @classmethod
    def from_config_path(
        cls,
        path: str | Path,
        env_file: str | Path | None = ".env",
        backend_name: str = "fake",
        progress_reporter: Callable[[str], None] | None = None,
    ) -> CallLab:
        """Create a lab from a YAML config file.

        :param path: Configuration path.
        :param env_file: Optional local env file.
        :param backend_name: ``fake`` or ``pjsua2``.
        :param progress_reporter: Optional callback for human-readable progress lines.
        :returns: New call lab.
        """

        return cls(
            config=load_config(path, env_file=env_file),
            backend=make_backend(backend_name),
            progress_reporter=progress_reporter,
        )

    async def __aenter__(self) -> CallLab:
        """Initialize the backend and record startup artifacts.

        :returns: Initialized call lab.
        """

        pjsip_log = self.artifacts.path_for("logs", "pjsip", ".log")
        await self.backend.initialize(self.config, pjsip_log_path=pjsip_log)
        self.artifacts.record_event("backend_initialized", backend=type(self.backend).__name__)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Shut down backend resources and finalize artifacts.

        :param exc_type: Exception type from the managed block, if any.
        :param exc: Exception instance from the managed block, if any.
        :param tb: Traceback from the managed block, if any.
        :returns: None.
        """

        await self.backend.shutdown()
        self.artifacts.record_event("backend_shutdown")
        self.artifacts.finalize()

    def client(self, name: str) -> Any:
        """Return a configured client by name.

        :param name: Logical client name.
        :returns: Client configuration.
        """

        return self.config.client(name)

    def target(self, name: str) -> Any:
        """Return a configured target by name.

        :param name: Logical target name.
        :returns: Target configuration.
        """

        return self.config.target(name)

    def resolve_call_target(self, client_name: str, target: str, use_target_extension: bool = False) -> str:
        """Resolve a scenario call target into a backend dial string.

        :param client_name: Logical client placing the call.
        :param target: Client name, target name, literal URI, or numeric extension.
        :param use_target_extension: Whether ``target`` names a client whose extension should be dialed.
        :returns: Dialable SIP target for the backend. E.164 targets from an LGW
            are expanded with that gateway's registrar domain and TLS port.
        :raises ScenarioError: If extension dialing cannot resolve a configured client extension.
        """

        if use_target_extension:
            try:
                target_client = self.config.client(target)
            except ConfigError as exc:
                raise ScenarioError(
                    f"use_target_extension requires target to be a configured SIP client: {target!r}"
                ) from exc
            if target_client.extension is None:
                raise ScenarioError(f"SIP client {target!r} has no configured extension")
            return self._extension_target_uri(client_name, target_client.extension)

        resolved = self.config.resolve_target_uri(target)
        caller = self.config.client(client_name)
        if resolved == target and caller.is_local_gateway and _is_e164_number(target):
            return f"sip:{target}@{caller.registrar_domain}:5061"
        if resolved != target or ":" in resolved or "@" in resolved or not resolved.isdecimal():
            return resolved

        return self._extension_target_uri(client_name, resolved)

    def _extension_target_uri(self, client_name: str, extension: str) -> str:
        """Resolve a numeric extension against the calling client's registrar.

        :param client_name: Logical client placing the call.
        :param extension: Numeric extension to dial.
        :returns: Dialable SIP URI, or the original extension if no host can be derived.
        """

        registrar_host = _sip_host(self.config.client(client_name).registrar_uri)
        if registrar_host is None:
            return extension
        return f"sip:{extension}@{registrar_host}"

    async def run_scenario_path(self, path: str | Path) -> None:
        """Load and run a scenario file.

        :param path: Scenario YAML file.
        :returns: None.
        """

        await self.run_scenario(load_scenario(path))

    async def run_scenario(self, scenario: Scenario) -> None:
        """Run a parsed scenario.

        :param scenario: Scenario to execute.
        :returns: None.
        """

        if scenario.endpoints:
            runtime = BehaviorRuntime(self, scenario)
            previous_runtime = self._behavior_runtime
            self._behavior_runtime = runtime
            try:
                await runtime.run_scenario(lambda: self._run_scenario_steps(scenario))
            finally:
                self._behavior_runtime = previous_runtime
            return
        await self._run_scenario_steps(scenario)

    async def _run_scenario_steps(self, scenario: Scenario) -> None:
        """Run the explicit step sequence for a parsed scenario.

        :param scenario: Scenario to execute.
        :returns: None.
        """

        self.artifacts.record_event("scenario_started", name=scenario.name, source=str(scenario.source or ""))
        step_count = len(scenario.steps)
        for step in scenario.steps:
            self.artifacts.record_event("step_started", index=step.index, action=step.action)
            self._progress(f"step {step.index}/{step_count}: {step.action}")
            await self._run_step(step)
            self.artifacts.record_event("step_finished", index=step.index, action=step.action)
        self.artifacts.record_event("scenario_finished", name=scenario.name)

    async def run_behavior_action(self, action: str, params: dict[str, Any]) -> None:
        """Run an action on behalf of an endpoint behavior.

        :param action: Reused scenario action name.
        :param params: Already scoped action parameters.
        :returns: None.
        """

        await self._run_action(action, params)

    async def _run_step(self, step: ScenarioStep) -> None:
        """Dispatch one scenario step to its action handler.

        :param step: Validated scenario step.
        :returns: None.
        :raises ScenarioError: If no handler exists for the action.
        """

        await self._run_action(step.action, step.params)

    async def _run_action(self, action: str, params: dict[str, Any]) -> None:
        """Dispatch an action to its handler.

        :param action: Scenario or behavior action name.
        :param params: Action parameters.
        :returns: None.
        :raises ScenarioError: If no handler exists for the action.
        """

        handler = getattr(self, f"_step_{action}", None)
        if handler is None:
            raise ScenarioError(f"Unsupported action: {action}")
        await handler(params)

    async def _step_parallel(self, params: dict[str, Any]) -> None:
        """Run named scenario branches concurrently.

        :param params: Step parameters containing validated ``branches``.
        :returns: None.
        :raises ScenarioError: If any branch fails.
        """

        branches = cast(dict[str, tuple[ScenarioStep, ...]], params["branches"])
        branch_names = list(branches)
        failures: list[str] = []
        failed = False
        self.artifacts.record_event("parallel_started", branches=branch_names)
        try:
            async with asyncio.TaskGroup() as task_group:
                for branch_name, steps in branches.items():
                    task_group.create_task(
                        self._run_parallel_branch(branch_name, steps, failures),
                        name=f"wxcalls-branch-{branch_name}",
                    )
        except* Exception as exc_group:
            failed = True
            summary = "; ".join(failures) or _exception_summary(exc_group)
            raise ScenarioError(f"Parallel step failed: {summary}") from exc_group
        finally:
            self.artifacts.record_event("parallel_finished", branches=branch_names, failed=failed)

    async def _run_parallel_branch(
        self,
        branch_name: str,
        steps: tuple[ScenarioStep, ...],
        failures: list[str],
    ) -> None:
        """Run one branch inside a ``parallel`` step.

        :param branch_name: Scenario branch name.
        :param steps: Validated branch steps.
        :param failures: Shared list used to summarize branch failures.
        :returns: None.
        :raises ScenarioError: If any branch step fails.
        """

        self.artifacts.record_event("branch_started", branch=branch_name, steps=len(steps))
        try:
            for step in steps:
                self.artifacts.record_event(
                    "step_started",
                    branch=branch_name,
                    index=step.index,
                    action=step.action,
                )
                self._progress(f"branch {branch_name} step {step.index}/{len(steps)}: {step.action}")
                await self._run_step(step)
                self.artifacts.record_event(
                    "step_finished",
                    branch=branch_name,
                    index=step.index,
                    action=step.action,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"branch {branch_name!r} failed at step {step.index} ({step.action}): {exc}"
            failures.append(message)
            self.artifacts.record_event(
                "branch_failed",
                branch=branch_name,
                index=step.index,
                action=step.action,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ScenarioError(message) from exc
        self.artifacts.record_event("branch_finished", branch=branch_name)

    async def _step_register(self, params: dict[str, Any]) -> None:
        """Run a ``register`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        clients = _as_list(params.get("clients", params.get("client")))
        timeout = float(params.get("timeout", 30.0))
        registered: list[RegistrationResult] = []
        for client_name in clients:
            client = self.config.client(str(client_name))
            result = await self.backend.register_client(client, client.credentials(self.config.env), timeout=timeout)
            self.registrations[client.name] = result
            registered.append(result)
            self.artifacts.record_event("client_registered", client=client.name, expires=result.expires)

        # Registration soak steps run concurrently so multi-client scenarios keep
        # both endpoints alive over the same wall-clock interval.
        if _should_stay_registered(params):
            await asyncio.gather(*(self._stay_registered(result, params) for result in registered))

    async def _stay_registered(self, result: RegistrationResult, params: dict[str, Any]) -> None:
        """Keep a registered client alive according to scenario parameters.

        :param result: Initial registration result.
        :param params: Register step parameters.
        :returns: None.
        :raises ScenarioError: If no duration can be derived.
        """

        explicit_seconds = params.get("stay_registered_for")
        if explicit_seconds is None:
            if result.expires is None:
                raise ScenarioError(
                    f"Cannot derive stay_registered duration for {result.client_name}; "
                    "registration response did not include an expiration interval"
                )
            seconds = float(result.expires * 2)
            require_reregistration = bool(params.get("require_reregistration", True))
        else:
            seconds = float(explicit_seconds)
            require_reregistration = bool(params.get("require_reregistration", False))

        min_reregistrations = int(params.get("min_reregistrations", 1))
        self.artifacts.record_event(
            "registration_wait_started",
            client=result.client_name,
            seconds=seconds,
            require_reregistration=require_reregistration,
            min_reregistrations=min_reregistrations,
        )
        wait_result = await self.backend.stay_registered(
            result.client_name,
            seconds=seconds,
            require_reregistration=require_reregistration,
            min_reregistrations=min_reregistrations,
        )
        self.artifacts.record_event(
            "registration_wait_finished",
            client=wait_result.client_name,
            seconds=wait_result.seconds,
            refreshes_observed=wait_result.refreshes_observed,
            last_expires=wait_result.last_expires,
        )

    async def _step_call(self, params: dict[str, Any]) -> None:
        """Run a ``call`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        timeout = float(params.get("timeout", 30.0))
        target_uri = self.resolve_call_target(
            str(params["client"]),
            str(params["target"]),
            use_target_extension=bool(params.get("use_target_extension", False)),
        )
        save_as = str(params.get("save_as", "call"))
        self._progress(f"call initiated: {params['client']} -> {target_uri} ({save_as})")
        caller_id = params.get("caller_id")
        pai_caller_id = params.get("pai_caller_id")
        client = self.config.client(str(params["client"]))
        if (caller_id is not None or pai_caller_id is not None) and not client.is_local_gateway:
            raise ScenarioError("caller_id and pai_caller_id are supported only for Local Gateway calls")
        call_options: dict[str, str] = {}
        if caller_id is not None:
            call_options["caller_id"] = str(caller_id)
        if pai_caller_id is not None:
            call_options["pai_caller_id"] = str(pai_caller_id)
        call = await self.backend.place_call(
            client_name=str(params["client"]),
            target_uri=target_uri,
            timeout=timeout,
            video=bool(params.get("video", False)),
            **call_options,
        )
        self.calls[save_as] = call
        self.artifacts.record_event("call_placed", call=call.id, client=call.client_name, target=target_uri)
        if call.state == "connected":
            self._report_call_established(save_as, call)

    async def _step_expect_incoming(self, params: dict[str, Any]) -> None:
        """Run an ``expect_incoming`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        timeout = float(params.get("timeout", 30.0))
        from_uri = params.get("from_uri")
        save_as = str(params.get("save_as", f"{params['client']}_incoming"))
        call = await self.backend.wait_for_incoming(
            client_name=str(params["client"]),
            timeout=timeout,
            from_uri=str(from_uri) if from_uri else None,
        )
        self.calls[save_as] = call
        self.artifacts.record_event("incoming_call_seen", call=call.id, client=call.client_name)
        self._progress(f"call received: {call.client_name} <- {call.remote_uri} ({save_as})")

    async def _step_answer(self, params: dict[str, Any]) -> None:
        """Run an ``answer`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call_ref = str(params["call"])
        call = self._call(call_ref)
        await self.backend.answer(call, status_code=int(params.get("status_code", 200)))
        self.artifacts.record_event("call_answered", call=call.id)
        if call.state == "connected":
            self._report_call_established(call_ref, call)

    async def _step_reject(self, params: dict[str, Any]) -> None:
        """Run a ``reject`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call_ref = str(params["call"])
        call = self._call(call_ref)
        await self.backend.reject(call, status_code=int(params.get("status_code", 486)))
        self.artifacts.record_event("call_rejected", call=call.id)
        self._report_call_ended(call_ref, call)

    async def _step_wait_state(self, params: dict[str, Any]) -> None:
        """Run a ``wait_state`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call_ref = str(params["call"])
        state = str(params["state"])
        call = self._call(call_ref)
        await self.backend.wait_call_state(
            call,
            state=state,
            timeout=float(params.get("timeout", 30.0)),
        )
        self.artifacts.record_event("call_state_seen", call=call.id, state=state)
        if state == "connected":
            self._report_call_established(call_ref, call)
        if state == "disconnected":
            self._report_call_ended(call_ref, call)

    async def _step_wait_media(self, params: dict[str, Any]) -> None:
        """Wait until audio media is established for a call.

        :param params: Step parameters.
        :returns: None.
        :raises WxTimeoutError: If media is not observed before timeout.
        """

        call = self._call(str(params["call"]))
        timeout = float(params.get("timeout", 30.0))
        deadline = asyncio.get_running_loop().time() + timeout
        while not call.media_active:
            if call.state == "disconnected":
                self._report_call_ended(str(params["call"]), call)
                raise WxTimeoutError(f"Call {call.id} disconnected before audio media became active")
            if asyncio.get_running_loop().time() >= deadline:
                raise WxTimeoutError(f"Timed out waiting for audio media on call {call.id}")
            await asyncio.sleep(0.01)
        self.artifacts.record_event("call_media_seen", call=call.id, media="audio")

    async def _step_wait_behavior_state(self, params: dict[str, Any]) -> None:
        """Run a ``wait_behavior_state`` scenario step.

        :param params: Step parameters.
        :returns: None.
        :raises ScenarioError: If no behavior runtime is active.
        """

        if self._behavior_runtime is None:
            raise ScenarioError("wait_behavior_state requires endpoint behaviors")
        endpoint = str(params["endpoint"])
        state = str(params["state"])
        await self._behavior_runtime.wait_endpoint_state(
            endpoint=endpoint,
            state=state,
            timeout=float(params.get("timeout", 30.0)),
        )
        self.artifacts.record_event("behavior_state_seen", endpoint=endpoint, state=state)

    async def _step_trigger_behavior(self, params: dict[str, Any]) -> None:
        """Run a ``trigger_behavior`` scenario step.

        :param params: Step parameters.
        :returns: None.
        :raises ScenarioError: If no behavior runtime is active.
        """

        if self._behavior_runtime is None:
            raise ScenarioError("trigger_behavior requires endpoint behaviors")
        endpoint = str(params["endpoint"])
        name = str(params["name"])
        await self._behavior_runtime.emit_trigger(endpoint=endpoint, name=name)
        self.artifacts.record_event("behavior_trigger_emitted", endpoint=endpoint, name=name)

    async def _step_play_tts(self, params: dict[str, Any]) -> None:
        """Run a ``play_tts`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call_ref = str(params["call"])
        call = self._call(call_ref)
        asset = self.media_factory.prepare_tts(
            text=str(params["text"]),
            marker=str(params["marker"]) if params.get("marker") else None,
            voice=str(params["voice"]) if params.get("voice") else None,
        )
        self._progress(f"playing TTS: {call_ref} <- {asset.path}")
        await self.backend.play_wav(call, asset.path)
        self.artifacts.record_event("tts_played", call=call.id, path=str(asset.path), marker=asset.marker)
        self._progress(f"played TTS: {call_ref}")

    async def _step_play_wav(self, params: dict[str, Any]) -> None:
        """Run a ``play_wav`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call_ref = str(params["call"])
        call = self._call(call_ref)
        asset = self.media_factory.prepare_wav(
            source=Path(str(params["path"])),
            marker=str(params["marker"]) if params.get("marker") else None,
        )
        self._progress(f"playing WAV: {call_ref} <- {asset.path}")
        await self.backend.play_wav(call, asset.path)
        self.artifacts.record_event("wav_played", call=call.id, path=str(asset.path), marker=asset.marker)
        self._progress(f"played WAV: {call_ref}")

    async def _step_record(self, params: dict[str, Any]) -> None:
        """Run a ``record`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call = self._call(str(params["call"]))
        name = str(params.get("save_as", "recording"))
        path = self.artifacts.path_for("media", name, ".wav")
        self._progress(f"recording media: {params['call']} -> {path}")
        await self.backend.record_wav(call, path, seconds=float(params.get("seconds", 3.0)))
        self.recordings[name] = path
        self.artifacts.record_event("recorded", call=call.id, recording=name, path=str(path))
        self._progress(f"recorded media: {path}")

    async def _step_record_during_playback(self, params: dict[str, Any]) -> None:
        """Record one call leg while playing media into another leg.

        :param params: Step parameters.
        :returns: None.
        :raises ScenarioError: If marker assertion is requested without a marker.
        :raises BackendError: If the requested marker is not detected.
        """

        playback_ref = str(params["playback_call"])
        recording_ref = str(params["recording_call"])
        playback_call = self._call(playback_ref)
        recording_call = self._call(recording_ref)
        asset = self._playback_asset_from_params(params)
        pre_roll = float(params.get("pre_roll", 0.25))
        post_roll = float(params.get("post_roll", 0.5))
        explicit_seconds = params.get("seconds")
        asset_seconds = _wav_duration_seconds(asset.path)
        recording_seconds = (
            float(explicit_seconds)
            if explicit_seconds is not None
            else pre_roll + asset_seconds + post_roll + _RECORD_DURING_PLAYBACK_GRACE_SECONDS
        )
        name = str(params.get("save_as", "recording"))
        path = self.artifacts.path_for("media", name, ".wav")

        self._progress(f"recording media during playback: {recording_ref} -> {path}")
        record_task = asyncio.create_task(
            self.backend.record_wav(recording_call, path, seconds=recording_seconds),
            name=f"wxcalls-record-{name}",
        )
        try:
            if pre_roll:
                await asyncio.sleep(pre_roll)
            self._progress(f"playing media during recording: {playback_ref} <- {asset.path}")
            await self.backend.play_wav(playback_call, asset.path)
            if post_roll:
                await asyncio.sleep(post_roll)
            if explicit_seconds is None and not record_task.done():
                await asyncio.sleep(0)
            if explicit_seconds is None and not record_task.done():
                record_task.cancel()
                with suppress(asyncio.CancelledError):
                    await record_task
            else:
                await record_task
        except Exception:
            if record_task.done():
                with suppress(Exception, asyncio.CancelledError):
                    await record_task
            else:
                record_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await record_task
            raise

        self.recordings[name] = path
        self.artifacts.record_event("recorded", call=recording_call.id, recording=name, path=str(path))
        self.artifacts.record_event(
            "recorded_during_playback",
            playback_call=playback_call.id,
            recording_call=recording_call.id,
            recording=name,
            path=str(path),
            marker=asset.marker,
        )
        self._progress(f"recorded media: {path}")

        should_assert_marker = bool(params.get("assert_marker", asset.marker is not None))
        if should_assert_marker:
            if asset.marker is None:
                raise ScenarioError("record_during_playback assert_marker requires a marker")
            self._assert_marker(path, asset.marker)

    async def _step_assert_marker(self, params: dict[str, Any]) -> None:
        """Run an ``assert_marker`` scenario step.

        :param params: Step parameters.
        :returns: None.
        :raises ScenarioError: If the named recording is unknown.
        :raises BackendError: If the marker cannot be detected.
        """

        recording = self.recordings.get(str(params["recording"]))
        if recording is None:
            raise ScenarioError(f"Unknown recording: {params['recording']}")
        marker = str(params["marker"])
        self._assert_marker(recording, marker)

    async def _step_hold(self, params: dict[str, Any]) -> None:
        """Run a ``hold`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call = self._call(str(params["call"]))
        await self.backend.hold(call)
        self.artifacts.record_event("call_held", call=call.id)

    async def _step_resume(self, params: dict[str, Any]) -> None:
        """Run a ``resume`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        call = self._call(str(params["call"]))
        await self.backend.resume(call)
        self.artifacts.record_event("call_resumed", call=call.id)

    async def _step_consult_call(self, params: dict[str, Any]) -> None:
        """Run a ``consult_call`` step using the regular call path.

        :param params: Step parameters.
        :returns: None.
        """

        await self._step_call(params | {"save_as": params.get("save_as", "consult_call")})

    async def _step_attended_transfer(self, params: dict[str, Any]) -> None:
        """Run an ``attended_transfer`` scenario step.

        :param params: Step parameters.
        :returns: None.
        """

        primary = self._call(str(params["primary_call"]))
        consult = self._call(str(params["consult_call"]))
        await self.backend.attended_transfer(primary, consult)
        self.artifacts.record_event("attended_transfer", primary=primary.id, consult=consult.id)

    async def _step_hangup(self, params: dict[str, Any]) -> None:
        """Run a ``hangup`` scenario step for one or more calls.

        :param params: Step parameters.
        :returns: None.
        """

        for name in _as_list(params.get("calls", params.get("call"))):
            call_ref = str(name)
            call = self._call(call_ref)
            await self.backend.hangup(call)
            self.artifacts.record_event("call_hung_up", call=call.id)
            self._report_call_ended(call_ref, call)

    async def _step_video_smoke(self, params: dict[str, Any]) -> None:
        """Run a ``video_smoke`` scenario step.

        :param params: Step parameters.
        :returns: None.
        :raises BackendError: If the backend reports a non-skipped video failure.
        """

        result = await self.backend.video_smoke(
            client_name=str(params["client"]),
            target_uri=self.config.resolve_target_uri(str(params["target"])),
            timeout=float(params.get("timeout", 30.0)),
        )
        self.artifacts.record_event(
            "video_smoke",
            supported=result.supported,
            skipped=result.skipped,
            evidence=result.evidence,
        )
        if not result.supported and not result.skipped:
            raise BackendError(f"Video smoke failed: {result.evidence}")

    def _playback_asset_from_params(self, params: dict[str, Any]) -> MediaAsset:
        """Prepare playback media from ``text`` or ``path`` step parameters.

        :param params: Scenario step parameters.
        :returns: Prepared media asset.
        """

        marker = str(params["marker"]) if params.get("marker") else None
        if "text" in params:
            return self.media_factory.prepare_tts(
                text=str(params["text"]),
                marker=marker,
                voice=str(params["voice"]) if params.get("voice") else None,
            )
        return self.media_factory.prepare_wav(
            source=Path(str(params["path"])),
            marker=marker,
        )

    def _assert_marker(self, recording: Path, marker: str) -> None:
        """Assert that a marker is present in a recording artifact.

        :param recording: Recording WAV path.
        :param marker: Marker identifier.
        :returns: None.
        :raises BackendError: If the marker cannot be detected.
        """

        if not detect_marker(recording, marker):
            raise BackendError(f"Marker {marker!r} was not detected in recording {recording}")
        self.artifacts.record_event("marker_detected", recording=str(recording), marker=marker)

    def _call(self, name: str) -> CallHandle:
        """Return a call handle saved under a scenario name.

        :param name: Scenario call reference.
        :returns: Saved call handle.
        :raises ScenarioError: If the name is unknown.
        """

        try:
            return self.calls[name]
        except KeyError as exc:
            raise ScenarioError(f"Unknown call reference: {name}") from exc

    def _progress(self, message: str) -> None:
        """Emit one progress line if a reporter was provided.

        :param message: Human-readable progress message.
        :returns: None.
        """

        if self.progress_reporter is not None:
            self.progress_reporter(message)

    def _report_call_established(self, call_ref: str, call: CallHandle) -> None:
        """Report call establishment once per backend call id.

        :param call_ref: Scenario call reference.
        :param call: Established call handle.
        :returns: None.
        """

        if call.id in self._reported_established_call_ids:
            return
        self._reported_established_call_ids.add(call.id)
        self._progress(f"call established: {call_ref} {call.client_name} <-> {call.remote_uri}")

    def _report_call_ended(self, call_ref: str, call: CallHandle) -> None:
        """Report call completion once per backend call id.

        :param call_ref: Scenario call reference.
        :param call: Ended call handle.
        :returns: None.
        """

        if call.id in self._reported_ended_call_ids:
            return
        self._reported_ended_call_ids.add(call.id)
        self._progress(f"call ended: {call_ref} {call.client_name} <-> {call.remote_uri}")


def make_backend(name: str) -> SipBackend:
    """Create a backend by name.

    :param name: Backend name, either ``fake`` or ``pjsua2``.
    :returns: Backend instance.
    :raises ScenarioError: If the backend is unknown.
    """

    normalized = name.lower()
    if normalized == "fake":
        return FakeSipBackend()
    if normalized == "pjsua2":
        return Pjsua2SipBackend()
    raise ScenarioError(f"Unsupported backend: {name}")


@asynccontextmanager
async def lab_from_config(
    config_path: str | Path,
    env_file: str | Path | None = ".env",
    backend_name: str = "pjsua2",
    progress_reporter: Callable[[str], None] | None = None,
) -> AsyncIterator[CallLab]:
    """Create and initialize a call lab from configuration.

    :param config_path: Config YAML path.
    :param env_file: Optional env file path.
    :param backend_name: Backend name.
    :param progress_reporter: Optional callback for human-readable progress lines.
    :yields: Initialized call lab.
    """

    lab = CallLab.from_config_path(
        config_path,
        env_file=env_file,
        backend_name=backend_name,
        progress_reporter=progress_reporter,
    )
    async with lab:
        yield lab


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


def _should_stay_registered(params: dict[str, Any]) -> bool:
    """Return whether a register step requests a registration soak.

    :param params: Register step parameters.
    :returns: ``True`` when the scenario should wait after registration.
    """

    return bool(params.get("stay_registered", False)) or "stay_registered_for" in params


def _is_e164_number(value: str) -> bool:
    """Return whether a target is a plus-prefixed E.164 dial string.

    :param value: Candidate target string.
    :returns: ``True`` when the value begins with ``+`` and contains digits only.
    """

    return re.fullmatch(r"\+[0-9]+", value) is not None


def _wav_duration_seconds(path: Path) -> float:
    """Return the duration of a WAV file.

    :param path: WAV file path.
    :returns: Duration in seconds.
    :raises BackendError: If the WAV file cannot be inspected.
    """

    try:
        with wave.open(str(path), "rb") as wav:
            frame_rate = wav.getframerate()
            if frame_rate <= 0:
                raise BackendError(f"Invalid WAV frame rate for {path}: {frame_rate}")
            return wav.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error) as exc:
        raise BackendError(f"Unable to inspect WAV duration for {path}: {exc}") from exc


def _exception_summary(exc: BaseException) -> str:
    """Return a concise message for nested task-group exceptions.

    :param exc: Exception or exception group to summarize.
    :returns: Human-readable exception message.
    """

    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_exception_summary(nested) for nested in exc.exceptions)
    return str(exc)


def _sip_host(uri: str) -> str | None:
    """Extract a SIP host from a registrar URI.

    :param uri: SIP or SIPS URI.
    :returns: Host portion, or ``None`` when unavailable.
    """

    remainder = uri.split(":", 1)[1] if ":" in uri else uri
    host = remainder.split("@", 1)[-1].split(";", 1)[0].split("?", 1)[0].strip()
    return host or None
