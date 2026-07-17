"""Core-owned transaction for deterministic reference paper execution."""

from __future__ import annotations

from stonks_agent.application.ledger.post import (
    LedgerPostingPolicy,
    build_fill_journal,
)
from stonks_agent.application.telemetry import record_operation
from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.execution_model import PaperExecutionRequest
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.domain.trading_persistence import PaperExecutionRecord
from stonks_agent.ports.execution import PaperExecutionModelPort
from stonks_agent.ports.telemetry import OperationRecorderPort
from stonks_agent.ports.trading_unit_of_work import (
    TradingCommitError,
    TradingUnitOfWorkFactory,
)


def execute_reference_paper(
    request: PaperExecutionRequest,
    model: PaperExecutionModelPort,
    ledger_policy: LedgerPostingPolicy,
    unit_of_work: TradingUnitOfWorkFactory,
    *,
    telemetry: OperationRecorderPort | None = None,
) -> Result[PaperExecutionRecord]:
    """Persist receipt, fills, journals, and settled projections atomically."""

    return record_operation(
        telemetry,
        component=ComponentName.EXECUTION,
        operation=OperationName.EXECUTE,
        call=lambda: _execute_reference_paper(
            request,
            model,
            ledger_policy,
            unit_of_work,
        ),
    )


def _execute_reference_paper(
    request: PaperExecutionRequest,
    model: PaperExecutionModelPort,
    ledger_policy: LedgerPostingPolicy,
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[PaperExecutionRecord]:
    result, activate_kill = _execute_once(request, model, ledger_policy, unit_of_work)
    if not activate_kill:
        return result
    activated = _activate_accounting_kill_switch(unit_of_work)
    return result if isinstance(activated, Success) else activated


def _execute_once(
    request: PaperExecutionRequest,
    model: PaperExecutionModelPort,
    ledger_policy: LedgerPostingPolicy,
    unit_of_work: TradingUnitOfWorkFactory,
) -> tuple[Result[PaperExecutionRecord], bool]:
    intent = request.command.intent
    with unit_of_work() as transaction:
        replay = transaction.trading.get_execution_record(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(replay, Success):
            if replay.value.command_hash != request.command.command_hash:
                return (
                    failure(
                        ErrorCode.CONFLICT, "Execution idempotency payload changed"
                    ),
                    False,
                )
            graph = transaction.ledger.validate_execution_graph(replay.value)
            return (replay, False) if isinstance(graph, Success) else (graph, True)
        if replay.error.code is not ErrorCode.NOT_FOUND:
            return replay, False
        enabled = transaction.ledger.execution_enabled(intent.account_id)
        if isinstance(enabled, Failure):
            return enabled, False
        loaded_intent = transaction.trading.get_order_by_idempotency(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(loaded_intent, Failure):
            return loaded_intent, False
        if loaded_intent.value != intent:
            return (
                failure(ErrorCode.CONFLICT, "Execution order intent binding changed"),
                False,
            )
        held = transaction.trading.get_reservation(intent.reservation_id)
        if isinstance(held, Failure):
            return held, False
        account = transaction.trading.get_account(intent.account_id)
        if isinstance(account, Failure):
            return account, False
        if (
            account.value.account_aggregate_sequence
            != request.command.account_aggregate_sequence
        ):
            sequence_replay = transaction.trading.get_execution_record(
                account_id=intent.account_id,
                idempotency_key=intent.idempotency_key,
            )
            if (
                isinstance(sequence_replay, Success)
                and sequence_replay.value.command_hash == request.command.command_hash
            ):
                graph = transaction.ledger.validate_execution_graph(
                    sequence_replay.value
                )
                return (
                    (sequence_replay, False)
                    if isinstance(graph, Success)
                    else (graph, True)
                )
            return (
                failure(ErrorCode.CONFLICT, "Execution account sequence is stale"),
                False,
            )
        projection = transaction.ledger.get_projection(intent.account_id)
        if isinstance(projection, Failure):
            return projection, projection.error.code is ErrorCode.CONFLICT
        events = transaction.trading.list_order_events(intent.intent_id)
        if isinstance(events, Failure):
            return events, False
        fills = transaction.trading.list_fills(intent.intent_id)
        if isinstance(fills, Failure):
            return fills, False
        concurrent_replay = transaction.trading.get_execution_record(
            account_id=intent.account_id,
            idempotency_key=intent.idempotency_key,
        )
        if isinstance(concurrent_replay, Success):
            if concurrent_replay.value.command_hash != request.command.command_hash:
                return (
                    failure(
                        ErrorCode.CONFLICT, "Execution idempotency payload changed"
                    ),
                    False,
                )
            graph = transaction.ledger.validate_execution_graph(concurrent_replay.value)
            return (
                (concurrent_replay, False)
                if isinstance(graph, Success)
                else (graph, True)
            )
        if concurrent_replay.error.code is not ErrorCode.NOT_FOUND:
            return concurrent_replay, False
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
            return (
                failure(ErrorCode.CONFLICT, "Execution state cannot be rehydrated"),
                False,
            )
        simulated = model.execute(authoritative)
        if isinstance(simulated, Failure):
            return simulated, False
        persisted = transaction.trading.apply_paper_execution(
            request.command,
            simulated.value,
            expected_account_sequence=account.value.account_aggregate_sequence,
        )
        if isinstance(persisted, Failure):
            return persisted, False
        current_projection = projection.value
        current_account_sequence = account.value.account_aggregate_sequence
        new_fills = simulated.value.receipt.fills[len(fills.value) :]
        for fill in new_fills:
            journal = build_fill_journal(fill, current_projection, ledger_policy)
            if isinstance(journal, Failure):
                return journal, True
            posted = transaction.ledger.append(
                journal.value,
                expected_sequence=current_projection.ledger_sequence,
                expected_hash=current_projection.ledger_hash,
                expected_account_sequence=current_account_sequence,
            )
            if isinstance(posted, Failure):
                return posted, True
            refreshed = transaction.ledger.get_projection(intent.account_id)
            if isinstance(refreshed, Failure):
                return refreshed, True
            current_projection = refreshed.value
            current_account_sequence += 1
        graph = transaction.ledger.validate_execution_graph(persisted.value)
        if isinstance(graph, Failure):
            return graph, True
        try:
            transaction.commit()
        except TradingCommitError:
            return (
                failure(ErrorCode.CONFLICT, "Accounted execution commit failed"),
                True,
            )
        return persisted, False


def _activate_accounting_kill_switch(
    unit_of_work: TradingUnitOfWorkFactory,
) -> Result[bool]:
    with unit_of_work() as transaction:
        activated = transaction.ledger.activate_global_kill_switch(
            reason_code="ledger_invariant_mismatch",
            actor="system:paper_execution",
        )
        if isinstance(activated, Failure):
            return failure(
                ErrorCode.INTERNAL_ERROR,
                "Ledger failed and global paper kill switch could not be activated",
            )
        try:
            transaction.commit()
        except TradingCommitError:
            return failure(
                ErrorCode.INTERNAL_ERROR,
                "Global paper kill switch activation did not commit",
            )
        return activated
