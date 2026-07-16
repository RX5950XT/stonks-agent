from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stonks_agent.entrypoints.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("module", "arguments"),
    [
        (
            "stonks_agent.entrypoints.cli_commands.data.create_engine",
            ["data", "request-snapshot", "--database-url", "postgresql://blocked"],
        ),
        (
            "stonks_agent.entrypoints.cli_commands.research.create_engine",
            [
                "research",
                "request",
                "--snapshot-id",
                "39000000-0000-4000-8000-000000000002",
                "--database-url",
                "postgresql://blocked",
            ],
        ),
        (
            "stonks_agent.entrypoints.cli_commands.strategy.create_engine",
            [
                "strategy",
                "show",
                "--strategy-id",
                "kronos-return",
                "--strategy-version",
                "1.0.0",
                "--database-url",
                "postgresql://blocked",
            ],
        ),
        (
            "stonks_agent.entrypoints.cli_commands.operations.create_engine",
            [
                "paper",
                "status",
                "--scope",
                "account",
                "--account-id",
                "paper-main",
                "--database-url",
                "postgresql://blocked",
            ],
        ),
    ],
)
@pytest.mark.parametrize("environment", [None, "staging", "production", "preview"])
def test_database_cli_denies_nonlocal_environment_before_database_connection(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    arguments: list[str],
    environment: str | None,
) -> None:
    calls: list[str] = []

    def blocked_engine(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("database")
        raise AssertionError("database must not be reached")

    monkeypatch.setattr(module, blocked_engine)
    variables = {} if environment is None else {"STONKS_ENVIRONMENT": environment}

    result = runner.invoke(app, arguments, env=variables)

    assert result.exit_code != 0
    assert calls == []
    assert "local database CLI is unavailable" in result.output
    assert "postgresql://blocked" not in result.output
    assert "traceback" not in result.output.lower()


@pytest.mark.parametrize("environment", ["local", "development", "test"])
def test_local_cli_environment_boundary_allows_explicit_local_values(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    called: list[str] = []

    class Engine:
        def dispose(self) -> None:
            called.append("disposed")

    def engine(database_url: str, *, pool_pre_ping: bool) -> Engine:
        assert database_url == "postgresql://allowed"
        assert pool_pre_ping is True
        called.append("connected")
        return Engine()

    def operation(*args: object, **kwargs: object) -> object:
        del args, kwargs
        called.append("operation")
        raise RuntimeError("stop after boundary")

    monkeypatch.setattr(
        "stonks_agent.entrypoints.cli_commands.data.create_engine",
        engine,
    )
    monkeypatch.setattr(
        "stonks_agent.entrypoints.cli_commands.data.request_snapshot",
        operation,
    )

    result = runner.invoke(
        app,
        ["data", "request-snapshot", "--database-url", "postgresql://allowed"],
        env={"STONKS_ENVIRONMENT": environment},
    )

    assert result.exit_code != 0
    assert called == ["connected", "operation", "disposed"]
