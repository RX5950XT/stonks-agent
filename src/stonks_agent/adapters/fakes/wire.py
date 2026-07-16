"""Map the in-memory vertical slice onto canonical wire contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from stonks_agent.application.workflows.run_cycle import (
    RunCycleRequest,
    RunCycleResult,
    stable_hash,
)
from stonks_contracts.common import ContractModel, Money
from stonks_contracts.evidence import EvidenceItem, EvidenceKind
from stonks_contracts.execution import (
    ExecutionCommand,
    ExecutionReceipt,
    Fill,
    JournalPosting,
    JournalSide,
    JournalTransaction,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.portfolio import PortfolioTarget, TargetAllocation
from stonks_contracts.report import AnalysisReport
from stonks_contracts.risk import (
    AccountReservation,
    ReservationKind,
    ReservationState,
    RiskDecision,
)
from stonks_contracts.signal import (
    AlphaSignal,
    PromotionState,
    SignalDirection,
)


@dataclass(frozen=True, slots=True)
class FakeWireBundle:
    evidence: EvidenceItem
    signal: AlphaSignal
    target: PortfolioTarget
    risk: RiskDecision
    reservation: AccountReservation
    order_intent: OrderIntent
    command: ExecutionCommand
    receipt: ExecutionReceipt
    journal: JournalTransaction
    report: AnalysisReport

    def models(self) -> tuple[ContractModel, ...]:
        return (
            self.evidence,
            self.signal,
            self.target,
            self.risk,
            self.reservation,
            self.order_intent,
            self.command,
            self.receipt,
            self.journal,
            self.report,
        )


def export_completed_run(
    request: RunCycleRequest, result: RunCycleResult
) -> FakeWireBundle:
    """Export a successful fake cycle without leaking internal record types."""
    if result.status != "completed" or result.execution_receipt is None:
        raise ValueError(
            "only completed runs with an execution receipt can be exported"
        )
    if result.reservation is None or result.order_intent is None:
        raise ValueError("completed execution requires reservation and order intent")
    if result.journal_transaction is None or result.execution_receipt.fill is None:
        raise ValueError("completed execution requires fill and journal")

    identifiers = _Identifiers(request, result)
    evidence = _evidence(request, result, identifiers)
    signal = _signal(request, identifiers)
    target = _target(request, result, identifiers, signal)
    risk = _risk(request, result, identifiers, target)
    fill_time = result.execution_receipt.fill.bar_time
    reservation = _reservation(request, result, identifiers, fill_time)
    order = _order(request, result, identifiers, fill_time)
    command = ExecutionCommand(
        command_id=identifiers.command,
        intent=order,
        attempt_generation=1,
        attempt_nonce=stable_hash((result.run_id, "attempt"))[:32],
        issued_at=request.as_of,
    )
    fill = _fill(request, result, identifiers, command)
    receipt = _receipt(result, identifiers, command, fill)
    journal = _journal(result, identifiers, fill, order)
    report = _report(request, result, identifiers, evidence)
    return FakeWireBundle(
        evidence=evidence,
        signal=signal,
        target=target,
        risk=risk,
        reservation=reservation,
        order_intent=order,
        command=command,
        receipt=receipt,
        journal=journal,
        report=report,
    )


@dataclass(frozen=True, slots=True)
class _Identifiers:
    run: UUID
    evidence: UUID
    signal: UUID
    snapshot: UUID
    target: UUID
    risk: UUID
    reservation: UUID
    order: UUID
    command: UUID
    fill: UUID
    receipt: UUID
    journal: UUID
    report: UUID
    instrument: UUID

    def __init__(self, request: RunCycleRequest, result: RunCycleResult) -> None:
        reservation = result.reservation
        order = result.order_intent
        receipt = result.execution_receipt
        journal = result.journal_transaction
        assert reservation is not None
        assert order is not None
        assert receipt is not None
        assert journal is not None
        object.__setattr__(self, "run", _uuid(result.run_id))
        object.__setattr__(self, "evidence", _uuid(result.evidence_id))
        object.__setattr__(self, "signal", _uuid(f"{result.run_id}:signal"))
        object.__setattr__(self, "snapshot", _uuid(f"{result.run_id}:snapshot"))
        object.__setattr__(self, "target", _uuid(f"{result.run_id}:target"))
        object.__setattr__(self, "risk", _uuid(result.risk_decision.decision_id))
        object.__setattr__(self, "reservation", _uuid(reservation.reservation_id))
        object.__setattr__(self, "order", _uuid(order.order_intent_id))
        object.__setattr__(self, "command", _uuid(f"{result.run_id}:command"))
        fill = receipt.fill
        assert fill is not None
        object.__setattr__(self, "fill", _uuid(fill.fill_id))
        object.__setattr__(self, "receipt", _uuid(receipt.receipt_id))
        object.__setattr__(self, "journal", _uuid(journal.transaction_id))
        object.__setattr__(self, "report", _uuid(result.report.report_id))
        object.__setattr__(self, "instrument", _uuid(request.instrument_id))


def _evidence(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
) -> EvidenceItem:
    content_hash = stable_hash({"symbol": request.symbol, "fixture_price": "100.00"})
    return EvidenceItem(
        evidence_id=identifiers.evidence,
        subject=request.symbol,
        kind=EvidenceKind.MARKET_DATA,
        payload={"symbol": request.symbol, "fixture_price": "100.00"},
        event_time=request.as_of,
        published_at=request.as_of,
        available_at=request.evidence_available_at,
        observed_at=request.as_of,
        as_of=request.as_of,
        source="stonks-agent-fixture",
        provider="replay",
        content_hash=content_hash,
        raw_artifact_ref=f"sha256:{content_hash}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE,
            completeness=Decimal("1"),
        ),
        license_tag="internal-fixture",
        redistribution_tag="allowed",
        untrusted_content=False,
    )


def _signal(request: RunCycleRequest, identifiers: _Identifiers) -> AlphaSignal:
    return AlphaSignal(
        signal_id=identifiers.signal,
        strategy_id="fixture-alpha",
        strategy_version="1.0.0",
        instrument_id=identifiers.instrument,
        as_of=request.as_of,
        horizon="1-session",
        value=request.signal_value,
        confidence=request.signal_confidence,
        expires_at=request.as_of + timedelta(days=7),
        direction=SignalDirection.LONG,
        evidence_refs=(identifiers.evidence,),
        reason_codes=("fixture_positive_signal",),
        promotion_state=PromotionState.PAPER_ELIGIBLE,
    )


def _target(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    signal: AlphaSignal,
) -> PortfolioTarget:
    target = result.portfolio_target
    return PortfolioTarget(
        target_id=identifiers.target,
        account_id=request.account_id,
        portfolio_snapshot_id=identifiers.snapshot,
        as_of=request.as_of,
        allocations=(
            TargetAllocation(
                instrument_id=identifiers.instrument,
                target_weight=target.target_weight,
                current_quantity=target.target_quantity - target.delta_quantity,
                target_quantity=target.target_quantity,
                delta_quantity=target.delta_quantity,
            ),
        ),
        input_signal_ids=(signal.signal_id,),
        input_evidence_ids=(identifiers.evidence,),
        policy_version=target.policy_version,
        expected_turnover=target.target_weight,
        expected_cost=Money(currency="USD", amount=Decimal("1.00")),
        calculation_hash=result.control_hash,
    )


def _risk(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    target: PortfolioTarget,
) -> RiskDecision:
    return RiskDecision(
        decision_id=identifiers.risk,
        portfolio_target_id=target.target_id,
        account_id=request.account_id,
        approved=True,
        normalized_target=target,
        reasons=result.risk_decision.reasons,
        limits_snapshot_hash=stable_hash({"max_weight": "0.05"}),
        policy_version=result.risk_decision.policy_version,
        policy_hash=stable_hash(result.risk_decision.policy_version),
        decided_at=request.as_of,
        expires_at=request.as_of + timedelta(days=7),
    )


def _reservation(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    fill_time: datetime,
) -> AccountReservation:
    assert result.reservation is not None
    return AccountReservation(
        reservation_id=identifiers.reservation,
        account_id=request.account_id,
        kind=ReservationKind.CASH,
        commodity="USD",
        amount=result.reservation.cash_amount,
        risk_decision_id=identifiers.risk,
        portfolio_sequence=result.reservation.account_sequence,
        order_intent_id=identifiers.order,
        state=ReservationState(result.reservation.status),
        created_at=request.as_of,
        expires_at=fill_time + timedelta(days=1),
    )


def _order(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    fill_time: datetime,
) -> OrderIntent:
    assert result.order_intent is not None
    order = result.order_intent
    return OrderIntent(
        intent_id=identifiers.order,
        run_id=identifiers.run,
        account_id=request.account_id,
        instrument_id=identifiers.instrument,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=order.quantity,
        time_in_force=TimeInForce.DAY,
        valid_from=request.as_of,
        valid_until=fill_time + timedelta(days=1),
        risk_decision_id=identifiers.risk,
        reservation_id=identifiers.reservation,
        portfolio_snapshot_id=identifiers.snapshot,
        aggregate_sequence=result.risk_decision.account_sequence,
        idempotency_key=order.idempotency_key,
        execution_model_version="paper-v1",
        created_at=request.as_of,
    )


def _fill(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    command: ExecutionCommand,
) -> Fill:
    receipt = result.execution_receipt
    assert receipt is not None
    internal = receipt.fill
    assert internal is not None
    return Fill(
        fill_id=identifiers.fill,
        command_id=command.command_id,
        order_intent_id=identifiers.order,
        account_id=request.account_id,
        instrument_id=identifiers.instrument,
        side=OrderSide.BUY,
        quantity=internal.quantity,
        price=internal.price,
        fee_currency="USD",
        fees=internal.fee,
        slippage=internal.price - Decimal("100.00"),
        occurred_at=internal.bar_time,
        sequence=1,
        simulator_ref="fixture-paper-v1",
    )


def _receipt(
    result: RunCycleResult,
    identifiers: _Identifiers,
    command: ExecutionCommand,
    fill: Fill,
) -> ExecutionReceipt:
    internal_receipt = result.execution_receipt
    assert internal_receipt is not None
    return ExecutionReceipt(
        receipt_id=identifiers.receipt,
        command_id=command.command_id,
        order_intent_id=identifiers.order,
        status=OrderStatus.FILLED,
        occurred_at=fill.occurred_at,
        sequence=1,
        filled_quantity=fill.quantity,
        remaining_quantity=Decimal("0"),
        command_quantity=fill.quantity,
        fills=(fill,),
        simulator_ref=internal_receipt.receipt_id,
    )


def _journal(
    result: RunCycleResult,
    identifiers: _Identifiers,
    fill: Fill,
    order: OrderIntent,
) -> JournalTransaction:
    assert result.journal_transaction is not None
    postings = tuple(
        JournalPosting(
            account=posting.account,
            commodity=posting.commodity,
            side=(
                JournalSide.DEBIT if posting.signed_amount > 0 else JournalSide.CREDIT
            ),
            amount=abs(posting.signed_amount),
        )
        for posting in result.journal_transaction.postings
    )
    return JournalTransaction(
        transaction_id=identifiers.journal,
        sequence=1,
        occurred_at=fill.occurred_at,
        source_fill_id=fill.fill_id,
        source_order_intent_id=order.intent_id,
        postings=postings,
    )


def _report(
    request: RunCycleRequest,
    result: RunCycleResult,
    identifiers: _Identifiers,
    evidence: EvidenceItem,
) -> AnalysisReport:
    return AnalysisReport(
        report_id=identifiers.report,
        run_id=identifiers.run,
        owner_subject=f"paper:{request.account_id}",
        subject=request.symbol,
        as_of=request.as_of,
        language="zh-TW",
        report_type="paper-cycle",
        conclusion=result.report.conclusion,
        score=Decimal("0.80"),
        confidence=request.signal_confidence,
        action_guardrails=("paper_only", "risk_approved", "next_session_fill"),
        data_limitations=result.report.limitations,
        evidence_refs=(evidence.evidence_id,),
        signal_ids=(identifiers.signal,),
        generator_version="fixture-report-v1",
        policy_version="report-policy-v1",
    )


def _uuid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"stonks-agent:{value}")
