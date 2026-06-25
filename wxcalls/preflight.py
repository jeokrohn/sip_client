"""Preflight diagnostics for local Webex Calling scenario runs."""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path

from wxcalls.config import load_config
from wxcalls.exceptions import ConfigError


@dataclass(frozen=True)
class PreflightCheck:
    """One diagnostic check result.

    :param name: Check name.
    :param status: ``ok``, ``warn``, or ``fail``.
    :param detail: Human-readable result detail.
    """

    name: str
    status: str
    detail: str


def run_preflight(config_path: Path, env_file: Path | None = Path(".env")) -> list[PreflightCheck]:
    """Run local preflight checks.

    :param config_path: Configuration YAML path.
    :param env_file: Optional local env file.
    :returns: Ordered check results.
    """

    checks: list[PreflightCheck] = []
    try:
        config = load_config(config_path, env_file=env_file)
        checks.append(PreflightCheck("config", "ok", f"loaded {config_path}"))
    except ConfigError as exc:
        return [PreflightCheck("config", "fail", str(exc))]

    try:
        config.validate_credentials()
        checks.append(PreflightCheck("credentials", "ok", "all configured SIP secrets are present"))
    except ConfigError as exc:
        checks.append(PreflightCheck("credentials", "fail", str(exc)))

    try:
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        probe = config.artifacts_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(PreflightCheck("artifacts", "ok", f"writable: {config.artifacts_dir}"))
    except OSError as exc:
        checks.append(PreflightCheck("artifacts", "fail", f"not writable: {exc}"))

    if importlib.util.find_spec("pjsua2") is None:
        checks.append(
            PreflightCheck(
                "pjsua2",
                "warn",
                "pjsua2 is not importable; install native bindings before live backend runs",
            )
        )
    else:
        checks.append(PreflightCheck("pjsua2", "ok", "pjsua2 import found"))

    for command in ("say", "afconvert"):
        if shutil.which(command):
            checks.append(PreflightCheck(command, "ok", f"{command} found"))
        else:
            checks.append(
                PreflightCheck(
                    command,
                    "warn",
                    f"{command} not found; runtime TTS is unavailable on this host",
                )
            )

    return checks


def has_failures(checks: list[PreflightCheck]) -> bool:
    """Return whether any preflight check failed.

    :param checks: Check results.
    :returns: ``True`` if any check has status ``fail``.
    """

    return any(check.status == "fail" for check in checks)
