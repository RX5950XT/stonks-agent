from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from integration.postgres.test_paper_execution import (
    _broker,
    _ledger_policy,
    _seed_order,
    execution_request,
)
from integration.postgres.test_trading_persistence import (
    ACCOUNT_ID,
    INSTRUMENT_ID,
    NOW,
    SNAPSHOT_ID,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.ledger_repository import PostgresLedgerRepository
from stonks_agent.adapters.postgres.trading_repository import PostgresTradingRepository
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.application.ledger.reconcile import reconcile_paper_account
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.execution_model import ExecutionBar, PaperExecutionRequest
from stonks_agent.domain.orders import (
    OrderSide,
    OrderType,
    TimeInForce,
    build_execution_command,
    create_order_intent,
)
from stonks_agent.domain.portfolio import PortfolioTarget, TargetAllocation
from stonks_agent.domain.reservations import ReservationKind, create_reservation
from stonks_agent.domain.risk import RiskCheck, RiskDecision

pytestmark = pytest.mark.postgres


def test_accounted_execution_replay_matches_database_projection(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    executed = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(executed, Success)

    with Session(clean_database) as session:
        ledger = PostgresLedgerRepository(session)
        opening = ledger.get_opening_snapshot(ACCOUNT_ID)
        projection = ledger.get_projection(ACCOUNT_ID)
        journals = ledger.list_transactions(ACCOUNT_ID)

    assert isinstance(opening, Success)
    assert isinstance(projection, Success)
    assert isinstance(journals, Success)
    assert len(journals.value) == 1
    assert projection.value.cash("USD") < Decimal("10000.00")
    assert projection.value.projection_hash != opening.value.snapshot_hash
    reconciled = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=execution_request().as_of,
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(reconciled, Success)
    assert reconciled.value.matched


def test_fill_without_journal_is_rejected_at_commit(clean_database: Engine) -> None:
    _seed_order(clean_database)
    candidate = execution_request()
    simulated = _broker().execute(candidate)
    assert isinstance(simulated, Success)

    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        persisted = repository.apply_paper_execution(
            candidate.command,
            simulated.value,
            expected_account_sequence=1,
        )
        assert isinstance(persisted, Success)
        with pytest.raises(DBAPIError, match="requires exact journal accounting"):
            session.commit()
        session.rollback()

    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from paper_fill")) == 0
        assert (
            connection.scalar(text("select count(*) from paper_execution_receipt")) == 0
        )


def test_buy_then_sell_replays_average_cost_fees_and_realized_pnl(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    bought = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(bought, Success)
    with Session(clean_database) as session:
        before = PostgresLedgerRepository(session).get_projection(ACCOUNT_ID)
    assert isinstance(before, Success)
    sell_request = _seed_sell_order(clean_database)

    sold = execute_reference_paper(
        sell_request,
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(sold, Success)
    assert sold.value.outcome.fill is not None
    sell_fill = sold.value.outcome.fill
    cost = before.value.inventory_value(INSTRUMENT_ID, "USD") / Decimal("2")
    with Session(clean_database) as session:
        after = PostgresLedgerRepository(session).get_projection(ACCOUNT_ID)
    assert isinstance(after, Success)
    assert after.value.position(INSTRUMENT_ID) == Decimal("2")
    assert after.value.inventory_value(INSTRUMENT_ID, "USD") == cost
    assert after.value.cash("USD") == (
        before.value.cash("USD") + sell_fill.quantity * sell_fill.price - sell_fill.fees
    )
    assert after.value.fees("USD") == before.value.fees("USD") + sell_fill.fees
    assert (
        after.value.realized_pnl("USD") == sell_fill.quantity * sell_fill.price - cost
    )


def test_projection_drift_rolls_back_reconcile_and_activates_global_kill_switch(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    executed = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(executed, Success)
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "alter table paper_ledger_account_projection disable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )
        connection.execute(
            text(
                """
                update paper_ledger_account_projection
                set debit_total=debit_total+0.01
                where account_id=:account_id and ledger_account='asset:cash:USD'
                """
            ),
            {"account_id": ACCOUNT_ID},
        )
        connection.execute(
            text(
                "alter table paper_ledger_account_projection enable trigger "
                "trg_paper_ledger_projection_mutation"
            )
        )

    reconciled = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=execution_request().as_of,
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(reconciled, Failure)
    with clean_database.connect() as connection:
        switch = (
            connection.execute(
                text(
                    "select active, reason_code, actor from paper_kill_switch "
                    "where scope='global'"
                )
            )
            .mappings()
            .one()
        )
    assert switch == {
        "active": True,
        "reason_code": "ledger_reconciliation_failed",
        "actor": "system:ledger_reconciliation",
    }

    blocked = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(blocked, Failure)


def test_unknown_persisted_order_state_activates_global_kill_switch(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    executed = execute_reference_paper(
        execution_request(),
        _broker(),
        _ledger_policy(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(executed, Success)
    with clean_database.begin() as connection:
        connection.execute(
            text("alter table order_event disable trigger trg_order_event_append_only")
        )
        connection.execute(
            text(
                """
                update order_event set to_status='accepted',
                    cumulative_filled_quantity=0
                where order_intent_id=:intent_id and sequence=2
                """
            ),
            {"intent_id": execution_request().command.intent.intent_id},
        )
        connection.execute(
            text("alter table order_event enable trigger trg_order_event_append_only")
        )

    reconciled = reconcile_paper_account(
        ACCOUNT_ID,
        as_of=execution_request().as_of,
        unit_of_work=lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(reconciled, Failure)
    with clean_database.connect() as connection:
        assert connection.scalar(
            text("select active from paper_kill_switch where scope='global'")
        )


def _seed_sell_order(engine: Engine) -> PaperExecutionRequest:
    target = _sell_target()
    decision = RiskDecision.create(
        decision_id=UUID("46000000-0000-4000-8000-000000000103"),
        target=target,
        approved=True,
        normalized_target=target,
        checks=(RiskCheck(code="sell_position", passed=True),),
        policy_version="risk-v1",
        policy_hash="b" * 64,
        decided_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=10),
    )
    reserved = create_reservation(
        reservation_id=UUID("46000000-0000-4000-8000-000000000104"),
        order_intent_id=UUID("46000000-0000-4000-8000-000000000105"),
        decision=decision,
        kind=ReservationKind.POSITION,
        commodity=str(INSTRUMENT_ID),
        amount=Decimal("2"),
        quantum=Decimal("1"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(minutes=2, seconds=1),
        expires_at=NOW + timedelta(minutes=9),
        current_account_sequence=2,
        current_portfolio_sequence=0,
    )
    assert isinstance(reserved, Success)
    order = create_order_intent(
        intent_id=reserved.value.reservation.order_intent_id,
        run_id=UUID("46000000-0000-4000-8000-000000000106"),
        decision=decision,
        reservation=reserved.value.reservation,
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
        quantity_quantum=Decimal("1"),
        limit_price=None,
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        valid_from=NOW + timedelta(minutes=2, seconds=1),
        valid_until=NOW + timedelta(minutes=9),
        idempotency_key=f"{ACCOUNT_ID}:sell-1",
        execution_model_version="paper-v1",
        created_at=NOW + timedelta(minutes=2, seconds=1),
    )
    assert isinstance(order, Success)
    with Session(engine) as session:
        repository = PostgresTradingRepository(session)
        assert isinstance(repository.save_target(target), Success)
        assert isinstance(repository.save_risk_decision(decision), Success)
        assert isinstance(
            repository.create_reservation_order(reserved.value, order.value), Success
        )
        session.commit()
    command = build_execution_command(
        command_id=UUID("46000000-0000-4000-8000-000000000107"),
        intent=order.value,
        decision=decision,
        reservation=reserved.value.reservation,
        current_account_sequence=3,
        current_portfolio_sequence=0,
        attempt_generation=1,
        attempt_nonce="sell-attempt",
        issued_at=NOW + timedelta(minutes=2, seconds=2),
    )
    assert isinstance(command, Success)
    return PaperExecutionRequest(
        command=command.value,
        reservation=reserved.value.reservation,
        prior_events=(),
        prior_fills=(),
        bars=(_sell_bar(),),
        as_of=NOW + timedelta(minutes=4),
    )


def _sell_target() -> PortfolioTarget:
    return PortfolioTarget.create(
        target_id=UUID("46000000-0000-4000-8000-000000000101"),
        account_id=ACCOUNT_ID,
        portfolio_snapshot_id=SNAPSHOT_ID,
        account_aggregate_sequence=2,
        portfolio_sequence=0,
        as_of=NOW + timedelta(minutes=2),
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT_ID,
                current_quantity=Decimal("4"),
                target_quantity=Decimal("2"),
                delta_quantity=Decimal("-2"),
                quantity_quantum=Decimal("1"),
                target_weight=Decimal("0.02"),
            ),
        ),
        input_signal_ids=(UUID("46000000-0000-4000-8000-000000000102"),),
        policy_version="portfolio-v1",
        policy_hash="a" * 64,
        expected_turnover=Decimal("0.02"),
        expected_cost=Decimal("1.00"),
        cost_currency="USD",
    )


def _sell_bar() -> ExecutionBar:
    opens_at = NOW + timedelta(minutes=3)
    return ExecutionBar(
        instrument_id=INSTRUMENT_ID,
        mic="XNAS",
        opens_at=opens_at,
        closes_at=opens_at + timedelta(seconds=30),
        available_at=opens_at + timedelta(seconds=30),
        open=Decimal("125.00"),
        high=Decimal("126.00"),
        low=Decimal("124.00"),
        close=Decimal("125.50"),
        volume=Decimal("100"),
        currency="USD",
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("1"),
        source_ref="sell-next-bar",
        source_hash="c" * 64,
        tradable=True,
    )
