"""Pytest integration for YAML Webex Calling scenarios."""

from __future__ import annotations

import asyncio
import glob
from pathlib import Path
from typing import Any

import pytest

from wxcalls.orchestrator import CallLab, lab_from_config


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register Webex Calling pytest options.

    :param parser: Pytest option parser.
    :returns: None.
    """

    group = parser.getgroup("wxcalls")
    group.addoption(
        "--scenario",
        action="append",
        default=[],
        help="Scenario YAML file or glob to collect and run.",
    )
    group.addoption(
        "--wxcalls-config",
        action="store",
        default="config.local.yml",
        help="Framework config YAML path.",
    )
    group.addoption(
        "--wxcalls-env-file",
        action="store",
        default=".env",
        help="Local dotenv file for SIP credentials.",
    )
    group.addoption(
        "--wxcalls-backend",
        action="store",
        choices=("fake", "pjsua2"),
        default="pjsua2",
        help="Scenario backend.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Add ``--scenario`` paths to pytest collection args.

    :param config: Active pytest configuration.
    :returns: None.
    """

    scenario_paths = _expanded_scenarios(config)
    selected = {str(path.resolve()) for path in scenario_paths}
    config._wxcalls_scenarios = selected  # type: ignore[attr-defined]
    # Pytest only asks collectors about files in its argument set, so selected YAML
    # scenarios are appended before collection starts.
    for path in scenario_paths:
        text = str(path)
        if text not in config.args:
            config.args.append(text)


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.File | None:
    """Collect selected YAML scenario files as pytest tests.

    :param file_path: Candidate file path from pytest collection.
    :param parent: Parent pytest collector.
    :returns: Scenario file collector when selected, otherwise ``None``.
    """

    selected = getattr(parent.config, "_wxcalls_scenarios", set())
    if str(file_path.resolve()) in selected:
        return ScenarioFile.from_parent(parent, path=file_path)
    return None


class ScenarioFile(pytest.File):
    """Pytest collector for one YAML scenario file."""

    def collect(self) -> list[ScenarioItem]:
        """Yield one test item for the scenario file.

        :returns: List containing the scenario test item.
        """

        return [ScenarioItem.from_parent(self, name=self.path.stem)]


class ScenarioItem(pytest.Item):
    """Pytest item that executes a YAML scenario."""

    def runtest(self) -> None:
        """Run the scenario via ``CallLab``.

        :returns: None.
        """

        config = self.config.getoption("--wxcalls-config")
        env_file = self.config.getoption("--wxcalls-env-file")
        backend = self.config.getoption("--wxcalls-backend")

        async def run() -> None:
            """Execute the collected scenario inside a managed async lab.

            :returns: None.
            """

            async with lab_from_config(
                config,
                env_file=env_file,
                backend_name=backend,
                progress_reporter=_print_progress,
            ) as lab:
                await lab.run_scenario_path(self.path)

        asyncio.run(run())

    def repr_failure(self, excinfo: pytest.ExceptionInfo[BaseException]) -> str:
        """Render scenario failures with path context.

        :param excinfo: Pytest exception wrapper.
        :returns: Human-readable failure text.
        """

        return f"{self.path}: {excinfo.value}"

    def reportinfo(self) -> tuple[Path, int | None, str]:
        """Return pytest report metadata.

        :returns: Path, line number, and display name for pytest reports.
        """

        return self.path, 0, f"wxcalls scenario: {self.path.name}"


@pytest.fixture
def call_lab(request: pytest.FixtureRequest) -> CallLab:
    """Create an uninitialized call lab from pytest options.

    :param request: Pytest fixture request.
    :returns: Call lab configured from pytest options.
    """

    return CallLab.from_config_path(
        request.config.getoption("--wxcalls-config"),
        env_file=request.config.getoption("--wxcalls-env-file"),
        backend_name=request.config.getoption("--wxcalls-backend"),
    )


@pytest.fixture
def client(call_lab: CallLab) -> Any:
    """Return a lookup function for configured SIP clients.

    :param call_lab: Configured call lab fixture.
    :returns: Client lookup callable.
    """

    return call_lab.client


@pytest.fixture
def target(call_lab: CallLab) -> Any:
    """Return a lookup function for configured targets.

    :param call_lab: Configured call lab fixture.
    :returns: Target lookup callable.
    """

    return call_lab.target


@pytest.fixture
def run_scenario(request: pytest.FixtureRequest) -> Any:
    """Return a helper that runs a scenario path inside a managed lab.

    :param request: Pytest fixture request.
    :returns: Callable that executes a scenario path.
    """

    def _run(path: str | Path) -> None:
        """Run one scenario path through a temporary lab.

        :param path: Scenario YAML path.
        :returns: None.
        """

        async def run() -> None:
            """Execute the scenario inside the async lab lifecycle.

            :returns: None.
            """

            async with lab_from_config(
                request.config.getoption("--wxcalls-config"),
                env_file=request.config.getoption("--wxcalls-env-file"),
                backend_name=request.config.getoption("--wxcalls-backend"),
                progress_reporter=_print_progress,
            ) as lab:
                await lab.run_scenario_path(path)

        asyncio.run(run())

    return _run


def _expanded_scenarios(config: pytest.Config) -> list[Path]:
    """Expand pytest ``--scenario`` patterns into concrete paths.

    :param config: Active pytest configuration.
    :returns: Scenario paths, preserving unmatched patterns as literal paths.
    """

    paths: list[Path] = []
    for pattern in config.getoption("--scenario"):
        matches = [Path(match) for match in glob.glob(pattern)]
        paths.extend(matches or [Path(pattern)])
    return paths


def _print_progress(message: str) -> None:
    """Print one scenario progress line.

    :param message: Progress message from the orchestrator.
    :returns: None.
    """

    print(f"[wxcalls] {message}", flush=True)
