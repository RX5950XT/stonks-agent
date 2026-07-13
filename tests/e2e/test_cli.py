from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.domain.artifact import ArtifactMetadata
from stonks_agent.domain.errors import Success
from stonks_agent.entrypoints.cli import app
from stonks_contracts.evidence import Sensitivity

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


def test_report_cli_reads_only_finalized_report_artifact(tmp_path: Path) -> None:
    report_id = UUID("39000000-0000-4000-8000-000000000001")
    store = LocalArtifactStore(tmp_path)
    stored = store.finalize(
        b"# report\n",
        metadata=ArtifactMetadata(
            media_type="text/markdown",
            license_tag="Apache-2.0",
            sensitivity=Sensitivity.INTERNAL,
            source="stonks-agent-report-renderer",
            attributes=(
                ("format", "markdown_full"),
                ("report_id", str(report_id)),
                ("template_version", "stonks-report-templates/1.0.0"),
            ),
        ),
        finalized_at=datetime(2026, 7, 13, 8, tzinfo=UTC),
    )
    assert isinstance(stored, Success)

    result = runner.invoke(
        app,
        [
            "report",
            "show",
            "--content-hash",
            stored.value.content_hash,
            "--artifact-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["report_id"] == str(report_id)
    assert payload["data"]["content"] == "# report\n"


def test_research_cli_requires_database_and_exposes_no_execution_command() -> None:
    help_result = runner.invoke(app, ["research", "--help"])
    request_result = runner.invoke(
        app,
        [
            "research",
            "request",
            "--snapshot-id",
            "39000000-0000-4000-8000-000000000002",
        ],
        env={"STONKS_DATABASE_URL": ""},
    )

    assert help_result.exit_code == 0
    assert "request" in help_result.output
    assert "events" in help_result.output
    assert "order" not in help_result.output.lower()
    assert request_result.exit_code != 0
    assert "STONKS_DATABASE_URL" in request_result.output
    assert "traceback" not in request_result.output.lower()
