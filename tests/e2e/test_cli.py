from __future__ import annotations

import json

from typer.testing import CliRunner

from stonks_agent.entrypoints.cli import app

runner = CliRunner()


def test_fake_cycle_cli_returns_standard_success_envelope() -> None:
    result = runner.invoke(
        app,
        [
            "fake-cycle",
            "--symbol",
            "AAPL",
            "--as-of",
            "2026-01-02T21:00:00+00:00",
            "--idempotency-key",
            "cli-cycle",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["status"] == 200
    assert payload["error"] is None
    assert payload["metadata"] == {"execution_mode": "paper"}
    assert payload["data"]["symbol"] == "AAPL"
    assert payload["data"]["run_status"] == "completed"
    assert payload["data"]["fill_price"] == "101.00"
    assert len(payload["data"]["projection_hash"]) == 64


def test_fake_cycle_cli_rejects_naive_timestamp_without_stack_trace() -> None:
    result = runner.invoke(
        app,
        [
            "fake-cycle",
            "--as-of",
            "2026-01-02T21:00:00",
        ],
    )

    assert result.exit_code != 0
    assert "timezone" in result.output.lower()
    assert "traceback" not in result.output.lower()
