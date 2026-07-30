from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from stonks_agent.entrypoints.gui_paper import bootstrap_account, paper_reader

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
pytest_plugins = ["integration.postgres.conftest"]

NOW = datetime(2026, 7, 29, 9, tzinfo=UTC)


def test_gui_paper_reader_projects_account_risk_safety_and_integrity_without_writes(
    clean_database: Engine,
) -> None:
    bootstrap_account(
        clean_database,
        account_id="paper-local",
        clock=lambda: NOW,
    )
    before = _mutation_counts(clean_database)

    capability = paper_reader(
        clean_database,
        account_id="paper-local",
        clock=lambda: NOW,
    )()

    assert capability.state == "ready"
    assert capability.portfolio is not None
    assert capability.portfolio.cash[0].available == 100_000
    assert capability.portfolio.position_count == 0
    assert capability.nav is not None
    assert capability.nav.state == "empty"
    assert capability.risk is not None
    assert capability.risk.state == "empty"
    assert capability.safety is not None
    assert capability.safety.state == "available"
    assert capability.safety.active is False
    assert capability.integrity is not None
    assert capability.integrity.state == "verified"
    assert _mutation_counts(clean_database) == before


def _mutation_counts(engine: Engine) -> tuple[int, ...]:
    tables = (
        "paper_account_event",
        "paper_operator_action",
        "portfolio_target",
        "risk_decision",
        "order_intent",
        "journal_transaction",
    )
    with engine.connect() as connection:
        return tuple(
            int(connection.scalar(text(f"select count(*) from {table}")) or 0)
            for table in tables
        )
