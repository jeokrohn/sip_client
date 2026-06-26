"""Configuration loading for SIP clients, targets, and local secrets."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from wxcalls.exceptions import ConfigError


@dataclass(frozen=True)
class SipCredentials:
    """Resolved SIP authentication credentials.

    :param username: SIP digest username.
    :param password: SIP digest password.
    """

    username: str
    password: str


@dataclass(frozen=True)
class SipClientConfig:
    """Configuration for one pre-provisioned generic SIP phone identity.

    :param name: Stable logical name used by scenarios.
    :param id_uri: SIP identity URI used in the account ``From`` header.
    :param registrar_uri: SIP registrar URI.
    :param username_env: Environment variable containing the SIP username.
    :param password_env: Environment variable containing the SIP password.
    :param proxy_uri: Optional outbound proxy or route URI.
    :param transport: Preferred transport label, typically ``tls``, ``tcp``, or ``udp``.
    :param enable_video: Whether video may be offered for video smoke tests.
    :param local_port: Optional local SIP signaling port override.
    """

    name: str
    id_uri: str
    registrar_uri: str
    username_env: str
    password_env: str
    proxy_uri: str | None = None
    transport: str = "tls"
    enable_video: bool = False
    local_port: int | None = None

    def credentials(self, env: dict[str, str] | None = None) -> SipCredentials:
        """Resolve configured credential environment variables.

        :param env: Environment mapping to read. Defaults to ``os.environ``.
        :returns: Resolved SIP credentials.
        :raises ConfigError: If a referenced environment variable is missing.
        """

        source = os.environ if env is None else env
        username = source.get(self.username_env)
        password = source.get(self.password_env)
        missing = [
            name
            for name, value in (
                (self.username_env, username),
                (self.password_env, password),
            )
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(f"Missing SIP credential environment variable(s): {joined}")
        return SipCredentials(username=username or "", password=password or "")


@dataclass(frozen=True)
class TargetConfig:
    """Configuration for an opaque call target.

    :param name: Stable logical name used by scenarios.
    :param uri: Dialable SIP URI or address.
    :param kind: Human-readable target type, such as ``webex`` or ``pstn``.
    """

    name: str
    uri: str
    kind: str = "sip"


@dataclass(frozen=True)
class LabConfig:
    """Top-level framework configuration.

    :param clients: SIP clients available to scenarios.
    :param targets: Opaque non-simulated call targets.
    :param artifacts_dir: Directory where run artifacts are written.
    :param dns_nameservers: Optional DNS resolver addresses for PJSIP SRV/NAPTR lookups.
    :param env: Resolved local environment including values from ``.env``.
    """

    clients: tuple[SipClientConfig, ...]
    targets: tuple[TargetConfig, ...] = ()
    artifacts_dir: Path = Path("artifacts")
    dns_nameservers: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)

    def client(self, name: str) -> SipClientConfig:
        """Return a configured SIP client by logical name.

        :param name: Logical client name.
        :returns: Matching client configuration.
        :raises ConfigError: If the client is unknown.
        """

        for client in self.clients:
            if client.name == name:
                return client
        raise ConfigError(f"Unknown SIP client: {name}")

    def target(self, name: str) -> TargetConfig:
        """Return a configured target by logical name.

        :param name: Logical target name.
        :returns: Matching target configuration.
        :raises ConfigError: If the target is unknown.
        """

        for target in self.targets:
            if target.name == name:
                return target
        raise ConfigError(f"Unknown call target: {name}")

    def resolve_target_uri(self, target: str) -> str:
        """Resolve a scenario target reference into a dialable URI.

        :param target: Client name, target name, or literal URI.
        :returns: SIP URI/address suitable for the backend.
        """

        for client in self.clients:
            if client.name == target:
                return client.id_uri
        for configured_target in self.targets:
            if configured_target.name == target:
                return configured_target.uri
        return target

    def validate_credentials(self) -> None:
        """Validate that all configured client credentials can be resolved.

        :returns: None.
        :raises ConfigError: If any client secret is missing.
        """

        for client in self.clients:
            client.credentials(self.env)


def load_dotenv(path: Path) -> dict[str, str]:
    """Load a small ``KEY=VALUE`` env file without mutating ``os.environ``.

    :param path: File to load.
    :returns: Parsed key/value pairs, or an empty mapping if the file does not exist.
    """

    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {line_no}: empty key")
        values[key] = _strip_env_quotes(value.strip())
    return values


def load_config(path: str | Path, env_file: str | Path | None = ".env") -> LabConfig:
    """Load and validate a framework configuration file.

    :param path: YAML configuration file.
    :param env_file: Optional dotenv-style file for local secrets.
    :returns: Parsed lab configuration.
    :raises ConfigError: If the YAML structure is invalid.
    """

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in configuration file {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a mapping")

    # Local dotenv values are layered over the process environment so shell-provided
    # secrets remain available while per-lab overrides stay easy.
    env = dict(os.environ)
    if env_file:
        env.update(load_dotenv(Path(env_file)))

    clients = tuple(_parse_client(item) for item in _required_list(raw, "clients"))
    targets = tuple(_parse_target(item) for item in raw.get("targets", []) or [])
    artifacts_dir = Path(str(raw.get("artifacts_dir", "artifacts")))
    # Explicit YAML nameservers win; an empty field falls back to the host resolver
    # configuration for Mac-first live PJSUA2 runs.
    dns_nameservers = _optional_string_list(raw, "dns_nameservers") or get_dns_from_scutil()
    _ensure_unique("client", [client.name for client in clients])
    _ensure_unique("target", [target.name for target in targets])

    return LabConfig(
        clients=clients,
        targets=targets,
        artifacts_dir=artifacts_dir,
        dns_nameservers=dns_nameservers,
        env=env,
    )


def get_dns_from_scutil() -> tuple[str, ...]:
    """Return system DNS nameservers reported by ``scutil --dns``.

    :returns: Deduplicated nameservers in system resolver order, or an empty
        tuple when ``scutil`` is unavailable.
    """

    try:
        result = subprocess.run(["scutil", "--dns"], capture_output=True, text=True)
    except OSError:
        return ()

    servers = re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", result.stdout)
    return tuple(dict.fromkeys(servers))


def _parse_client(raw: Any) -> SipClientConfig:
    """Parse one raw client mapping into a typed client config.

    :param raw: Raw YAML client value.
    :returns: Parsed SIP client configuration.
    :raises ConfigError: If required client fields are absent or malformed.
    """

    if not isinstance(raw, dict):
        raise ConfigError("Each client entry must be a mapping")
    required = ["name", "id_uri", "registrar_uri", "username_env", "password_env"]
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ConfigError(f"Client entry is missing required field(s): {', '.join(missing)}")
    local_port = raw.get("local_port")
    if local_port is not None and not isinstance(local_port, int):
        raise ConfigError(f"Client {raw['name']} local_port must be an integer")
    return SipClientConfig(
        name=str(raw["name"]),
        id_uri=str(raw["id_uri"]),
        registrar_uri=str(raw["registrar_uri"]),
        username_env=str(raw["username_env"]),
        password_env=str(raw["password_env"]),
        proxy_uri=str(raw["proxy_uri"]) if raw.get("proxy_uri") else None,
        transport=str(raw.get("transport", "tls")).lower(),
        enable_video=bool(raw.get("enable_video", False)),
        local_port=local_port,
    )


def _parse_target(raw: Any) -> TargetConfig:
    """Parse one raw target mapping into a typed target config.

    :param raw: Raw YAML target value.
    :returns: Parsed target configuration.
    :raises ConfigError: If the target entry is malformed.
    """

    if not isinstance(raw, dict):
        raise ConfigError("Each target entry must be a mapping")
    if not raw.get("name") or not raw.get("uri"):
        raise ConfigError("Target entries require name and uri")
    return TargetConfig(name=str(raw["name"]), uri=str(raw["uri"]), kind=str(raw.get("kind", "sip")))


def _required_list(raw: dict[str, Any], key: str) -> list[Any]:
    """Return a required non-empty list from a configuration mapping.

    :param raw: Raw configuration mapping.
    :param key: Field name to read.
    :returns: Required list value.
    :raises ConfigError: If the field is absent, empty, or not a list.
    """

    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"Configuration field {key!r} must be a non-empty list")
    return value


def _optional_string_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    """Return an optional list field as stripped strings.

    :param raw: Raw configuration mapping.
    :param key: Field name to read.
    :returns: Parsed tuple of non-empty strings.
    :raises ConfigError: If the field is not a list or contains empty values.
    """

    value = raw.get(key, [])
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"Configuration field {key!r} must be a list")
    parsed = tuple(str(item).strip() for item in value)
    if any(not item for item in parsed):
        raise ConfigError(f"Configuration field {key!r} cannot contain empty values")
    return parsed


def _ensure_unique(kind: str, values: list[str]) -> None:
    """Validate that logical names are unique.

    :param kind: Human-readable value category for error messages.
    :param values: Values to inspect for duplicates.
    :returns: None.
    :raises ConfigError: If duplicate values are present.
    """

    seen: set[str] = set()
    duplicates = sorted({value for value in values if value in seen or seen.add(value)})
    if duplicates:
        raise ConfigError(f"Duplicate {kind} name(s): {', '.join(duplicates)}")


def _strip_env_quotes(value: str) -> str:
    """Remove one matching pair of shell-style quotes from a dotenv value.

    :param value: Raw dotenv value.
    :returns: Unquoted value when quotes match, otherwise the original value.
    """

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
