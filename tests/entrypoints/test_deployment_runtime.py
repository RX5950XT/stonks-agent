from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from stonks_agent.config.deployment import (
    DeploymentDatabaseSettings,
    DeploymentSettings,
    RuntimeDatabaseRoleSettings,
)
from stonks_agent.entrypoints import deployment
from stonks_agent.entrypoints.deployment import (
    DeploymentRuntimeError,
    app,
    create_runtime_engine,
    packaged_migration_head,
    provision_runtime_role,
)

ROOT = Path(__file__).resolve().parents[2]


def test_packaged_migration_head_is_single_and_exact() -> None:
    assert (
        packaged_migration_head(
            ROOT / "alembic.ini",
            script_location=ROOT / "migrations",
        )
        == "0018"
    )


def test_packaged_migration_head_rejects_missing_or_multiple_heads(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.ini"
    with pytest.raises(DeploymentRuntimeError):
        packaged_migration_head(missing, script_location=tmp_path / "migrations")


@pytest.mark.parametrize("command", ("serve", "migrate"))
def test_deployment_commands_fail_with_safe_envelope(command: str) -> None:
    result = CliRunner().invoke(
        app,
        [command],
        env={"STONKS_DEPLOYMENT_ROOT": "Z:/sensitive/internal/path"},
    )

    assert result.exit_code == 1
    assert result.exception is not None
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["status"] == 500
    assert payload["error"] == {
        "code": "internal_error",
        "message": "Internal server error",
        "details": {},
    }
    assert "Traceback" not in result.output
    assert "sensitive" not in result.output


def test_runtime_role_password_is_converted_to_scram_verifier_before_ddl(
    tmp_path: Path,
) -> None:
    password = "runtime-" + "bound-value"
    password_file = tmp_path / "runtime-password"
    password_file.write_text(password, encoding="utf-8")
    statements: list[tuple[object, object]] = []
    encrypted: list[tuple[bytes, bytes, bytes]] = []

    class PgConnection:
        def encrypt_password(
            self,
            value: bytes,
            role: bytes,
            algorithm: bytes,
        ) -> bytes:
            encrypted.append((value, role, algorithm))
            return b"SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVkLWtleQ==:c2VydmVyLWtleQ=="

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(
            self,
            statement: object,
            parameters: object = None,
        ) -> None:
            statements.append((statement, parameters))

        def fetchone(self) -> tuple[bool]:
            return (False,)

    class Driver:
        pgconn = PgConnection()

        def cursor(self) -> Cursor:
            return Cursor()

    class DbApiConnection:
        driver_connection = Driver()

    class Connection:
        connection = DbApiConnection()

    provision_runtime_role(
        cast(Any, Connection()),
        RuntimeDatabaseRoleSettings(
            login_name="stonks_runtime",
            password_file=password_file,
            owner_user="postgres",
        ),
    )

    assert all(password not in str(statement) for statement, _ in statements)
    assert encrypted == [
        (
            password.encode(),
            b"stonks_runtime",
            b"scram-sha-256",
        )
    ]
    assert any("SCRAM-SHA-256" in str(statement) for statement, _ in statements)


def test_runtime_engine_fixes_internal_transport_and_query_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_file = tmp_path / "runtime-password"
    password_file.write_text("runtime-bound-value", encoding="utf-8")
    observed: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(url: object, **kwargs: object) -> object:
        observed["url"] = url
        observed.update(kwargs)
        return sentinel

    monkeypatch.setattr(deployment, "create_engine", fake_create_engine)
    settings = DeploymentSettings(
        environment="production",
        execution_mode="paper",
        build_revision="deadbeef",
        deployment_root="/opt/stonks",
        server_host="0.0.0.0",
        server_port=8000,
        database=DeploymentDatabaseSettings(
            host="postgres",
            port=5432,
            name="stonks",
            user="stonks_runtime",
            password_file=password_file,
            connect_timeout_seconds=3,
            pool_size=4,
            max_overflow=2,
        ),
    )

    result = create_runtime_engine(settings)

    assert cast(object, result) is sentinel
    assert observed["hide_parameters"] is True
    assert observed["connect_args"] == {
        "application_name": "stonks-agent-core",
        "connect_timeout": 3,
        "options": "-c statement_timeout=30000 -c lock_timeout=5000",
        "sslmode": "disable",
    }
