from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import InstrumentRow
from stonks_agent.adapters.postgres.trading_repository import (
    PostgresTradingRepository,
)
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalPosting, JournalSide, JournalTransaction
from stonks_agent.domain.orders import (
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    append_order_event,
    create_order_intent,
)
from stonks_agent.domain.portfolio import (
    AccountPortfolioSnapshot,
    CashBalance,
    PortfolioTarget,
    TargetAllocation,
)
from stonks_agent.domain.reservations import (
    ReservationKind,
    ReservationMutation,
    create_reservation,
)
from stonks_agent.domain.risk import RiskCheck, RiskDecision
from stonks_agent.ports.trading_repository import TradingRepositoryPort

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 13, 10, tzinfo=UTC)
ACCOUNT_ID = "paper-account-p4"
INSTRUMENT_ID = UUID("42000000-0000-4000-8000-000000000001")
SNAPSHOT_ID = UUID("42000000-0000-4000-8000-000000000002")
TARGET_ID = UUID("42000000-0000-4000-8000-000000000003")
DECISION_ID = UUID("42000000-0000-4000-8000-000000000004")
RESERVATION_ID = UUID("42000000-0000-4000-8000-000000000005")
INTENT_ID = UUID("42000000-0000-4000-8000-000000000006")
FILL_ID = UUID("42000000-0000-4000-8000-000000000007")
COMMAND_ID = UUID("42000000-0000-4000-8000-000000000008")
HASH_A = "a" * 64
HASH_B = "b" * 64


def account_snapshot() -> AccountPortfolioSnapshot:
    return AccountPortfolioSnapshot(
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        ledger_hash=None,
        cash=(
            CashBalance(
                currency="USD",
                settled_amount=Decimal("10000.00"),
                reserved_amount=Decimal("0.00"),
                quantum=Decimal("0.01"),
            ),
        ),
    )


def target(
    *,
    target_id: UUID = TARGET_ID,
    account_id: str = ACCOUNT_ID,
    expected_cost: Decimal = Decimal("5.00"),
) -> PortfolioTarget:
    return PortfolioTarget.create(
        target_id=target_id,
        account_id=account_id,
        portfolio_snapshot_id=SNAPSHOT_ID,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        as_of=NOW,
        allocations=(
            TargetAllocation(
                instrument_id=INSTRUMENT_ID,
                current_quantity=Decimal("0"),
                target_quantity=Decimal("4"),
                delta_quantity=Decimal("4"),
                quantity_quantum=Decimal("1"),
                target_weight=Decimal("0.04"),
            ),
        ),
        input_signal_ids=(UUID("42000000-0000-4000-8000-000000000009"),),
        policy_version="portfolio-v1",
        policy_hash=HASH_A,
        expected_turnover=Decimal("0.04"),
        expected_cost=expected_cost,
        cost_currency="USD",
    )


def decision(
    *,
    target_value: PortfolioTarget | None = None,
    decision_id: UUID = DECISION_ID,
) -> RiskDecision:
    selected_target = target_value or target()
    return RiskDecision.create(
        decision_id=decision_id,
        target=selected_target,
        approved=True,
        normalized_target=selected_target,
        checks=(RiskCheck(code="cash_available", passed=True),),
        policy_version="risk-v1",
        policy_hash=HASH_B,
        decided_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )


def reservation_order(
    *,
    reservation_id: UUID = RESERVATION_ID,
    intent_id: UUID = INTENT_ID,
    idempotency_key: str = "paper-account-p4:order-1",
    reserved_amount: Decimal = Decimal("405.00"),
) -> tuple[ReservationMutation, OrderIntent]:
    reserved = create_reservation(
        reservation_id=reservation_id,
        order_intent_id=intent_id,
        decision=decision(),
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=reserved_amount,
        quantum=Decimal("0.01"),
        instrument_id=INSTRUMENT_ID,
        at=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=4),
        current_account_sequence=0,
        current_portfolio_sequence=0,
    )
    assert isinstance(reserved, Success)
    order = create_order_intent(
        intent_id=intent_id,
        run_id=UUID("42000000-0000-4000-8000-000000000010"),
        decision=decision(),
        reservation=reserved.value.reservation,
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("4"),
        quantity_quantum=Decimal("1"),
        limit_price=Decimal("100.00"),
        stop_price=None,
        time_in_force=TimeInForce.DAY,
        valid_from=NOW + timedelta(seconds=2),
        valid_until=NOW + timedelta(minutes=4),
        idempotency_key=idempotency_key,
        execution_model_version="paper-v1",
        created_at=NOW + timedelta(seconds=2),
    )
    assert isinstance(order, Success)
    return reserved.value, order.value


def paper_fill() -> Fill:
    return Fill(
        fill_id=FILL_ID,
        command_id=COMMAND_ID,
        order_intent_id=INTENT_ID,
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        side=OrderSide.BUY,
        quantity=Decimal("4"),
        quantity_quantum=Decimal("1"),
        price=Decimal("100.00"),
        price_quantum=Decimal("0.01"),
        fee_currency="USD",
        fees=Decimal("1.00"),
        slippage=Decimal("0.01"),
        occurred_at=NOW + timedelta(minutes=2),
    )


def balanced_journal() -> JournalTransaction:
    return JournalTransaction.create(
        transaction_id=UUID("42000000-0000-4000-8000-000000000013"),
        account_id=ACCOUNT_ID,
        sequence=1,
        occurred_at=NOW + timedelta(minutes=2),
        previous_hash=None,
        source_order_intent_id=INTENT_ID,
        source_fill_id=FILL_ID,
        postings=(
            JournalPosting(
                posting_id=UUID("42000000-0000-4000-8000-000000000014"),
                ledger_account="asset:cash:USD",
                commodity="USD",
                side=JournalSide.DEBIT,
                amount=Decimal("401.00"),
                quantum=Decimal("0.01"),
            ),
            JournalPosting(
                posting_id=UUID("42000000-0000-4000-8000-000000000015"),
                ledger_account="clearing:paper:USD",
                commodity="USD",
                side=JournalSide.CREDIT,
                amount=Decimal("401.00"),
                quantum=Decimal("0.01"),
            ),
        ),
    )


def seed(repository: PostgresTradingRepository, session: Session) -> None:
    session.add(
        InstrumentRow(
            instrument_id=INSTRUMENT_ID,
            asset_class="equity",
            primary_symbol="P4TEST",
            exchange_mic="XNAS",
            currency="USD",
            timezone="America/New_York",
            valid_from=NOW,
            valid_to=None,
            version=1,
            created_at=NOW,
        )
    )
    session.flush()
    assert isinstance(
        repository.register_account(account_snapshot(), base_currency="USD"), Success
    )
    assert isinstance(repository.save_target(target()), Success)
    assert isinstance(repository.save_risk_decision(decision()), Success)


def test_atomic_reservation_order_is_idempotent_and_updates_available_cash(
    clean_database: Engine,
) -> None:
    mutation, order = reservation_order()
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        created = repository.create_reservation_order(mutation, order)
        replayed = repository.create_reservation_order(mutation, order)
        account = repository.get_account(ACCOUNT_ID)
        session.commit()

    assert isinstance(created, Success)
    assert isinstance(replayed, Success)
    assert created.value == replayed.value
    assert isinstance(account, Success)
    assert account.value.account_aggregate_sequence == 1
    assert account.value.cash[0].reserved_amount == Decimal("405.00")
    assert account.value.cash[0].available_amount == Decimal("9595.00")
    assert len(account.value.events) == 1
    assert account.value.events[0].aggregate_ref_type == "reservation_order"


def test_order_idempotency_key_with_different_payload_fails_closed(
    clean_database: Engine,
) -> None:
    mutation, order = reservation_order()
    other_mutation, other_order = reservation_order(
        reservation_id=UUID("42000000-0000-4000-8000-000000000011"),
        intent_id=UUID("42000000-0000-4000-8000-000000000012"),
    )
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        assert isinstance(repository.create_reservation_order(mutation, order), Success)
        conflict = repository.create_reservation_order(other_mutation, other_order)

    assert isinstance(conflict, Failure)
    assert conflict.error.code is ErrorCode.CONFLICT


def test_concurrent_account_cas_allows_only_one_reservation_order(
    clean_database: Engine,
) -> None:
    with Session(clean_database) as session:
        seed(PostgresTradingRepository(session), session)
        session.commit()
    barrier = Barrier(2)
    pairs = (
        reservation_order(),
        reservation_order(
            reservation_id=UUID("42000000-0000-4000-8000-000000000011"),
            intent_id=UUID("42000000-0000-4000-8000-000000000012"),
            idempotency_key="paper-account-p4:order-2",
        ),
    )

    def reserve(pair: tuple[ReservationMutation, OrderIntent]) -> object:
        mutation, order = pair
        with Session(clean_database) as session:
            repository = PostgresTradingRepository(session)
            barrier.wait()
            result = repository.create_reservation_order(mutation, order)
            if isinstance(result, Success):
                session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, pairs))

    assert sum(isinstance(item, Success) for item in results) == 1
    assert (
        sum(
            isinstance(item, Failure) and item.error.code is ErrorCode.CONFLICT
            for item in results
        )
        == 1
    )


def test_registration_authorization_and_empty_reads_are_idempotent(
    clean_database: Engine,
) -> None:
    changed_target = target(expected_cost=Decimal("6.00"))
    missing_target = target(
        target_id=UUID("42000000-0000-4000-8000-000000000016"),
        account_id="missing-account",
    )
    unsaved_target = target(target_id=UUID("42000000-0000-4000-8000-000000000017"))
    unsaved_decision = decision(
        target_value=unsaved_target,
        decision_id=UUID("42000000-0000-4000-8000-000000000018"),
    )
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        account_replay = repository.register_account(
            account_snapshot(), base_currency="USD"
        )
        account_conflict = repository.register_account(
            account_snapshot(), base_currency="EUR"
        )
        target_replay = repository.save_target(target())
        target_conflict = repository.save_target(changed_target)
        risk_replay = repository.save_risk_decision(decision())
        missing_account = repository.get_account("missing-account")
        missing_target_result = repository.save_target(missing_target)
        missing_risk_result = repository.save_risk_decision(unsaved_decision)
        events = repository.list_order_events(INTENT_ID)
        journal = repository.list_journal(ACCOUNT_ID)

    assert isinstance(account_replay, Success)
    assert isinstance(target_replay, Success)
    assert isinstance(risk_replay, Success)
    for result in (account_conflict, target_conflict):
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CONFLICT
    for result in (missing_account, missing_target_result, missing_risk_result):
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.NOT_FOUND
    assert isinstance(events, Success) and events.value == ()
    assert isinstance(journal, Success) and journal.value == ()


def test_insufficient_cash_rolls_back_account_cas(clean_database: Engine) -> None:
    mutation, order = reservation_order(reserved_amount=Decimal("10000.01"))
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        rejected = repository.create_reservation_order(mutation, order)
        account = repository.get_account(ACCOUNT_ID)
        session.commit()

    assert isinstance(rejected, Failure)
    assert rejected.error.code is ErrorCode.CONFLICT
    assert isinstance(account, Success)
    assert account.value.account_aggregate_sequence == 0
    assert account.value.cash[0].reserved_amount == Decimal("0.00")
    assert account.value.events == ()


def test_missing_or_mismatched_execution_sources_fail_closed(
    clean_database: Engine,
) -> None:
    mutation, order = reservation_order()
    accepted = append_order_event(
        order,
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=1),
    )
    assert isinstance(accepted, Success)
    mismatched_order = order.model_copy(
        update={"reservation_id": UUID("42000000-0000-4000-8000-000000000019")}
    )
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        missing_event = repository.append_order_event(accepted.value)
        missing_fill = repository.save_fill(paper_fill())
        missing_journal = repository.append_journal(
            balanced_journal(), expected_account_sequence=0
        )
        mismatched = repository.create_reservation_order(mutation, mismatched_order)
        account = repository.get_account(ACCOUNT_ID)

    for result in (missing_event, missing_fill, missing_journal):
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.NOT_FOUND
    assert isinstance(mismatched, Failure)
    assert mismatched.error.code is ErrorCode.CONFLICT
    assert isinstance(account, Success)
    assert account.value.account_aggregate_sequence == 0


def test_corrupt_persisted_payload_returns_structured_conflict(
    clean_database: Engine,
) -> None:
    corrupt_id = UUID("42000000-0000-4000-8000-000000000020")
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        session.execute(
            text(
                """
                insert into portfolio_target
                    (target_id, account_id, portfolio_snapshot_id,
                     account_aggregate_sequence, portfolio_sequence,
                     calculation_hash, policy_hash, payload, created_at)
                values
                    (:target_id, :account_id, :snapshot_id, 0, 0,
                     :calculation_hash, :policy_hash, '{}'::jsonb, :created_at)
                """
            ),
            {
                "target_id": corrupt_id,
                "account_id": ACCOUNT_ID,
                "snapshot_id": SNAPSHOT_ID,
                "calculation_hash": "c" * 64,
                "policy_hash": HASH_A,
                "created_at": NOW,
            },
        )
        result = repository.save_target(target(target_id=corrupt_id))

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT


def test_order_event_fill_and_balanced_journal_are_append_only(
    clean_database: Engine,
) -> None:
    mutation, order = reservation_order()
    accepted = append_order_event(
        order,
        previous=None,
        target_status=OrderStatus.ACCEPTED,
        cumulative_filled_quantity=Decimal("0"),
        occurred_at=NOW + timedelta(minutes=1),
    )
    assert isinstance(accepted, Success)
    filled_event = append_order_event(
        order,
        previous=accepted.value,
        target_status=OrderStatus.FILLED,
        cumulative_filled_quantity=Decimal("4"),
        occurred_at=NOW + timedelta(minutes=2),
    )
    assert isinstance(filled_event, Success)
    fill = paper_fill()
    journal = balanced_journal()
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        assert isinstance(repository.create_reservation_order(mutation, order), Success)
        assert isinstance(repository.append_order_event(accepted.value), Success)
        assert isinstance(repository.append_order_event(accepted.value), Success)
        assert isinstance(repository.append_order_event(filled_event.value), Success)
        assert isinstance(repository.save_fill(fill), Success)
        assert isinstance(repository.save_fill(fill), Success)
        posted = repository.append_journal(journal, expected_account_sequence=1)
        posted_replay = repository.append_journal(journal, expected_account_sequence=1)
        session.commit()
        events = repository.list_order_events(INTENT_ID)
        transactions = repository.list_journal(ACCOUNT_ID)

    assert isinstance(posted, Success)
    assert isinstance(posted_replay, Success)
    assert isinstance(events, Success)
    assert [item.sequence for item in events.value] == [1, 2]
    assert isinstance(transactions, Success)
    assert transactions.value == (journal,)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text("update order_event set reason = 'tampered' where event_id = :id"),
            {"id": accepted.value.event_id},
        )


def test_worker_has_no_trading_table_credentials(clean_database: Engine) -> None:
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        clean_database.begin() as connection,
    ):
        connection.execute(text("set local role stonks_worker"))
        connection.execute(text("select * from paper_account"))


def test_unit_of_work_exposes_trading_repository(clean_database: Engine) -> None:
    with PostgresUnitOfWork(clean_database) as unit_of_work:
        assert isinstance(unit_of_work.trading, PostgresTradingRepository)
        assert isinstance(unit_of_work.trading, TradingRepositoryPort)
