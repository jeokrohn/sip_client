from __future__ import annotations

from pathlib import Path

from wxcalls.preflight import has_failures, run_preflight


def test_preflight_reports_missing_credentials_as_failure(tmp_path: Path) -> None:
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

    checks = run_preflight(config_path, env_file=None)

    assert has_failures(checks)
    assert any(check.name == "credentials" and check.status == "fail" for check in checks)
