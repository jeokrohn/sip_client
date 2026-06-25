"""Async scenario orchestration over a SIP backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from wxcalls.artifacts import ArtifactWriter
from wxcalls.backends.base import CallHandle, RegistrationResult, SipBackend
from wxcalls.backends.fake import FakeSipBackend
from wxcalls.backends.pjsua2 import Pjsua2SipBackend
from wxcalls.config import LabConfig, load_config
from wxcalls.exceptions import BackendError, ScenarioError
from wxcalls.media import MediaFactory, detect_marker
from wxcalls.scenario import Scenario, ScenarioStep, load_scenario


class CallLab:
    """Coordinates clients, call handles, media, and scenario execution."""

    def __init__(
        self,
        config: LabConfig,
        backend: SipBackend | None = None,
        artifact_writer: ArtifactWriter | None = None,
        media_factory: MediaFactory | None = None,
    ) -> None:
        """Create a call lab.

        :param config: Lab configuration.
        :param backend: SIP backend. Defaults to the fake backend.
        :param artifact_writer: Artifact writer for run outputs.
        :param media_factory: Media generator.
        """

        self.config = config
        self.backend = backend or FakeSipBackend()
        self.artifacts = artifact_writer or ArtifactWriter(config.artifacts_dir)
        self.media_factory = media_factory or MediaFactory(self.artifacts.path_for("media", "generated", ""))
        self.calls: dict[str, CallHandle] = {}
        self.registrations: dict[str, RegistrationResult] = {}
        self.recordings: dict[str, Path] = {}

    @classmethod
    def from_config_path(
        cls,
        path: str | Path,
        env_file: str | Path | None = ".env",
        backend_name: str = "fake",
    ) -> CallLab:
        """Create a lab from a YAML config file.

        :param path: Configuration path.
        :param env_file: Optional local env file.
        :param backend_name: ``fake`` or ``pjsua2``.
        :returns: New call lab.
        """

        return cls(config=load_config(path, env_file=env_file), backend=make_backend(backend_name))

    async def __aenter__(self) -> CallLab:
        pjsip_log = self.artifacts.path_for("logs", "pjsip", ".log")
        await self.backend.initialize(self.config, pjsip_log_path=pjsip_log)
        self.artifacts.record_event("backend_initialized", backend=type(self.backend).__name__)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.backend.shutdown()
        self.artifacts.record_event("backend_shutdown")
        self.artifacts.finalize()

    def client(self, name: str) -> Any:
        """Return a configured client by name."""

        return self.config.client(name)

    def target(self, name: str) -> Any:
        """Return a configured target by name."""

        return self.config.target(name)

    async def run_scenario_path(self, path: str | Path) -> None:
        """Load and run a scenario file.

        :param path: Scenario YAML file.
        """

        await self.run_scenario(load_scenario(path))

    async def run_scenario(self, scenario: Scenario) -> None:
        """Run a parsed scenario.

        :param scenario: Scenario to execute.
        """

        self.artifacts.record_event("scenario_started", name=scenario.name, source=str(scenario.source or ""))
        for step in scenario.steps:
            self.artifacts.record_event("step_started", index=step.index, action=step.action)
            await self._run_step(step)
            self.artifacts.record_event("step_finished", index=step.index, action=step.action)
        self.artifacts.record_event("scenario_finished", name=scenario.name)

    async def _run_step(self, step: ScenarioStep) -> None:
        handler = getattr(self, f"_step_{step.action}", None)
        if handler is None:
            raise ScenarioError(f"Unsupported action: {step.action}")
        await handler(step.params)

    async def _step_register(self, params: dict[str, Any]) -> None:
        clients = _as_list(params.get("clients", params.get("client")))
        timeout = float(params.get("timeout", 30.0))
        registered: list[RegistrationResult] = []
        for client_name in clients:
            client = self.config.client(str(client_name))
            result = await self.backend.register_client(client, client.credentials(self.config.env), timeout=timeout)
            self.registrations[client.name] = result
            registered.append(result)
            self.artifacts.record_event("client_registered", client=client.name, expires=result.expires)

        if _should_stay_registered(params):
            await asyncio.gather(*(self._stay_registered(result, params) for result in registered))

    async def _stay_registered(self, result: RegistrationResult, params: dict[str, Any]) -> None:
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
        timeout = float(params.get("timeout", 30.0))
        target_uri = self.config.resolve_target_uri(str(params["target"]))
        call = await self.backend.place_call(
            client_name=str(params["client"]),
            target_uri=target_uri,
            timeout=timeout,
            video=bool(params.get("video", False)),
        )
        self.calls[str(params.get("save_as", "call"))] = call
        self.artifacts.record_event("call_placed", call=call.id, client=call.client_name, target=target_uri)

    async def _step_expect_incoming(self, params: dict[str, Any]) -> None:
        timeout = float(params.get("timeout", 30.0))
        from_uri = params.get("from_uri")
        call = await self.backend.wait_for_incoming(
            client_name=str(params["client"]),
            timeout=timeout,
            from_uri=str(from_uri) if from_uri else None,
        )
        self.calls[str(params.get("save_as", f"{params['client']}_incoming"))] = call
        self.artifacts.record_event("incoming_call_seen", call=call.id, client=call.client_name)

    async def _step_answer(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        await self.backend.answer(call, status_code=int(params.get("status_code", 200)))
        self.artifacts.record_event("call_answered", call=call.id)

    async def _step_reject(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        await self.backend.reject(call, status_code=int(params.get("status_code", 486)))
        self.artifacts.record_event("call_rejected", call=call.id)

    async def _step_wait_state(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        await self.backend.wait_call_state(
            call,
            state=str(params["state"]),
            timeout=float(params.get("timeout", 30.0)),
        )
        self.artifacts.record_event("call_state_seen", call=call.id, state=str(params["state"]))

    async def _step_play_tts(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        asset = self.media_factory.prepare_tts(
            text=str(params["text"]),
            marker=str(params["marker"]) if params.get("marker") else None,
            voice=str(params["voice"]) if params.get("voice") else None,
        )
        await self.backend.play_wav(call, asset.path)
        self.artifacts.record_event("tts_played", call=call.id, path=str(asset.path), marker=asset.marker)

    async def _step_play_wav(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        asset = self.media_factory.prepare_wav(
            source=Path(str(params["path"])),
            marker=str(params["marker"]) if params.get("marker") else None,
        )
        await self.backend.play_wav(call, asset.path)
        self.artifacts.record_event("wav_played", call=call.id, path=str(asset.path), marker=asset.marker)

    async def _step_record(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        name = str(params.get("save_as", "recording"))
        path = self.artifacts.path_for("media", name, ".wav")
        await self.backend.record_wav(call, path, seconds=float(params.get("seconds", 3.0)))
        self.recordings[name] = path
        self.artifacts.record_event("recorded", call=call.id, recording=name, path=str(path))

    async def _step_assert_marker(self, params: dict[str, Any]) -> None:
        recording = self.recordings.get(str(params["recording"]))
        if recording is None:
            raise ScenarioError(f"Unknown recording: {params['recording']}")
        marker = str(params["marker"])
        if not detect_marker(recording, marker):
            raise BackendError(f"Marker {marker!r} was not detected in recording {recording}")
        self.artifacts.record_event("marker_detected", recording=str(recording), marker=marker)

    async def _step_hold(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        await self.backend.hold(call)
        self.artifacts.record_event("call_held", call=call.id)

    async def _step_resume(self, params: dict[str, Any]) -> None:
        call = self._call(str(params["call"]))
        await self.backend.resume(call)
        self.artifacts.record_event("call_resumed", call=call.id)

    async def _step_consult_call(self, params: dict[str, Any]) -> None:
        await self._step_call(params | {"save_as": params.get("save_as", "consult_call")})

    async def _step_attended_transfer(self, params: dict[str, Any]) -> None:
        primary = self._call(str(params["primary_call"]))
        consult = self._call(str(params["consult_call"]))
        await self.backend.attended_transfer(primary, consult)
        self.artifacts.record_event("attended_transfer", primary=primary.id, consult=consult.id)

    async def _step_hangup(self, params: dict[str, Any]) -> None:
        for name in _as_list(params.get("calls", params.get("call"))):
            call = self._call(str(name))
            await self.backend.hangup(call)
            self.artifacts.record_event("call_hung_up", call=call.id)

    async def _step_video_smoke(self, params: dict[str, Any]) -> None:
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

    def _call(self, name: str) -> CallHandle:
        try:
            return self.calls[name]
        except KeyError as exc:
            raise ScenarioError(f"Unknown call reference: {name}") from exc


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
) -> AsyncIterator[CallLab]:
    """Create and initialize a call lab from configuration.

    :param config_path: Config YAML path.
    :param env_file: Optional env file path.
    :param backend_name: Backend name.
    :yields: Initialized call lab.
    """

    lab = CallLab.from_config_path(config_path, env_file=env_file, backend_name=backend_name)
    async with lab:
        yield lab


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def _should_stay_registered(params: dict[str, Any]) -> bool:
    return bool(params.get("stay_registered", False)) or "stay_registered_for" in params
