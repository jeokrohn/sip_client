from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

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


def test_load_config_falls_back_to_scutil_nameservers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def fake_run(args: list[str], *, capture_output: bool, text: bool) -> CompletedProcess[str]:
        assert args == ["scutil", "--dns"]
        assert capture_output
        assert text
        return CompletedProcess(
            args=args,
            returncode=0,
            stdout="""
resolver #1
  nameserver[0] : 192.0.2.53
  nameserver[1] : 2001:db8::53
resolver #2
  nameserver[0] : 192.0.2.53
""",
        )

    monkeypatch.setattr("wxcalls.config.subprocess.run", fake_run)

    config = load_config(config_path, env_file=None)

    assert config.dns_nameservers == ("192.0.2.53", "2001:db8::53")


def test_load_config_prefers_configured_nameservers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
dns_nameservers:
  - 192.0.2.54
clients:
  - name: alice
    id_uri: sip:alice@example.invalid
    registrar_uri: sip:registrar.example.invalid
    username_env: ALICE_USER
    password_env: ALICE_PASS
""",
        encoding="utf-8",
    )

    def fail_run(*args: object, **kwargs: object) -> None:
        pytest.fail("scutil should not be called when nameservers are configured")

    monkeypatch.setattr("wxcalls.config.subprocess.run", fail_run)

    config = load_config(config_path, env_file=None)

    assert config.dns_nameservers == ("192.0.2.54",)


def test_missing_credentials_raise_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("wxcalls.config.get_dns_from_scutil", lambda: ())

    config = load_config(config_path, env_file=None)

    with pytest.raises(ConfigError, match="ALICE_USER"):
        config.validate_credentials()


def test_load_dotenv_rejects_malformed_line(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("NOT_VALID\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid .env line"):
        load_dotenv(env_path)
