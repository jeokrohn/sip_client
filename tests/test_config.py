from __future__ import annotations

from pathlib import Path

import pytest

from wxcalls.config import ConfigError, load_config, load_dotenv


def test_load_config_resolves_clients_and_targets(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
artifacts_dir: artifacts-test
dns_nameservers:
  - 192.0.2.53
clients:
  - name: alice
    id_uri: sip:alice@example.invalid
    registrar_uri: sip:registrar.example.invalid
    username_env: ALICE_USER
    password_env: ALICE_PASS
targets:
  - name: webex
    uri: sip:webex@example.invalid
""",
        encoding="utf-8",
    )
    env_path.write_text("ALICE_USER=alice\nALICE_PASS='secret'\n", encoding="utf-8")

    config = load_config(config_path, env_file=env_path)

    assert config.client("alice").credentials(config.env).username == "alice"
    assert config.client("alice").credentials(config.env).password == "secret"
    assert config.dns_nameservers == ("192.0.2.53",)
    assert config.resolve_target_uri("alice") == "sip:alice@example.invalid"
    assert config.resolve_target_uri("webex") == "sip:webex@example.invalid"
    assert config.resolve_target_uri("sip:literal@example.invalid") == "sip:literal@example.invalid"


def test_missing_credentials_raise_clear_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
clients:
  - name: alice
    id_uri: sip:alice@example.invalid
    registrar_uri: sip:registrar.example.invalid
    username_env: ALICE_USER
    password_env: ALICE_PASS
""",
        encoding="utf-8",
    )

    config = load_config(config_path, env_file=None)

    with pytest.raises(ConfigError, match="ALICE_USER"):
        config.validate_credentials()


def test_load_dotenv_rejects_malformed_line(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("NOT_VALID\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid .env line"):
        load_dotenv(env_path)
