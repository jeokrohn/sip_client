"""Configuration loading for SIP clients, targets, and local secrets."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

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
    """Configuration for one SIP phone identity or synthetic local gateway.

    :param name: Stable logical name used by scenarios.
    :param id_uri: SIP identity URI used in the account ``From`` header.
    :param registrar_uri: SIP registrar URI.
    :param username_env: Environment variable containing the SIP username.
    :param password_env: Environment variable containing the SIP password.
    :param extension: Optional numeric extension owned by this client.
    :param proxy_uri: Optional outbound proxy or route URI.
    :param transport: Preferred transport label, typically ``tls``, ``tcp``, or ``udp``.
    :param enable_video: Whether video may be offered for video smoke tests.
    :param local_port: Optional local SIP signaling port override.
    :param kind: SIP endpoint type, either ``sip`` or ``local_gateway``.
    :param registrar_domain: Webex Calling registrar domain for an LGW.
    :param trunk_group: Trunk OTG/DTG value appended to the LGW identity.
    :param line_port: Line/Port value used as the LGW registration identity.
    :param outbound_proxy: Control Hub outbound proxy address for an LGW.
    """

    name: str
    id_uri: str
    registrar_uri: str
    username_env: str
    password_env: str
    extension: str | None = None
    proxy_uri: str | None = None
    transport: str = "tls"
    enable_video: bool = False
    local_port: int | None = None
    kind: str = "sip"
    registrar_domain: str | None = None
    trunk_group: str | None = None
    line_port: str | None = None
    outbound_proxy: str | None = None

    @property
    def is_local_gateway(self) -> bool:
        """Return whether this client uses registration-based LGW settings.

        :returns: ``True`` for the ``local_gateway`` client kind.
        """

        return self.kind == "local_gateway"

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

    def client_for_extension(self, extension: str) -> SipClientConfig | None:
        """Return the client that owns an extension.

        :param extension: Numeric extension to resolve.
        :returns: Matching SIP client, if one owns the extension.
        """

        normalized = extension.strip()
        for client in self.clients:
            if client.extension == normalized:
                return client
        return None

    def client_name_for_extension_uri(self, uri: str) -> str | None:
        """Resolve an extension SIP URI back to a configured client name.

        :param uri: SIP URI such as ``sip:7108@registrar.example.invalid``.
        :returns: Matching client name, if the URI belongs to an owned extension.
        """

        user, host = _sip_uri_user_host(uri)
        if user is None or host is None:
            return None
        client = self.client_for_extension(user)
        if client is None:
            return None
        registrar_host = _sip_uri_host(client.registrar_uri)
        if registrar_host is None or host.lower() != registrar_host.lower():
            return None
        return client.name

    def validate_credentials(self) -> None:
        """Validate that all configured client credentials can be resolved.

        :returns: None.
        :raises ConfigError: If any client secret is missing.
        """

        for client in self.clients:
            client.credentials(self.env)


class _RawSipClientConfig(BaseModel):
    """Pydantic model for a SIP or Local Gateway entry loaded from YAML.

    :param name: Stable logical name used by scenarios.
    :param id_uri: SIP identity URI for generic SIP clients.
    :param registrar_uri: SIP registrar URI for generic SIP clients.
    :param username_env: Environment variable containing the SIP username.
    :param password_env: Environment variable containing the SIP password.
    :param extension: Optional numeric extension owned by this client.
    :param proxy_uri: Optional outbound proxy or route URI.
    :param transport: Preferred transport label.
    :param enable_video: Whether video may be offered for video smoke tests.
    :param local_port: Optional local SIP signaling port override.
    :param kind: Endpoint type, either ``sip`` or ``local_gateway``.
    :param registrar_domain: Control Hub Registrar Domain for an LGW.
    :param trunk_group: Control Hub Trunk Group OTG/DTG value.
    :param line_port: Control Hub Line/Port value.
    :param outbound_proxy: Control Hub outbound proxy address.
    """

    name: str
    id_uri: str | None = None
    registrar_uri: str | None = None
    username_env: str
    password_env: str
    extension: str | int | None = None
    proxy_uri: str | None = None
    transport: str = "tls"
    enable_video: bool = False
    local_port: int | None = None
    kind: str = "sip"
    registrar_domain: str | None = None
    trunk_group: str | None = None
    line_port: str | None = None
    outbound_proxy: str | None = None

    @field_validator("name", "username_env", "password_env")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        """Validate and normalize a required client string.

        :param value: Candidate string value.
        :returns: Stripped non-empty string.
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be empty")
        return stripped

    @field_validator("proxy_uri")
    @classmethod
    def _optional_proxy(cls, value: str | None) -> str | None:
        """Normalize an optional proxy URI.

        :param value: Candidate proxy URI.
        :returns: Stripped proxy URI, or ``None`` when empty.
        """

        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("id_uri", "registrar_uri")
    @classmethod
    def _optional_uri(cls, value: str | None) -> str | None:
        """Normalize optional generic SIP account URIs.

        :param value: Candidate SIP URI.
        :returns: Stripped URI, or ``None`` when absent.
        """

        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("kind")
    @classmethod
    def _supported_client_kind(cls, value: str) -> str:
        """Normalize and validate the configured endpoint type.

        :param value: Candidate endpoint type.
        :returns: Normalized supported endpoint type.
        :raises ValueError: If the endpoint type is unsupported.
        """

        kind = value.strip().lower()
        if kind not in {"sip", "local_gateway"}:
            raise ValueError("kind must be 'sip' or 'local_gateway'")
        return kind

    @field_validator("registrar_domain", "trunk_group", "line_port", "outbound_proxy")
    @classmethod
    def _optional_lgw_string(cls, value: str | None) -> str | None:
        """Normalize an optional LGW value.

        :param value: Candidate LGW setting.
        :returns: Stripped value, or ``None`` when absent.
        """

        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("extension", mode="before")
    @classmethod
    def _optional_numeric_extension(cls, value: str | int | None) -> str | None:
        """Normalize and validate an optional client extension.

        :param value: Candidate extension value.
        :returns: Stripped numeric extension, or ``None`` when absent.
        """

        if value is None:
            return None
        stripped = str(value).strip()
        if not stripped:
            raise ValueError("extension cannot be empty")
        if not stripped.isdecimal():
            raise ValueError("extension must contain only digits")
        return stripped

    @field_validator("transport")
    @classmethod
    def _lower_transport(cls, value: str) -> str:
        """Normalize the configured transport label.

        :param value: Candidate transport label.
        :returns: Lowercase transport label.
        """

        stripped = value.strip().lower()
        if not stripped:
            raise ValueError("transport cannot be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_endpoint_fields(self) -> Self:
        """Validate fields required by each client kind.

        :returns: Validated raw SIP client configuration.
        :raises ValueError: If the selected kind has incomplete or conflicting fields.
        """

        if self.kind == "sip":
            if not self.id_uri or not self.registrar_uri:
                raise ValueError("id_uri and registrar_uri are required for kind 'sip'")
            return self

        required = {
            "registrar_domain": self.registrar_domain,
            "trunk_group": self.trunk_group,
            "line_port": self.line_port,
            "outbound_proxy": self.outbound_proxy,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"kind 'local_gateway' requires {', '.join(missing)}")
        if self.transport != "tls":
            raise ValueError("kind 'local_gateway' requires transport 'tls'")
        if self.proxy_uri:
            raise ValueError("use outbound_proxy instead of proxy_uri for kind 'local_gateway'")
        if self.extension or self.enable_video:
            raise ValueError("extension and enable_video are not supported for kind 'local_gateway'")
        if self.line_port and self.registrar_domain:
            _local_gateway_line_port_user(self.line_port, self.registrar_domain)
        return self

    def to_config(self) -> SipClientConfig:
        """Convert the raw model into its public SIP client configuration.

        Local Gateway entries synthesize the registrar URI and From identity from
        the separate Control Hub fields.

        :returns: SIP client configuration dataclass.
        """

        id_uri = self.id_uri
        registrar_uri = self.registrar_uri
        proxy_uri = self.proxy_uri
        if self.kind == "local_gateway":
            # CUBE uses Line/Port as the address-of-record user and OTG/DTG as a
            # URI parameter; Digest authentication uses the separate credentials.
            line_port_user = _local_gateway_line_port_user(self.line_port or "", self.registrar_domain or "")
            id_uri = f"sip:{line_port_user}@{self.registrar_domain};otg={self.trunk_group}"
            # IOS XE uses SIPS for TLS discovery, then its SIP profile rewrites
            # the REGISTER request URI to SIP while the TLS transport stays active.
            registrar_uri = f"sip:{self.registrar_domain}:5061"
            proxy_uri = _local_gateway_proxy_uri(self.outbound_proxy or "")

        return SipClientConfig(
            name=self.name,
            id_uri=id_uri or "",
            registrar_uri=registrar_uri or "",
            username_env=self.username_env,
            password_env=self.password_env,
            extension=self.extension,
            proxy_uri=proxy_uri,
            transport=self.transport,
            enable_video=self.enable_video,
            local_port=self.local_port,
            kind=self.kind,
            registrar_domain=self.registrar_domain,
            trunk_group=self.trunk_group,
            line_port=self.line_port,
            outbound_proxy=self.outbound_proxy,
        )


class _RawTargetConfig(BaseModel):
    """Pydantic model for one opaque target entry loaded from YAML.

    :param name: Stable logical name used by scenarios.
    :param uri: Dialable SIP URI or address.
    :param kind: Human-readable target type.
    """

    name: str
    uri: str
    kind: str = "sip"

    @field_validator("name", "uri", "kind")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        """Validate and normalize a target string.

        :param value: Candidate string value.
        :returns: Stripped non-empty string.
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be empty")
        return stripped

    def to_config(self) -> TargetConfig:
        """Convert the raw model into the public dataclass config.

        :returns: Target configuration dataclass.
        """

        return TargetConfig(name=self.name, uri=self.uri, kind=self.kind)


class _RawLabConfig(BaseModel):
    """Pydantic model for the raw YAML lab configuration.

    :param clients: SIP clients available to scenarios.
    :param targets: Opaque non-simulated call targets.
    :param artifacts_dir: Directory where run artifacts are written.
    :param dns_nameservers: Optional DNS resolver addresses.
    """

    clients: tuple[_RawSipClientConfig, ...] = Field(min_length=1)
    targets: tuple[_RawTargetConfig, ...] = ()
    artifacts_dir: Path = Path("artifacts")
    dns_nameservers: tuple[str, ...] = ()

    @field_validator("targets", mode="before")
    @classmethod
    def _normalize_targets(cls, value: object) -> object:
        """Normalize an empty targets field before tuple validation.

        :param value: Raw ``targets`` value from YAML.
        :returns: Normalized value for Pydantic tuple parsing.
        """

        if value is None:
            return ()
        return value

    @field_validator("dns_nameservers", mode="before")
    @classmethod
    def _normalize_nameservers(cls, value: object) -> object:
        """Normalize empty DNS fields before tuple validation.

        :param value: Raw ``dns_nameservers`` value from YAML.
        :returns: Normalized value for Pydantic tuple parsing.
        """

        if value in (None, ""):
            return ()
        return value

    @field_validator("dns_nameservers")
    @classmethod
    def _non_empty_nameservers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Validate and strip configured DNS nameservers.

        :param value: Parsed nameserver tuple.
        :returns: Tuple of stripped nameservers.
        """

        parsed = tuple(item.strip() for item in value)
        if any(not item for item in parsed):
            raise ValueError("dns_nameservers cannot contain empty values")
        return parsed

    @model_validator(mode="after")
    def _validate_unique_names(self) -> Self:
        """Validate uniqueness of logical client and target names.

        :returns: Validated raw lab configuration.
        """

        _ensure_unique("client", [client.name for client in self.clients])
        _ensure_unique("target", [target.name for target in self.targets])
        _ensure_unique_extensions([client.extension for client in self.clients])
        return self

    def to_config(self, env: dict[str, str], dns_nameservers: tuple[str, ...]) -> LabConfig:
        """Convert the raw model into the public lab config dataclass.

        :param env: Resolved local environment.
        :param dns_nameservers: Final DNS nameservers after fallback handling.
        :returns: Lab configuration dataclass.
        """

        return LabConfig(
            clients=tuple(client.to_config() for client in self.clients),
            targets=tuple(target.to_config() for target in self.targets),
            artifacts_dir=self.artifacts_dir,
            dns_nameservers=dns_nameservers,
            env=env,
        )


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

    # Pydantic owns raw YAML shape and type validation. The public config remains
    # dataclass-based so the rest of the application API stays stable.
    try:
        parsed = _RawLabConfig.model_validate(raw)
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration file {config_path}: {exc}") from exc

    # Explicit YAML nameservers win; an empty field falls back to the host resolver
    # configuration for Mac-first live PJSUA2 runs.
    dns_nameservers = parsed.dns_nameservers or get_dns_from_scutil()
    return parsed.to_config(env=env, dns_nameservers=dns_nameservers)


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


def _local_gateway_proxy_uri(address: str) -> str:
    """Convert a Control Hub outbound proxy value into a TLS route URI.

    :param address: Hostname, ``dns:`` address, or SIP URI from Control Hub.
    :returns: Proxy URI configured for PJSUA2.
    """

    value = address.strip()
    if value.lower().startswith("dns:"):
        value = value[4:]
    if value.lower().startswith("sips:"):
        uri = value
    elif value.lower().startswith("sip:"):
        uri = value
        if "transport=tls" not in uri.lower():
            uri += ";transport=tls"
    else:
        uri = f"sips:{value}"
    parameters = uri.split(";", 1)[1:] or [""]
    if not any(parameter.lower() == "lr" for parameter in parameters):
        uri += ";lr"
    return uri


def _local_gateway_line_port_user(line_port: str, registrar_domain: str) -> str:
    """Extract the Line/Port user from either a bare value or a Control Hub AOR.

    :param line_port: Line/Port user or ``user@registrar-domain`` value.
    :param registrar_domain: Configured Control Hub Registrar Domain.
    :returns: User component used in the SIP identity URI.
    :raises ValueError: If the Line/Port AOR is malformed or has another domain.
    """

    if "@" not in line_port:
        return line_port
    if line_port.count("@") != 1:
        raise ValueError("Line/Port must be a SIP user or one user@Registrar-Domain value")
    user, domain = line_port.rsplit("@", 1)
    if not user or not domain:
        raise ValueError("Line/Port AOR must include both a user and domain")
    if domain.lower().rstrip(".") != registrar_domain.lower().rstrip("."):
        raise ValueError("Line/Port AOR domain must match registrar_domain")
    return user


def _ensure_unique_extensions(values: list[str | None]) -> None:
    """Validate that configured client extensions are unique.

    :param values: Optional extension values to inspect for duplicates.
    :returns: None.
    :raises ConfigError: If duplicate extensions are present.
    """

    present = [value for value in values if value is not None]
    seen: set[str] = set()
    duplicates = sorted({value for value in present if value in seen or seen.add(value)})
    if duplicates:
        raise ConfigError(f"Duplicate client extension(s): {', '.join(duplicates)}")


def _strip_env_quotes(value: str) -> str:
    """Remove one matching pair of shell-style quotes from a dotenv value.

    :param value: Raw dotenv value.
    :returns: Unquoted value when quotes match, otherwise the original value.
    """

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _sip_uri_user_host(uri: str) -> tuple[str | None, str | None]:
    """Extract the user and host from a SIP URI.

    :param uri: Candidate SIP URI.
    :returns: ``(user, host)`` when both are present, otherwise ``(None, None)``.
    """

    stripped = uri.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        stripped = stripped[1:-1].strip()
    if ":" not in stripped or "@" not in stripped:
        return None, None
    scheme, rest = stripped.split(":", 1)
    if scheme.lower() not in {"sip", "sips"}:
        return None, None
    user, host_part = rest.split("@", 1)
    host = host_part.split(";", 1)[0].strip()
    if not user or not host:
        return None, None
    return user.strip(), host


def _sip_uri_host(uri: str) -> str | None:
    """Extract the host from a SIP or SIPS URI.

    :param uri: Candidate SIP URI.
    :returns: Host component, if available.
    """

    stripped = uri.strip()
    if ":" not in stripped:
        return None
    scheme, rest = stripped.split(":", 1)
    if scheme.lower() not in {"sip", "sips"}:
        return None
    host_part = rest.rsplit("@", 1)[-1]
    host = host_part.split(";", 1)[0].strip()
    return host or None
