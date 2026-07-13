"""Immutable point-in-time valuation, outcome, and reflection contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel, is_quantized
from stonks_agent.domain.ledger import LedgerProjection
from stonks_agent.domain.orders import OrderSide
from stonks_agent.domain.research import ResearchRequest
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.common import (
    ArtifactRef,
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64
_RATIO_QUANTUM = Decimal("0.000000000001")


class PointInTimeMark(TradingModel):
    instrument_id: UUID
    currency: Currency
    price: PositiveDecimal
    event_time: UTCDateTime
    available_at: UTCDateTime
    evidence_id: UUID
    source_artifact_ref: ArtifactRef

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.event_time > self.available_at:
            raise ValueError("mark event time cannot be later than availability")
        return self


class PositionValuation(TradingModel):
    instrument_id: UUID
    quantity: PositiveDecimal
    mark: PointInTimeMark
    market_value: NonNegativeDecimal
    currency_quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.mark.instrument_id != self.instrument_id:
            raise ValueError("position mark identity changed")
        expected = (self.quantity * self.mark.price).quantize(
            self.currency_quantum,
            rounding=ROUND_HALF_EVEN,
        )
        if self.market_value != expected or not is_quantized(
            self.market_value, self.currency_quantum
        ):
            raise ValueError("position market value does not match its mark")
        return self


class PortfolioValuation(TradingModel):
    valuation_id: UUID
    account_id: NonEmptyString
    base_currency: Currency
    as_of: UTCDateTime
    ledger_sequence: int = Field(ge=0)
    ledger_hash: Sha256 | None = None
    ledger_projection_hash: Sha256
    currency_quantum: PositiveDecimal
    cash_value: NonNegativeDecimal
    position_value: NonNegativeDecimal
    nav: NonNegativeDecimal
    cumulative_fees: NonNegativeDecimal
    realized_pnl: DecimalString
    positions: tuple[PositionValuation, ...] = Field(max_length=100_000)
    valuation_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        valuation_id: UUID,
        account_id: str,
        base_currency: str,
        as_of: datetime,
        ledger_sequence: int,
        ledger_hash: str | None,
        ledger_projection_hash: str,
        currency_quantum: Decimal,
        cash_value: Decimal,
        position_value: Decimal,
        nav: Decimal,
        cumulative_fees: Decimal,
        realized_pnl: Decimal,
        positions: tuple[PositionValuation, ...],
    ) -> PortfolioValuation:
        values = {
            "valuation_id": valuation_id,
            "account_id": account_id,
            "base_currency": base_currency,
            "as_of": as_of,
            "ledger_sequence": ledger_sequence,
            "ledger_hash": ledger_hash,
            "ledger_projection_hash": ledger_projection_hash,
            "currency_quantum": currency_quantum,
            "cash_value": cash_value,
            "position_value": position_value,
            "nav": nav,
            "cumulative_fees": cumulative_fees,
            "realized_pnl": realized_pnl,
            "positions": positions,
        }
        provisional = cls.model_construct(
            valuation_id=valuation_id,
            account_id=account_id,
            base_currency=base_currency,
            as_of=as_of,
            ledger_sequence=ledger_sequence,
            ledger_hash=ledger_hash,
            ledger_projection_hash=ledger_projection_hash,
            currency_quantum=currency_quantum,
            cash_value=cash_value,
            position_value=position_value,
            nav=nav,
            cumulative_fees=cumulative_fees,
            realized_pnl=realized_pnl,
            positions=positions,
            valuation_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"valuation_hash": provisional.expected_valuation_hash()}
        )

    @model_validator(mode="after")
    def validate_valuation(self) -> Self:
        if (self.ledger_sequence == 0) != (self.ledger_hash is None):
            raise ValueError("only genesis valuation may omit ledger hash")
        identifiers = tuple(str(item.instrument_id) for item in self.positions)
        if identifiers != tuple(sorted(identifiers)) or len(identifiers) != len(
            set(identifiers)
        ):
            raise ValueError("valued positions must be unique and sorted")
        if any(item.mark.currency != self.base_currency for item in self.positions):
            raise ValueError("position marks must use the base currency")
        if any(item.mark.available_at > self.as_of for item in self.positions):
            raise ValueError("future marks cannot enter a valuation")
        if sum((item.market_value for item in self.positions), Decimal(0)) != (
            self.position_value
        ):
            raise ValueError("position value does not match valued positions")
        if self.cash_value + self.position_value != self.nav:
            raise ValueError("NAV must equal cash plus marked positions")
        monetary = (
            self.cash_value,
            self.position_value,
            self.nav,
            self.cumulative_fees,
            self.realized_pnl,
        )
        if any(not is_quantized(item, self.currency_quantum) for item in monetary):
            raise ValueError("valuation money must match currency quantum")
        if self.valuation_hash != self.expected_valuation_hash():
            raise ValueError("valuation hash does not match payload")
        return self

    def expected_valuation_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(
                mode="json",
                exclude={"valuation_id", "valuation_hash"},
            )
        )


class MarkToMarketCommand(TradingModel):
    valuation_id: UUID
    account_id: NonEmptyString
    base_currency: Currency
    as_of: UTCDateTime
    ledger: LedgerProjection
    marks: tuple[PointInTimeMark, ...] = Field(max_length=100_000)
    currency_quantum: PositiveDecimal


class OutcomeFillReference(TradingModel):
    risk_decision_id: UUID
    risk_decision_hash: Sha256
    account_id: NonEmptyString
    receipt_id: UUID
    receipt_hash: Sha256
    order_intent_id: UUID
    intent_hash: Sha256
    fill_id: UUID
    instrument_id: UUID
    side: OrderSide
    quantity: PositiveDecimal
    price: PositiveDecimal
    fee_currency: Currency
    fees: NonNegativeDecimal
    occurred_at: UTCDateTime


class BuildOutcomeCommand(TradingModel):
    outcome_id: UUID
    decision: RiskDecision
    valuations: tuple[PortfolioValuation, ...] = Field(min_length=2, max_length=10_000)
    benchmark_start: PointInTimeMark
    benchmark_end: PointInTimeMark
    fill_refs: tuple[OutcomeFillReference, ...] = Field(max_length=100_000)
    calculated_at: UTCDateTime


class OutcomeEvidence(TradingModel):
    outcome_id: UUID
    account_id: NonEmptyString
    historical_decision_id: UUID
    historical_decision_hash: Sha256
    historical_target_id: UUID
    historical_target_hash: Sha256
    instrument_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100_000)
    valuations: tuple[PortfolioValuation, ...] = Field(min_length=2, max_length=10_000)
    benchmark_start: PointInTimeMark
    benchmark_end: PointInTimeMark
    raw_return: DecimalString
    benchmark_return: DecimalString
    benchmark_alpha: DecimalString
    max_drawdown: DecimalString
    fee_currency: Currency
    fees: NonNegativeDecimal
    fill_refs: tuple[OutcomeFillReference, ...] = Field(max_length=100_000)
    source_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100_000)
    calculated_at: UTCDateTime
    outcome_hash: Sha256

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        _require_sorted_unique(self.instrument_ids, "outcome instruments")
        _require_sorted_unique(self.source_evidence_ids, "outcome evidence")
        _require_sorted_unique(
            tuple(item.fill_id for item in self.fill_refs), "outcome fills"
        )
        times = tuple(item.as_of for item in self.valuations)
        if times != tuple(sorted(times)) or len(times) != len(set(times)):
            raise ValueError("outcome valuations must be strictly ordered")
        if self.valuations[-1].as_of > self.calculated_at:
            raise ValueError("outcome cannot be calculated before its valuation")
        if not Decimal("-1") <= self.max_drawdown <= 0:
            raise ValueError("outcome drawdown must be between minus one and zero")
        expected_raw = _ratio(self.valuations[-1].nav, self.valuations[0].nav)
        expected_benchmark = _ratio(
            self.benchmark_end.price, self.benchmark_start.price
        )
        if (
            self.raw_return != expected_raw
            or self.benchmark_return != expected_benchmark
            or self.benchmark_alpha != _quantize(expected_raw - expected_benchmark)
            or self.max_drawdown != _drawdown(self.valuations)
        ):
            raise ValueError("outcome performance metrics do not match inputs")
        fee_delta = (
            self.valuations[-1].cumulative_fees - self.valuations[0].cumulative_fees
        )
        if self.fees != fee_delta or self.fees != sum(
            (item.fees for item in self.fill_refs), Decimal(0)
        ):
            raise ValueError("outcome fees do not reconcile with fills and ledger")
        if self.outcome_hash != self.expected_outcome_hash():
            raise ValueError("outcome hash does not match payload")
        return self

    def expected_outcome_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(
                mode="json",
                exclude={"outcome_id", "calculated_at", "outcome_hash"},
            )
        )


class ReflectionContext(TradingModel):
    historical_decision_id: UUID
    historical_decision_hash: Sha256
    outcome_id: UUID
    outcome_hash: Sha256
    outcome_evidence_id: UUID
    outcome_evidence_hash: Sha256
    request: ResearchRequest
    context_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        historical_decision_id: UUID,
        historical_decision_hash: str,
        outcome_id: UUID,
        outcome_hash: str,
        outcome_evidence_id: UUID,
        outcome_evidence_hash: str,
        request: ResearchRequest,
    ) -> ReflectionContext:
        values = {
            "historical_decision_id": historical_decision_id,
            "historical_decision_hash": historical_decision_hash,
            "outcome_id": outcome_id,
            "outcome_hash": outcome_hash,
            "outcome_evidence_id": outcome_evidence_id,
            "outcome_evidence_hash": outcome_evidence_hash,
            "request": request,
        }
        provisional = cls.model_construct(
            historical_decision_id=historical_decision_id,
            historical_decision_hash=historical_decision_hash,
            outcome_id=outcome_id,
            outcome_hash=outcome_hash,
            outcome_evidence_id=outcome_evidence_id,
            outcome_evidence_hash=outcome_evidence_hash,
            request=request,
            context_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"context_hash": provisional.expected_context_hash()}
        )

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if self.request.allowed_evidence_ids != frozenset({self.outcome_evidence_id}):
            raise ValueError("reflection must be scoped to exact outcome evidence")
        if self.context_hash != self.expected_context_hash():
            raise ValueError("reflection context hash does not match payload")
        return self

    def expected_context_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"context_hash"})
        )


def _require_sorted_unique(values: tuple[UUID, ...], label: str) -> None:
    identities = tuple(str(item) for item in values)
    if identities != tuple(sorted(identities)) or len(identities) != len(
        set(identities)
    ):
        raise ValueError(f"{label} must be unique and stably sorted")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def _ratio(end: Decimal, start: Decimal) -> Decimal:
    if start <= 0:
        raise ValueError("return baseline must be positive")
    return _quantize(end / start - 1)


def _drawdown(valuations: tuple[PortfolioValuation, ...]) -> Decimal:
    peak = valuations[0].nav
    if peak <= 0:
        raise ValueError("drawdown baseline must be positive")
    drawdown = Decimal(0)
    for item in valuations:
        peak = max(peak, item.nav)
        drawdown = min(drawdown, item.nav / peak - 1)
    return _quantize(drawdown)
