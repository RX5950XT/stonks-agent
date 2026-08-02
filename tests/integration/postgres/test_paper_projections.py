from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from integration.postgres.test_paper_operations import _seed_pending_order
from integration.postgres.test_trading_persistence import (
    ACCOUNT_ID,
    INTENT_ID,
    TARGET_ID,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from stonks_agent.adapters.postgres.ledger_repository import PostgresLedgerRepository
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.monitoring.mark_to_market import mark_to_market
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
    record_nav_projection,
)
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.monitoring import MarkToMarketCommand, PortfolioValuation
from stonks_agent.entrypoints.cli import app as cli_app

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 14, 6, tzinfo=UTC)
VIEWER = LocalPrincipal(
    subject="viewer:postgres",
    roles=frozenset({Role.VIEWER}),
    targets=frozenset({AccessTarget(kind=ResourceKind.ACCOUNT, identifier=ACCOUNT_ID)}),
)
RUNNER = CliRunner()


def _current_valuation(engine: Engine) -> PortfolioValuation:
    with Session(engine) as session:
        ledger = PostgresLedgerRepository(session).get_projection(ACCOUNT_ID)
    assert isinstance(ledger, Success)
    result = mark_to_market(
        MarkToMarketCommand(
            valuation_id=UUID("76000000-0000-4000-8000-000000000001"),
            account_id=ACCOUNT_ID,
            base_currency="USD",
            as_of=NOW,
            ledger=ledger.value,
            marks=(),
            currency_quantum=Decimal("0.01"),
        )
    )
    assert isinstance(result, Success)
    return result.value


def test_current_portfolio_nav_and_risk_are_verified_read_models(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    valuation = _current_valuation(clean_database)
    saved = record_nav_projection(valuation, lambda: PostgresUnitOfWork(clean_database))
    assert isinstance(saved, Success)

    portfolio = read_portfolio_projection(
        VIEWER, ACCOUNT_ID, lambda: PostgresUnitOfWork(clean_database)
    )
    nav = read_nav_projection(
        VIEWER, ACCOUNT_ID, lambda: PostgresUnitOfWork(clean_database)
    )
    risk = read_risk_projection(
        VIEWER,
        ACCOUNT_ID,
        as_of=NOW,
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(portfolio, Success)
    assert portfolio.value.pending_order_ids == (INTENT_ID,)
    assert portfolio.value.latest_target_ref is not None
    assert portfolio.value.latest_target_ref.ref_id == TARGET_ID
    assert portfolio.value.cash[0].available_amount == Decimal("9595.00")
    assert isinstance(nav, Success)
    assert nav.value == valuation
    assert isinstance(risk, Success)
    assert not risk.value.currently_authorized


def test_latest_nav_fails_closed_after_ledger_moves(
    clean_database: Engine,
) -> None:
    from integration.postgres.test_paper_execution import (
        _broker,
        _ledger_policy,
        execution_request,
    )

    from stonks_agent.application.execution.execute import execute_reference_paper

    _seed_pending_order(clean_database)
    valuation = _current_valuation(clean_database)
    assert isinstance(
        record_nav_projection(valuation, lambda: PostgresUnitOfWork(clean_database)),
        Success,
    )
    executed = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(executed, Success)

    stale = read_nav_projection(
        VIEWER, ACCOUNT_ID, lambda: PostgresUnitOfWork(clean_database)
    )

    assert isinstance(stale, Failure)
    assert stale.error.code is ErrorCode.CONFLICT


def test_valuation_is_idempotent_and_database_append_only(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    valuation = _current_valuation(clean_database)
    first = record_nav_projection(valuation, lambda: PostgresUnitOfWork(clean_database))
    replay = record_nav_projection(
        valuation, lambda: PostgresUnitOfWork(clean_database)
    )

    assert first == replay
    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text("update paper_portfolio_valuation set base_currency='TWD'")
        )


def test_paper_cli_reads_portfolio_nav_and_risk_projections(
    clean_database: Engine,
) -> None:
    _seed_pending_order(clean_database)
    valuation = _current_valuation(clean_database)
    assert isinstance(
        record_nav_projection(valuation, lambda: PostgresUnitOfWork(clean_database)),
        Success,
    )
    database = clean_database.url.render_as_string(hide_password=False)

    portfolio = RUNNER.invoke(
        cli_app,
        ["paper", "portfolio", "--account-id", ACCOUNT_ID, "--database-url", database],
        env={"STONKS_ENVIRONMENT": "test"},
    )
    nav = RUNNER.invoke(
        cli_app,
        ["paper", "nav", "--account-id", ACCOUNT_ID, "--database-url", database],
        env={"STONKS_ENVIRONMENT": "test"},
    )
    risk = RUNNER.invoke(
        cli_app,
        ["paper", "risk", "--account-id", ACCOUNT_ID, "--database-url", database],
        env={"STONKS_ENVIRONMENT": "test"},
    )

    assert portfolio.exit_code == nav.exit_code == risk.exit_code == 0
    assert '"projection_hash"' in portfolio.stdout
    assert '"valuation_hash"' in nav.stdout
    assert '"decision_hash"' in risk.stdout
