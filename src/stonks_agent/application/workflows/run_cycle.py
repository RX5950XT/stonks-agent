"""Canonical paper-cycle primitives used by the first vertical slice."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any


class IdempotencyConflict(RuntimeError):
    """An idempotency key was reused with a different payload."""


class LateResultRejected(RuntimeError):
    """A worker result no longer owns the active job attempt."""


class ReservationStatus(StrEnum):
    OPEN = "open"
    CONSUMED = "consumed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class RunCycleRequest:
    idempotency_key: str
    account_id: str
    instrument_id: str
    symbol: str
    as_of: datetime
    evidence_available_at: datetime
    signal_value: Decimal
    signal_confidence: Decimal

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if self.as_of.tzinfo is None or self.evidence_available_at.tzinfo is None:
            raise ValueError("all timestamps must be timezone-aware")
        if not Decimal("-1") <= self.signal_value <= Decimal("1"):
            raise ValueError("signal_value must be in [-1, 1]")
        if not Decimal("0") <= self.signal_confidence <= Decimal("1"):
            raise ValueError("signal_confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    event_type: str
    payload_hash: str
    previous_hash: str | None
    event_hash: str


@dataclass(frozen=True, slots=True)
class PortfolioTargetRecord:
    instrument_id: str
    target_weight: Decimal
    target_quantity: Decimal
    delta_quantity: Decimal
    policy_version: str = "portfolio-v1"


@dataclass(frozen=True, slots=True)
class RiskDecisionRecord:
    decision_id: str
    approved: bool
    reasons: tuple[str, ...]
    account_sequence: int
    policy_version: str = "risk-v1"


@dataclass(frozen=True, slots=True)
class ReservationRecord:
    reservation_id: str
    account_id: str
    instrument_id: str
    cash_amount: Decimal
    quantity: Decimal
    status: str
    account_sequence: int


@dataclass(frozen=True, slots=True)
class OrderIntentRecord:
    order_intent_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    reservation_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class FillRecord:
    fill_id: str
    instrument_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    bar_time: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReceiptRecord:
    receipt_id: str
    order_intent_id: str
    status: str
    fill: FillRecord | None


@dataclass(frozen=True, slots=True)
class JournalPostingRecord:
    account: str
    commodity: str
    signed_amount: Decimal


@dataclass(frozen=True, slots=True)
class JournalTransactionRecord:
    transaction_id: str
    source_fill_id: str
    postings: tuple[JournalPostingRecord, ...]

    def is_balanced(self) -> bool:
        totals: dict[str, Decimal] = {}
        for posting in self.postings:
            totals[posting.commodity] = (
                totals.get(posting.commodity, Decimal("0"))
                + posting.signed_amount
            )
        return bool(self.postings) and all(total == 0 for total in totals.values())


@dataclass(frozen=True, slots=True)
class AnalysisReportRecord:
    report_id: str
    conclusion: str
    evidence_refs: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AccountSnapshotRecord:
    account_id: str
    cash: Decimal
    positions: dict[str, Decimal]
    open_reservations: tuple[str, ...]
    sequence: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    projection_hash: str


@dataclass(frozen=True, slots=True)
class RunCycleResult:
    run_id: str
    status: str
    evidence_id: str
    portfolio_target: PortfolioTargetRecord
    risk_decision: RiskDecisionRecord
    reservation: ReservationRecord | None
    order_intent: OrderIntentRecord | None
    execution_receipt: ExecutionReceiptRecord | None
    journal_transaction: JournalTransactionRecord | None
    report: AnalysisReportRecord
    events: tuple[RunEvent, ...]
    control_hash: str
    projection_hash: str


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _canonical(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value

