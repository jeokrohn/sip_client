"""Command-line interface for Webex Calling scenario tests."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from wxcalls.orchestrator import lab_from_config
from wxcalls.preflight import has_failures, run_preflight


def main(argv: list[str] | None = None) -> int:
    """Run the ``wxcalls`` command-line interface.

    :param argv: Optional argument vector.
    :returns: Process exit code.
    """

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return _cmd_preflight(args)
    if args.command == "run":
        return _cmd_run(args)
    parser.print_help()
    return 2


def _cmd_preflight(args: argparse.Namespace) -> int:
    checks = run_preflight(Path(args.config), env_file=Path(args.env_file) if args.env_file else None)
    for check in checks:
        print(f"{check.status.upper():5} {check.name}: {check.detail}")
    return 1 if has_failures(checks) else 0


def _cmd_run(args: argparse.Namespace) -> int:
    async def run() -> None:
        async with lab_from_config(
            config_path=args.config,
            env_file=args.env_file,
            backend_name=args.backend,
            progress_reporter=_print_progress,
        ) as lab:
            for scenario in args.scenarios:
                await lab.run_scenario_path(scenario)

    asyncio.run(run())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxcalls")
    subparsers = parser.add_subparsers(dest="command")

    preflight = subparsers.add_parser("preflight", help="check local config and dependencies")
    preflight.add_argument("-c", "--config", required=True, help="framework YAML config path")
    preflight.add_argument("--env-file", default=".env", help="local env file path")

    run = subparsers.add_parser("run", help="run scenario YAML files")
    run.add_argument("-c", "--config", required=True, help="framework YAML config path")
    run.add_argument("--env-file", default=".env", help="local env file path")
    run.add_argument("--backend", choices=["fake", "pjsua2"], default="pjsua2", help="backend to use")
    run.add_argument("scenarios", nargs="+", help="scenario YAML files")

    return parser


def _print_progress(message: str) -> None:
    """Print one scenario progress line.

    :param message: Progress message from the orchestrator.
    """

    print(f"[wxcalls] {message}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
