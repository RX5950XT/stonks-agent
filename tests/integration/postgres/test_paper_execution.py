from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from integration.postgres.test_trading_persistence import (
    ACCOUNT_ID,
    COMMAND_ID,
    HASH_A,
    INSTRUMENT_ID,
    NOW,
    decision,
    reservation_order,
    seed,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from stonks_agent.adapters.execution.paper import (
    ReferencePaperBroker,
    load_paper_execution_policy,
)
from stonks_agent.adapters.postgres.trading_mapping import (
    account_event_row,
    new_account_event,
)
from stonks_agent.adapters.postgres.trading_repository import (
    PostgresTradingRepository,
)
from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.execution.execute import execute_reference_paper
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.execution_model import ExecutionBar, PaperExecutionRequest
from stonks_agent.domain.orders import build_execution_command

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[3]


def execution_request() -> PaperExecutionRequest:
    mutation, intent = reservation_order(reserved_amount=Decimal("405.00"))
    built = build_execution_command(
        command_id=COMMAND_ID,
        intent=intent,
        decision=decision(),
        reservation=mutation.reservation,
        current_account_sequence=1,
        current_portfolio_sequence=0,
        attempt_generation=1,
        attempt_nonce="postgres-paper-attempt",
        issued_at=NOW + timedelta(seconds=3),
    )
    assert isinstance(built, Success)
    opens_at = NOW + timedelta(minutes=1)
    bar = ExecutionBar(
        instrument_id=INSTRUMENT_ID,
        mic="XNAS",
        opens_at=opens_at,
        closes_at=opens_at + timedelta(seconds=30),
        available_at=opens_at + timedelta(seconds=30),
        open=Decimal("99.00"),
        high=Decimal("101.00"),
        low=Decimal("98.00"),
        close=Decimal("100.00"),
        volume=Decimal("100"),
        currency="USD",
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("1"),
        source_ref="postgres-paper-next-bar",
        source_hash=HASH_A,
        tradable=True,
    )
    return PaperExecutionRequest(
        command=built.value,
        reservation=mutation.reservation,
        prior_events=(),
        prior_fills=(),
        bars=(bar,),
        as_of=NOW + timedelta(minutes=2),
    )


def _broker() -> ReferencePaperBroker:
    return ReferencePaperBroker(
        load_paper_execution_policy(ROOT / "config" / "execution" / "paper_v1.yaml")
    )


def _seed_order(engine: Engine) -> None:
    mutation, intent = reservation_order(reserved_amount=Decimal("405.00"))
    with Session(engine) as session:
        repository = PostgresTradingRepository(session)
        seed(repository, session)
        assert isinstance(
            repository.create_reservation_order(mutation, intent), Success
        )
        session.commit()


def test_execution_receipt_fill_events_and_release_commit_atomically(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)

    result = execute_reference_paper(
        execution_request(),
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Success)
    assert result.value.outcome.receipt.status.value == "filled"
    assert result.value.outcome.fill is not None
    with clean_database.connect() as connection:
        counts = (
            connection.execute(
                text(
                    """
                select
                  (select count(*) from paper_execution_receipt) receipts,
                  (select count(*) from paper_fill) fills,
                  (select count(*) from order_event) order_events,
                  (select count(*) from reservation_event) reservation_events
                """
                )
            )
            .mappings()
            .one()
        )
        cash_reserved = connection.scalar(
            text(
                "select reserved_amount from paper_cash_projection "
                "where account_id=:account_id and currency='USD'"
            ),
            {"account_id": ACCOUNT_ID},
        )
        state = connection.scalar(
            text("select state from account_reservation where account_id=:account_id"),
            {"account_id": ACCOUNT_ID},
        )
    assert counts == {
        "receipts": 1,
        "fills": 1,
        "order_events": 2,
        "reservation_events": 3,
    }
    assert cash_reserved == Decimal("0.00")
    assert state == "released"


def test_execution_idempotency_replays_same_and_rejects_changed_command(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    first_request = execution_request()
    first = execute_reference_paper(
        first_request,
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    replay = execute_reference_paper(
        first_request,
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    changed_command = build_execution_command(
        command_id=UUID("42000000-0000-4000-8000-000000000099"),
        intent=first_request.command.intent,
        decision=decision(),
        reservation=first_request.reservation,
        current_account_sequence=1,
        current_portfolio_sequence=0,
        attempt_generation=2,
        attempt_nonce="changed-paper-attempt",
        issued_at=first_request.command.issued_at,
    )
    assert isinstance(changed_command, Success)
    changed = execute_reference_paper(
        first_request.model_copy(update={"command": changed_command.value}),
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(first, Success)
    assert isinstance(replay, Success)
    assert replay.value == first.value
    assert isinstance(changed, Failure)
    assert changed.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from paper_fill")) == 1
        assert connection.scalar(text("select count(*) from order_event")) == 2


def test_execution_sequence_drift_rolls_back_without_side_effects(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    with Session(clean_database) as session:
        session.execute(
            text(
                "update paper_account set aggregate_sequence=2 "
                "where account_id=:account_id"
            ),
            {"account_id": ACCOUNT_ID},
        )
        session.execute(
            text(
                "update paper_cash_projection set updated_sequence=2 "
                "where account_id=:account_id"
            ),
            {"account_id": ACCOUNT_ID},
        )
        updated_at = session.scalar(
            text("select updated_at from paper_account where account_id=:account_id"),
            {"account_id": ACCOUNT_ID},
        )
        previous_hash = session.scalar(
            text(
                "select event_hash from paper_account_event "
                "where account_id=:account_id and sequence=1"
            ),
            {"account_id": ACCOUNT_ID},
        )
        assert updated_at is not None
        session.add(
            account_event_row(
                new_account_event(
                    account_id=ACCOUNT_ID,
                    sequence=2,
                    event_type="test.sequence_drift",
                    aggregate_ref_type="test",
                    aggregate_ref_id=UUID("42000000-0000-4000-8000-000000000098"),
                    occurred_at=updated_at,
                    previous_hash=previous_hash,
                )
            )
        )
        session.commit()

    result = execute_reference_paper(
        execution_request(),
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from paper_fill")) == 0
        assert connection.scalar(text("select count(*) from order_event")) == 0
        assert (
            connection.scalar(text("select count(*) from paper_execution_receipt")) == 0
        )


def test_concurrent_duplicate_execution_commits_one_receipt_and_fill(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    submitted = execution_request()

    def execute() -> object:
        return execute_reference_paper(
            submitted,
            _broker(),
            lambda: PostgresUnitOfWork(clean_database),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: execute(), range(2)))

    assert all(isinstance(item, Success) for item in results)
    assert results[0] == results[1]
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from paper_fill")) == 1
        assert connection.scalar(text("select count(*) from order_event")) == 2
        assert (
            connection.scalar(text("select count(*) from paper_execution_receipt")) == 1
        )


def test_execution_receipt_is_append_only(clean_database: Engine) -> None:
    _seed_order(clean_database)
    result = execute_reference_paper(
        execution_request(),
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )
    assert isinstance(result, Success)

    with (
        pytest.raises(DBAPIError, match="append-only"),
        clean_database.begin() as connection,
    ):
        connection.execute(
            text("update paper_execution_receipt set command_hash=:hash"),
            {"hash": "f" * 64},
        )


def test_execution_queries_return_typed_not_found(clean_database: Engine) -> None:
    with Session(clean_database) as session:
        repository = PostgresTradingRepository(session)
        order = repository.get_order_by_idempotency(
            account_id="missing-account", idempotency_key="missing-order"
        )
        held = repository.get_reservation(UUID("42000000-0000-4000-8000-000000000097"))
        fills = repository.list_fills(UUID("42000000-0000-4000-8000-000000000097"))
        receipt = repository.get_execution_record(
            account_id="missing-account", idempotency_key="missing-order"
        )

    assert isinstance(order, Failure)
    assert order.error.code is ErrorCode.NOT_FOUND
    assert isinstance(held, Failure)
    assert held.error.code is ErrorCode.NOT_FOUND
    assert isinstance(fills, Success)
    assert fills.value == ()
    assert isinstance(receipt, Failure)
    assert receipt.error.code is ErrorCode.NOT_FOUND


def test_projection_reconciliation_failure_rolls_back_execution(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "update paper_cash_projection set reserved_amount=0 "
                "where account_id=:account_id and currency='USD'"
            ),
            {"account_id": ACCOUNT_ID},
        )

    result = execute_reference_paper(
        execution_request(),
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Failure)
    assert result.error.code is ErrorCode.CONFLICT
    with clean_database.connect() as connection:
        assert connection.scalar(text("select count(*) from order_event")) == 0
        assert connection.scalar(text("select count(*) from paper_fill")) == 0
        assert connection.scalar(text("select count(*) from reservation_event")) == 1


def test_pending_receipt_keeps_open_reservation_without_projection_change(
    clean_database: Engine,
) -> None:
    _seed_order(clean_database)
    submitted = execution_request()
    pending = submitted.model_copy(update={"bars": ()})

    result = execute_reference_paper(
        pending,
        _broker(),
        lambda: PostgresUnitOfWork(clean_database),
    )

    assert isinstance(result, Success)
    assert result.value.outcome.receipt.status.value == "accepted"
    with clean_database.connect() as connection:
        assert connection.scalar(
            text(
                "select reserved_amount from paper_cash_projection "
                "where account_id=:account_id and currency='USD'"
            ),
            {"account_id": ACCOUNT_ID},
        ) == Decimal("405.00")
        assert (
            connection.scalar(
                text(
                    "select state from account_reservation where account_id=:account_id"
                ),
                {"account_id": ACCOUNT_ID},
            )
            == "open"
        )
