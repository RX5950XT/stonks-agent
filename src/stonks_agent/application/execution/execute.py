"""Core-owned transaction for deterministic reference paper execution."""

from __future__ import annotations

from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.execution_model import PaperExecutionRequest
from stonks_agent.domain.trading_persistence import PaperExecutionRecord
from stonks_agent.ports.execution import PaperExecutionModelPort
from stonks_agent.ports.trading_unit_of_work import TradingUnitOfWorkFactory


def execute_reference_paper(
    request: PaperExecutionRequest,
    model: PaperExecutionModelPort,
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[PaperExecutionRecord]:
    """Rehydrate authoritative state, simulate, then atomically persist outcome."""

    intent = request.command.intent
    with unit_of_work() as transaction:
        replay = transaction.trading.get_execution_record(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(replay, Success):
            if replay.value.command_hash != request.command.command_hash:
                return failure(
                    ErrorCode.CONFLICT, "Execution idempotency payload changed"
                )
            return replay
        if replay.error.code is not ErrorCode.NOT_FOUND:
            return replay
        loaded_intent = transaction.trading.get_order_by_idempotency(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(loaded_intent, Failure):
            return loaded_intent
        if loaded_intent.value != intent:
            return failure(ErrorCode.CONFLICT, "Execution order intent binding changed")
        held = transaction.trading.get_reservation(intent.reservation_id)
        if isinstance(held, Failure):
            return held
        account = transaction.trading.get_account(intent.account_id)
        if isinstance(account, Failure):
            return account
        if (
            account.value.account_aggregate_sequence
            != request.command.account_aggregate_sequence
        ):
            return failure(ErrorCode.CONFLICT, "Execution account sequence is stale")
        events = transaction.trading.list_order_events(intent.intent_id)
        if isinstance(events, Failure):
            return events
        fills = transaction.trading.list_fills(intent.intent_id)
        if isinstance(fills, Failure):
            return fills
        concurrent_replay = transaction.trading.get_execution_record(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(concurrent_replay, Success):
            if concurrent_replay.value.command_hash != request.command.command_hash:
                return failure(
                    ErrorCode.CONFLICT, "Execution idempotency payload changed"
                )
            return concurrent_replay
        if concurrent_replay.error.code is not ErrorCode.NOT_FOUND:
            return concurrent_replay
        try:
            authoritative = PaperExecutionRequest(
                command=request.command,
                reservation=held.value,
                prior_events=events.value,
                prior_fills=fills.value,
                bars=request.bars,
                as_of=request.as_of,
            )
        except ValueError:
            return failure(ErrorCode.CONFLICT, "Execution state cannot be rehydrated")
        simulated = model.execute(authoritative)
        if isinstance(simulated, Failure):
            return simulated
        persisted = transaction.trading.apply_paper_execution(
            request.command,
            simulated.value,
            expected_account_sequence=account.value.account_aggregate_sequence,
        )
        if isinstance(persisted, Failure):
            return persisted
        transaction.commit()
        return persisted
