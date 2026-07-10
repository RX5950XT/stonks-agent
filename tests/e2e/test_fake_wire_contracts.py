from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from stonks_agent.adapters.fakes.platform import build_fake_run_service
from stonks_agent.adapters.fakes.wire import export_completed_run
from stonks_agent.application.workflows.run_cycle import RunCycleRequest
from stonks_contracts import (
    AccountReservation,
    AlphaSignal,
    AnalysisReport,
    ExecutionCommand,
    ExecutionReceipt,
    JournalTransaction,
    OrderIntent,
    PortfolioTarget,
    RiskDecision,
)

AS_OF = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def test_completed_fake_cycle_exports_canonical_wire_chain() -> None:
    request = RunCycleRequest(
        idempotency_key="wire-cycle",
        account_id="paper-main",
        instrument_id="instrument-aapl",
        symbol="AAPL",
        as_of=AS_OF,
        evidence_available_at=AS_OF,
        signal_value=Decimal("0.80"),
        signal_confidence=Decimal("0.90"),
    )
    result = build_fake_run_service(clock=AS_OF, seed="wire").run(request)

    wire = export_completed_run(request, result)

    assert isinstance(wire.signal, AlphaSignal)
    assert isinstance(wire.target, PortfolioTarget)
    assert isinstance(wire.risk, RiskDecision)
    assert isinstance(wire.reservation, AccountReservation)
    assert isinstance(wire.order_intent, OrderIntent)
    assert isinstance(wire.command, ExecutionCommand)
    assert isinstance(wire.receipt, ExecutionReceipt)
    assert isinstance(wire.journal, JournalTransaction)
    assert isinstance(wire.report, AnalysisReport)
    assert wire.signal.evidence_refs == (wire.evidence.evidence_id,)
    assert wire.target.input_signal_ids == (wire.signal.signal_id,)
    assert wire.risk.normalized_target == wire.target
    assert wire.order_intent.reservation_id == wire.reservation.reservation_id
    assert wire.command.intent == wire.order_intent
    assert wire.receipt.fill is not None
    assert wire.receipt.fill.occurred_at > request.as_of
    assert wire.journal.source_fill_id == wire.receipt.fill.fill_id
    assert wire.journal.is_balanced()
    assert wire.report.evidence_refs == (wire.evidence.evidence_id,)
    assert all(model.payload_hash() for model in wire.models())


def test_wire_chain_serialization_contains_no_trade_intent_contract() -> None:
    request = RunCycleRequest(
        idempotency_key="authority",
        account_id="paper-main",
        instrument_id="instrument-aapl",
        symbol="AAPL",
        as_of=AS_OF,
        evidence_available_at=AS_OF,
        signal_value=Decimal("0.80"),
        signal_confidence=Decimal("0.90"),
    )
    result = build_fake_run_service(clock=AS_OF, seed="authority").run(request)

    wire = export_completed_run(request, result)
    serialized = "\n".join(model.canonical_json() for model in wire.models())

    assert "TradeIntent" not in serialized
    assert "trade_intent" not in serialized

