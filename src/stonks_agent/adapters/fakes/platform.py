"""A semantics-complete, deterministic paper platform kept entirely in memory."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from threading import Lock, RLock
from typing import Any

from stonks_agent.application.workflows.run_cycle import (
    AccountSnapshotRecord,
    AnalysisReportRecord,
    ExecutionReceiptRecord,
    FillRecord,
    IdempotencyConflict,
    JournalPostingRecord,
    JournalTransactionRecord,
    OrderIntentRecord,
    PortfolioTargetRecord,
    ReplayResult,
    ReservationRecord,
    ReservationStatus,
    RiskDecisionRecord,
    RunCycleRequest,
    RunCycleResult,
    RunEvent,
    stable_hash,
)

CURRENT_PRICE = Decimal("100.00")
NEXT_PRICE = Decimal("101.00")
FEE = Decimal("1.00")
TARGET_WEIGHT = Decimal("0.05")
DEADBAND_LOW = Decimal("0.04")
DEADBAND_HIGH = Decimal("0.06")


@dataclass(slots=True)
class _AccountState:
    account_id: str
    cash: Decimal
    positions: dict[str, Decimal]
    reservations: dict[str, ReservationRecord]
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class _ReplayData:
    before: AccountSnapshotRecord
    result: RunCycleResult


class FakeRunService:
    """Run the canonical paper workflow with deterministic local adapters."""

    def __init__(self, *, clock: datetime, seed: str, initial_cash: Decimal) -> None:
        if clock.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self._clock = clock.astimezone(UTC)
        self._seed = seed
        self._initial_cash = initial_cash
        self._guard = RLock()
        self._account_locks: dict[str, Lock] = {}
        self._accounts: dict[str, _AccountState] = {}
        self._idempotency: dict[str, tuple[str, RunCycleResult]] = {}
        self._events: list[RunEvent] = []
        self._runs: dict[str, _ReplayData] = {}
        self._journals: dict[str, list[JournalTransactionRecord]] = {}

    @property
    def event_count(self) -> int:
        with self._guard:
            return len(self._events)

    def run(self, request: RunCycleRequest) -> RunCycleResult:
        payload_hash = stable_hash(request)
        cached = self._cached(request.idempotency_key, payload_hash)
        if cached is not None:
            return cached
        lock = self._account_lock(request.account_id)
        with lock:
            cached = self._cached(request.idempotency_key, payload_hash)
            if cached is not None:
                return cached
            result = self._run_locked(request, payload_hash)
            with self._guard:
                self._idempotency[request.idempotency_key] = (payload_hash, result)
            return result

    def account_snapshot(self, account_id: str) -> AccountSnapshotRecord:
        lock = self._account_lock(account_id)
        with lock:
            return self._snapshot(self._account(account_id))

    def journal_is_balanced(self, account_id: str) -> bool:
        with self._guard:
            entries = tuple(self._journals.get(account_id, ()))
        return all(entry.is_balanced() for entry in entries)

    def replay(self, run_id: str) -> ReplayResult:
        with self._guard:
            replay_data = self._runs[run_id]
        projection = _apply_result(replay_data.before, replay_data.result)
        return ReplayResult(run_id=run_id, projection_hash=stable_hash(projection))

    def _run_locked(
        self, request: RunCycleRequest, request_hash: str
    ) -> RunCycleResult:
        account = self._account(request.account_id)
        before = self._snapshot(account)
        run_id = self._identifier("run", request.idempotency_key, request_hash)
        events: list[RunEvent] = []
        self._append_event(events, run_id, "run.created", request)
        evidence_id = self._identifier("evidence", run_id)
        self._append_event(events, run_id, "evidence.collected", evidence_id)
        target = self._build_target(account, request)
        self._append_event(events, run_id, "portfolio.target_built", target)
        risk = self._evaluate_risk(account, request, target, run_id)
        self._append_event(events, run_id, "risk.evaluated", risk)
        execution = self._execute_if_approved(account, request, target, risk, run_id)
        reservation, intent, receipt, journal = execution
        for event_type, payload in _execution_events(execution):
            self._append_event(events, run_id, event_type, payload)
        report = self._build_report(run_id, evidence_id, risk, receipt)
        self._append_event(events, run_id, "report.generated", report)
        self._append_event(events, run_id, "run.completed", risk.approved)
        after = self._snapshot(account)
        status = "completed" if risk.approved else "rejected"
        control_hash = stable_hash((target, risk, reservation, intent, receipt, journal))
        result = RunCycleResult(
            run_id=run_id,
            status=status,
            evidence_id=evidence_id,
            portfolio_target=target,
            risk_decision=risk,
            reservation=reservation,
            order_intent=intent,
            execution_receipt=receipt,
            journal_transaction=journal,
            report=report,
            events=tuple(events),
            control_hash=control_hash,
            projection_hash=stable_hash(after),
        )
        with self._guard:
            self._events.extend(events)
            self._runs[run_id] = _ReplayData(before=before, result=result)
            if journal is not None:
                self._journals.setdefault(request.account_id, []).append(journal)
        return result

    def _build_target(
        self, account: _AccountState, request: RunCycleRequest
    ) -> PortfolioTargetRecord:
        current_quantity = account.positions.get(request.instrument_id, Decimal("0"))
        nav = account.cash + sum(
            quantity * CURRENT_PRICE for quantity in account.positions.values()
        )
        current_weight = (
            current_quantity * CURRENT_PRICE / nav if nav > 0 else Decimal("0")
        )
        eligible = request.signal_value > 0 and request.signal_confidence >= Decimal("0.5")
        if eligible and DEADBAND_LOW <= current_weight <= DEADBAND_HIGH:
            target_quantity = current_quantity
        elif eligible:
            target_quantity = (nav * TARGET_WEIGHT / CURRENT_PRICE).to_integral_value(
                rounding=ROUND_DOWN
            )
        else:
            target_quantity = current_quantity
        return PortfolioTargetRecord(
            instrument_id=request.instrument_id,
            target_weight=TARGET_WEIGHT if eligible else current_weight,
            target_quantity=target_quantity,
            delta_quantity=target_quantity - current_quantity,
        )

    def _evaluate_risk(
        self,
        account: _AccountState,
        request: RunCycleRequest,
        target: PortfolioTargetRecord,
        run_id: str,
    ) -> RiskDecisionRecord:
        reasons: list[str] = []
        if request.evidence_available_at > request.as_of:
            reasons.append("future_evidence")
        if target.delta_quantity < 0:
            reasons.append("sell_not_supported")
        required_cash = _reservation_cash(target.delta_quantity)
        available_cash = account.cash - sum(
            item.cash_amount
            for item in account.reservations.values()
            if item.status == ReservationStatus.OPEN
        )
        if required_cash > available_cash:
            reasons.append("insufficient_cash")
        return RiskDecisionRecord(
            decision_id=self._identifier("risk", run_id),
            approved=not reasons,
            reasons=tuple(reasons),
            account_sequence=account.sequence,
        )

    def _execute_if_approved(
        self,
        account: _AccountState,
        request: RunCycleRequest,
        target: PortfolioTargetRecord,
        risk: RiskDecisionRecord,
        run_id: str,
    ) -> tuple[
        ReservationRecord | None,
        OrderIntentRecord | None,
        ExecutionReceiptRecord | None,
        JournalTransactionRecord | None,
    ]:
        if not risk.approved or target.delta_quantity == 0:
            return None, None, None, None
        reservation = self._reserve(account, request, target, risk, run_id)
        intent = OrderIntentRecord(
            order_intent_id=self._identifier("order", run_id),
            instrument_id=request.instrument_id,
            side="buy",
            quantity=target.delta_quantity,
            reservation_id=reservation.reservation_id,
            idempotency_key=f"{request.idempotency_key}:order",
        )
        fill = FillRecord(
            fill_id=self._identifier("fill", run_id),
            instrument_id=request.instrument_id,
            quantity=intent.quantity,
            price=NEXT_PRICE,
            fee=FEE,
            bar_time=_next_session(request.as_of),
        )
        journal = _journal_for_fill(self._identifier("journal", run_id), fill)
        self._apply_fill(account, reservation, fill)
        consumed = replace(reservation, status=ReservationStatus.CONSUMED)
        receipt = ExecutionReceiptRecord(
            receipt_id=self._identifier("receipt", run_id),
            order_intent_id=intent.order_intent_id,
            status="filled",
            fill=fill,
        )
        return consumed, intent, receipt, journal

    def _reserve(
        self,
        account: _AccountState,
        request: RunCycleRequest,
        target: PortfolioTargetRecord,
        risk: RiskDecisionRecord,
        run_id: str,
    ) -> ReservationRecord:
        if risk.account_sequence != account.sequence:
            raise RuntimeError("account sequence changed before reservation")
        reservation = ReservationRecord(
            reservation_id=self._identifier("reservation", run_id),
            account_id=request.account_id,
            instrument_id=request.instrument_id,
            cash_amount=_reservation_cash(target.delta_quantity),
            quantity=target.delta_quantity,
            status=ReservationStatus.OPEN,
            account_sequence=account.sequence,
        )
        account.reservations[reservation.reservation_id] = reservation
        account.sequence += 1
        return reservation

    @staticmethod
    def _apply_fill(
        account: _AccountState, reservation: ReservationRecord, fill: FillRecord
    ) -> None:
        cost = fill.quantity * fill.price + fill.fee
        if cost > reservation.cash_amount:
            raise RuntimeError("fill exceeded reservation")
        if cost > account.cash:
            raise RuntimeError("fill exceeded available cash")
        account.cash -= cost
        account.positions[fill.instrument_id] = (
            account.positions.get(fill.instrument_id, Decimal("0")) + fill.quantity
        )
        account.reservations.pop(reservation.reservation_id)
        account.sequence += 1

    def _build_report(
        self,
        run_id: str,
        evidence_id: str,
        risk: RiskDecisionRecord,
        receipt: ExecutionReceiptRecord | None,
    ) -> AnalysisReportRecord:
        if not risk.approved:
            conclusion = "rejected"
            limitations = risk.reasons
        elif receipt is None:
            conclusion = "hold"
            limitations = ()
        else:
            conclusion = "paper_filled"
            limitations = ("deterministic_fixture_data",)
        return AnalysisReportRecord(
            report_id=self._identifier("report", run_id),
            conclusion=conclusion,
            evidence_refs=(evidence_id,),
            limitations=limitations,
        )

    def _cached(self, key: str, payload_hash: str) -> RunCycleResult | None:
        with self._guard:
            cached = self._idempotency.get(key)
        if cached is None:
            return None
        cached_hash, result = cached
        if cached_hash != payload_hash:
            raise IdempotencyConflict("idempotency key payload mismatch")
        return result

    def _account_lock(self, account_id: str) -> Lock:
        with self._guard:
            return self._account_locks.setdefault(account_id, Lock())

    def _account(self, account_id: str) -> _AccountState:
        with self._guard:
            return self._accounts.setdefault(
                account_id,
                _AccountState(
                    account_id=account_id,
                    cash=self._initial_cash,
                    positions={},
                    reservations={},
                ),
            )

    @staticmethod
    def _snapshot(account: _AccountState) -> AccountSnapshotRecord:
        open_ids = tuple(
            sorted(
                reservation_id
                for reservation_id, reservation in account.reservations.items()
                if reservation.status == ReservationStatus.OPEN
            )
        )
        return AccountSnapshotRecord(
            account_id=account.account_id,
            cash=account.cash,
            positions=dict(sorted(account.positions.items())),
            open_reservations=open_ids,
            sequence=account.sequence,
        )

    def _append_event(
        self, events: list[RunEvent], run_id: str, event_type: str, payload: Any
    ) -> None:
        previous_hash = events[-1].event_hash if events else None
        payload_hash = stable_hash(payload)
        event_hash = stable_hash(
            {
                "run_id": run_id,
                "sequence": len(events) + 1,
                "event_type": event_type,
                "payload_hash": payload_hash,
                "previous_hash": previous_hash,
            }
        )
        events.append(
            RunEvent(
                run_id=run_id,
                sequence=len(events) + 1,
                event_type=event_type,
                payload_hash=payload_hash,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
        )

    def _identifier(self, kind: str, *parts: str) -> str:
        return f"{kind}_{stable_hash((self._seed, kind, *parts))[:24]}"


def build_fake_run_service(
    *,
    clock: datetime,
    seed: str,
    initial_cash: Decimal = Decimal("100000.00"),
) -> FakeRunService:
    return FakeRunService(clock=clock, seed=seed, initial_cash=initial_cash)


def _reservation_cash(quantity: Decimal) -> Decimal:
    if quantity <= 0:
        return Decimal("0")
    return quantity * NEXT_PRICE + FEE


def _next_session(as_of: datetime) -> datetime:
    candidate = as_of + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _journal_for_fill(
    transaction_id: str, fill: FillRecord
) -> JournalTransactionRecord:
    notional = fill.quantity * fill.price
    transaction = JournalTransactionRecord(
        transaction_id=transaction_id,
        source_fill_id=fill.fill_id,
        postings=(
            JournalPostingRecord(
                account=f"asset:securities:{fill.instrument_id}",
                commodity="USD",
                signed_amount=notional,
            ),
            JournalPostingRecord(
                account="expense:fees",
                commodity="USD",
                signed_amount=fill.fee,
            ),
            JournalPostingRecord(
                account="asset:cash:USD",
                commodity="USD",
                signed_amount=-(notional + fill.fee),
            ),
        ),
    )
    if not transaction.is_balanced():
        raise RuntimeError("journal transaction is not balanced")
    return transaction


def _execution_events(
    execution: tuple[
        ReservationRecord | None,
        OrderIntentRecord | None,
        ExecutionReceiptRecord | None,
        JournalTransactionRecord | None,
    ]
) -> tuple[tuple[str, Any], ...]:
    names = (
        "account.reservation_consumed",
        "order.intent_created",
        "execution.receipt_recorded",
        "journal.transaction_posted",
    )
    return tuple(
        (name, payload)
        for name, payload in zip(names, execution, strict=True)
        if payload is not None
    )


def _apply_result(
    before: AccountSnapshotRecord, result: RunCycleResult
) -> AccountSnapshotRecord:
    cash = before.cash
    positions = dict(before.positions)
    sequence = before.sequence
    if result.reservation is not None:
        sequence += 1
    receipt = result.execution_receipt
    if receipt is not None and receipt.fill is not None:
        fill = receipt.fill
        cash -= fill.quantity * fill.price + fill.fee
        positions[fill.instrument_id] = (
            positions.get(fill.instrument_id, Decimal("0")) + fill.quantity
        )
        sequence += 1
    return AccountSnapshotRecord(
        account_id=before.account_id,
        cash=cash,
        positions=dict(sorted(positions.items())),
        open_reservations=(),
        sequence=sequence,
    )

