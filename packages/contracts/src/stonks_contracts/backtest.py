"""Engine-neutral, replayable contracts for isolated simulation sidecars."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from .common import (
    ArtifactRef,
    ContractModel,
    Currency,
    DecimalString,
    NonEmptyString,
    NonNegativeDecimal,
    PositiveDecimal,
    Sha256,
    UnitDecimal,
    UTCDateTime,
    stable_payload_hash,
)

_BPS = Decimal("10000")
_ZERO = Decimal("0")
_PLACEHOLDER_HASH = "0" * 64


class BacktestEngineKind(StrEnum):
    REFERENCE = "reference"
    NAUTILUS = "nautilus"
    LEAN = "lean"


class BacktestOrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class BacktestOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class BacktestTimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class BacktestOrderStatus(StrEnum):
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class BacktestRuntimeIdentity(ContractModel):
    engine: BacktestEngineKind
    engine_version: NonEmptyString = Field(max_length=128)
    adapter_version: NonEmptyString = Field(max_length=128)
    runtime_hash: Sha256
    image_digest: ArtifactRef | None = None
    deterministic: Literal[True]

    @model_validator(mode="after")
    def require_external_image_provenance(self) -> Self:
        if self.engine is not BacktestEngineKind.REFERENCE and self.image_digest is None:
            raise ValueError("external backtest engine requires image digest")
        return self


class BacktestInstrument(ContractModel):
    instrument_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    asset_class: Literal["equity"]
    currency: Currency
    price_quantum: PositiveDecimal
    quantity_quantum: PositiveDecimal


class BacktestSession(ContractModel):
    session_date: date
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    opens_at: UTCDateTime
    closes_at: UTCDateTime
    break_start: UTCDateTime | None = None
    break_end: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if self.closes_at <= self.opens_at:
            raise ValueError("backtest session close must follow open")
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError("backtest session break must be paired")
        if (
            self.break_start is not None
            and self.break_end is not None
            and not self.opens_at < self.break_start < self.break_end < self.closes_at
        ):
            raise ValueError("backtest session break must be inside the session")
        return self


class BacktestCalendar(ContractModel):
    calendar_id: NonEmptyString = Field(max_length=128)
    version: NonEmptyString = Field(max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    sessions: tuple[BacktestSession, ...] = Field(min_length=1, max_length=100_000)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("backtest timezone must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_sessions(self) -> Self:
        keys = tuple((item.opens_at, item.mic, item.session_date) for item in self.sessions)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("backtest sessions must be unique and stably ordered")
        zone = ZoneInfo(self.timezone)
        if any(
            item.opens_at.astimezone(zone).date() != item.session_date for item in self.sessions
        ):
            raise ValueError("backtest session date does not match calendar timezone")
        for mic in {item.mic for item in self.sessions}:
            scoped = tuple(item for item in self.sessions if item.mic == mic)
            if any(current.opens_at < previous.closes_at for previous, current in pairwise(scoped)):
                raise ValueError("backtest sessions cannot overlap within a market")
        return self

    @property
    def calendar_hash(self) -> str:
        return self.payload_hash()


class BacktestBar(ContractModel):
    bar_id: UUID
    instrument_id: UUID
    opens_at: UTCDateTime
    closes_at: UTCDateTime
    available_at: UTCDateTime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal
    source_ref: NonEmptyString = Field(max_length=512)
    source_hash: Sha256
    tradable: bool

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if not self.opens_at < self.closes_at <= self.available_at:
            raise ValueError("backtest bar timeline is invalid")
        if self.high < self.low or not (
            self.low <= self.open <= self.high and self.low <= self.close <= self.high
        ):
            raise ValueError("backtest bar OHLC is invalid")
        return self


class BacktestDataset(ContractModel):
    dataset_id: UUID
    as_of: UTCDateTime
    interval: NonEmptyString = Field(max_length=32)
    adjustment: Literal["split_dividend_adjusted"]
    instruments: tuple[BacktestInstrument, ...] = Field(min_length=1, max_length=10_000)
    calendar: BacktestCalendar
    bars: tuple[BacktestBar, ...] = Field(min_length=1, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        instrument_keys = tuple(item.instrument_id.hex for item in self.instruments)
        if instrument_keys != tuple(sorted(instrument_keys)) or len(instrument_keys) != len(
            set(instrument_keys)
        ):
            raise ValueError("backtest instruments must be unique and stably ordered")
        bar_keys = tuple(
            (item.opens_at, item.instrument_id.hex, item.bar_id.hex) for item in self.bars
        )
        if bar_keys != tuple(sorted(bar_keys)) or len({item.bar_id for item in self.bars}) != len(
            self.bars
        ):
            raise ValueError("backtest bars must be unique and stably ordered")
        for instrument_id in {item.instrument_id for item in self.bars}:
            scoped = tuple(item for item in self.bars if item.instrument_id == instrument_id)
            if any(current.opens_at < previous.closes_at for previous, current in pairwise(scoped)):
                raise ValueError("backtest bars cannot overlap for an instrument")
        instruments = {item.instrument_id: item for item in self.instruments}
        if any(
            not _bar_is_valid(item, instruments, self.calendar, self.as_of) for item in self.bars
        ):
            raise ValueError("backtest bar failed instrument, calendar, quantum, or PIT validation")
        return self


class BacktestCostModel(ContractModel):
    model_kind: Literal["deterministic_next_bar"]
    realism_claim: Literal["reference_model_not_market_replay"]
    max_volume_participation: UnitDecimal
    half_spread_bps: NonNegativeDecimal
    base_slippage_bps: NonNegativeDecimal
    market_impact_bps_at_max_participation: NonNegativeDecimal
    fee_bps: NonNegativeDecimal
    per_unit_fee: NonNegativeDecimal
    minimum_fee: NonNegativeDecimal
    fee_quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_cost_model(self) -> Self:
        if self.max_volume_participation <= 0:
            raise ValueError("backtest volume participation must be positive")
        bps = (
            self.half_spread_bps,
            self.base_slippage_bps,
            self.market_impact_bps_at_max_participation,
            self.fee_bps,
        )
        if any(item > _BPS for item in bps):
            raise ValueError("backtest basis points cannot exceed 10000")
        if not _is_quantized(self.minimum_fee, self.fee_quantum):
            raise ValueError("backtest minimum fee must match fee quantum")
        return self

    @property
    def cost_model_hash(self) -> str:
        return self.payload_hash()


class BacktestCashBalance(ContractModel):
    currency: Currency
    amount: NonNegativeDecimal
    quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_amount(self) -> Self:
        if not _is_quantized(self.amount, self.quantum):
            raise ValueError("backtest cash must match currency quantum")
        return self


class BacktestPosition(ContractModel):
    instrument_id: UUID
    quantity: NonNegativeDecimal
    quantity_quantum: PositiveDecimal

    @model_validator(mode="after")
    def validate_quantity(self) -> Self:
        if not _is_quantized(self.quantity, self.quantity_quantum):
            raise ValueError("backtest position must match quantity quantum")
        return self


class BacktestOrder(ContractModel):
    order_id: UUID
    sequence: int = Field(ge=1)
    instrument_id: UUID
    side: BacktestOrderSide
    order_type: BacktestOrderType
    quantity: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    time_in_force: BacktestTimeInForce
    issued_at: UTCDateTime
    valid_until: UTCDateTime
    strategy_event_ref: NonEmptyString = Field(max_length=512)
    simulation_only: Literal[True] = True
    order_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        order_id: UUID,
        sequence: int,
        instrument_id: UUID,
        side: BacktestOrderSide,
        order_type: BacktestOrderType,
        quantity: Decimal,
        limit_price: Decimal | None,
        time_in_force: BacktestTimeInForce,
        issued_at: datetime,
        valid_until: datetime,
        strategy_event_ref: str,
    ) -> BacktestOrder:
        values = locals() | {"simulation_only": True}
        values.pop("cls")
        draft = cls.model_construct(**values, order_hash=_PLACEHOLDER_HASH)
        return cls.model_validate(values | {"order_hash": draft.expected_order_hash()})

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.valid_until <= self.issued_at:
            raise ValueError("backtest order validity is invalid")
        if self.order_type is BacktestOrderType.LIMIT and self.limit_price is None:
            raise ValueError("backtest limit order requires limit price")
        if self.order_type is BacktestOrderType.MARKET and self.limit_price is not None:
            raise ValueError("backtest market order cannot include limit price")
        if self.order_hash != self.expected_order_hash():
            raise ValueError("backtest order hash mismatch")
        return self

    def expected_order_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"order_hash"}))


class BacktestJob(ContractModel):
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    runtime: BacktestRuntimeIdentity
    strategy_artifact_ref: ArtifactRef
    strategy_content_hash: Sha256
    dataset_artifact_ref: ArtifactRef
    dataset: BacktestDataset
    cost_model: BacktestCostModel
    orders: tuple[BacktestOrder, ...] = Field(min_length=1, max_length=1_000_000)
    base_currency: Currency = "USD"
    initial_cash: tuple[BacktestCashBalance, ...] = Field(min_length=1, max_length=64)
    initial_positions: tuple[BacktestPosition, ...] = Field(min_length=1, max_length=10_000)
    requested_at: UTCDateTime
    deadline: UTCDateTime
    execution_mode: Literal["backtest"] = "backtest"

    @model_validator(mode="after")
    def validate_job(self) -> Self:
        if self.strategy_artifact_ref != f"sha256:{self.strategy_content_hash}":
            raise ValueError("backtest strategy artifact binding is invalid")
        if self.dataset_artifact_ref != f"sha256:{self.dataset.payload_hash()}":
            raise ValueError("backtest dataset artifact binding is invalid")
        if self.requested_at < self.dataset.as_of or self.deadline <= self.requested_at:
            raise ValueError("backtest request timeline is invalid")
        if not _job_inputs_are_valid(self):
            raise ValueError("backtest order or opening projection is invalid")
        return self

    @property
    def job_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"attempt_nonce"}))

    @property
    def input_hash(self) -> str:
        excluded = {
            "request_id",
            "run_id",
            "job_id",
            "attempt_generation",
            "attempt_nonce",
            "runtime",
            "requested_at",
            "deadline",
        }
        return stable_payload_hash(self.model_dump(mode="json", exclude=excluded))


class BacktestOrderOutcome(ContractModel):
    order_id: UUID
    order_hash: Sha256
    status: BacktestOrderStatus
    command_quantity: PositiveDecimal
    filled_quantity: NonNegativeDecimal
    remaining_quantity: NonNegativeDecimal
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.filled_quantity + self.remaining_quantity != self.command_quantity:
            raise ValueError("backtest outcome quantities must sum to command quantity")
        if self.status is BacktestOrderStatus.FILLED and self.remaining_quantity != 0:
            raise ValueError("filled backtest outcome must have zero remainder")
        if self.status is BacktestOrderStatus.PARTIALLY_FILLED and not (
            0 < self.filled_quantity < self.command_quantity
        ):
            raise ValueError("partial backtest outcome requires a partial quantity")
        if self.status is BacktestOrderStatus.REJECTED and self.filled_quantity != 0:
            raise ValueError("rejected backtest outcome cannot have fills")
        return self


class BacktestFill(ContractModel):
    fill_id: UUID
    order_id: UUID
    order_hash: Sha256
    instrument_id: UUID
    side: BacktestOrderSide
    quantity: PositiveDecimal
    quantity_quantum: PositiveDecimal
    price: PositiveDecimal
    price_quantum: PositiveDecimal
    fee_currency: Currency
    fees: NonNegativeDecimal
    fee_quantum: PositiveDecimal
    slippage: DecimalString
    occurred_at: UTCDateTime
    source_bar_id: UUID
    external_ref: str | None = Field(default=None, max_length=512)
    fill_hash: Sha256

    @classmethod
    def create(cls, **values: object) -> BacktestFill:
        draft = cls.model_construct(
            **values,  # type: ignore[arg-type]
            fill_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(values | {"fill_hash": draft.expected_fill_hash()})

    @model_validator(mode="after")
    def validate_fill(self) -> Self:
        values = (
            (self.quantity, self.quantity_quantum),
            (self.price, self.price_quantum),
            (self.fees, self.fee_quantum),
            (self.slippage, self.price_quantum),
        )
        if any(not _is_quantized(value, quantum) for value, quantum in values):
            raise ValueError("backtest fill values must match their quanta")
        if self.fill_hash != self.expected_fill_hash():
            raise ValueError("backtest fill hash mismatch")
        return self

    def expected_fill_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"fill_hash"}))


class BacktestResult(ContractModel):
    result_id: UUID
    request_id: UUID
    run_id: UUID
    job_id: UUID
    attempt_generation: int = Field(ge=1)
    attempt_nonce: NonEmptyString = Field(max_length=128, repr=False)
    runtime: BacktestRuntimeIdentity
    job_hash: Sha256
    input_hash: Sha256
    dataset_hash: Sha256
    calendar_hash: Sha256
    cost_model_hash: Sha256
    order_outcomes: tuple[BacktestOrderOutcome, ...] = Field(min_length=1, max_length=1_000_000)
    fills: tuple[BacktestFill, ...] = Field(max_length=1_000_000)
    final_cash: tuple[BacktestCashBalance, ...] = Field(min_length=1, max_length=64)
    final_positions: tuple[BacktestPosition, ...] = Field(min_length=1, max_length=10_000)
    total_fees: NonNegativeDecimal
    generated_at: UTCDateTime
    warnings: tuple[str, ...] = Field(default=(), max_length=128)
    semantic_hash: Sha256
    result_hash: Sha256
    execution_mode: Literal["backtest"] = "backtest"

    @classmethod
    def create(
        cls,
        *,
        result_id: UUID,
        job: BacktestJob,
        order_outcomes: tuple[BacktestOrderOutcome, ...],
        fills: tuple[BacktestFill, ...],
        final_cash: tuple[BacktestCashBalance, ...],
        final_positions: tuple[BacktestPosition, ...],
        total_fees: Decimal,
        generated_at: datetime,
        warnings: tuple[str, ...] = (),
    ) -> BacktestResult:
        values = {
            "result_id": result_id,
            "request_id": job.request_id,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "attempt_generation": job.attempt_generation,
            "attempt_nonce": job.attempt_nonce,
            "runtime": job.runtime,
            "job_hash": job.job_hash,
            "input_hash": job.input_hash,
            "dataset_hash": job.dataset.payload_hash(),
            "calendar_hash": job.dataset.calendar.calendar_hash,
            "cost_model_hash": job.cost_model.cost_model_hash,
            "order_outcomes": order_outcomes,
            "fills": fills,
            "final_cash": final_cash,
            "final_positions": final_positions,
            "total_fees": total_fees,
            "generated_at": generated_at,
            "warnings": warnings,
            "execution_mode": "backtest",
        }
        draft = cls.model_construct(
            **values,  # type: ignore[arg-type]
            semantic_hash=_PLACEHOLDER_HASH,
            result_hash=_PLACEHOLDER_HASH,
        )
        with_semantic = values | {"semantic_hash": draft.expected_semantic_hash()}
        result_draft = cls.model_construct(
            **with_semantic,  # type: ignore[arg-type]
            result_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            with_semantic | {"result_hash": result_draft.expected_result_hash()}
        )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        outcome_ids = tuple(item.order_id.hex for item in self.order_outcomes)
        fill_keys = tuple((item.occurred_at, item.fill_id.hex) for item in self.fills)
        cash_keys = tuple(item.currency for item in self.final_cash)
        position_keys = tuple(item.instrument_id.hex for item in self.final_positions)
        stable = (
            outcome_ids == tuple(sorted(outcome_ids))
            and len(outcome_ids) == len(set(outcome_ids))
            and fill_keys == tuple(sorted(fill_keys))
            and len({item.fill_id for item in self.fills}) == len(self.fills)
            and cash_keys == tuple(sorted(cash_keys))
            and len(cash_keys) == len(set(cash_keys))
            and position_keys == tuple(sorted(position_keys))
            and len(position_keys) == len(set(position_keys))
        )
        if not stable:
            raise ValueError("backtest result collections must be unique and stably ordered")
        if sum((item.fees for item in self.fills), _ZERO) != self.total_fees:
            raise ValueError("backtest total fees do not match fills")
        if self.semantic_hash != self.expected_semantic_hash():
            raise ValueError("backtest semantic hash mismatch")
        if self.result_hash != self.expected_result_hash():
            raise ValueError("backtest result hash mismatch")
        return self

    def expected_semantic_hash(self) -> str:
        fills = [
            item.model_dump(mode="json", exclude={"fill_id", "fill_hash", "external_ref"})
            for item in self.fills
        ]
        return stable_payload_hash(
            {
                "input_hash": self.input_hash,
                "order_outcomes": [item.model_dump(mode="json") for item in self.order_outcomes],
                "fills": fills,
                "final_cash": [item.model_dump(mode="json") for item in self.final_cash],
                "final_positions": [item.model_dump(mode="json") for item in self.final_positions],
                "total_fees": str(self.total_fees),
            }
        )

    def expected_result_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json", exclude={"result_hash"}))

    def validate_against(self, job: BacktestJob) -> None:
        from ._backtest_validation import validate_backtest_result

        validate_backtest_result(self, job)


def _bar_is_valid(
    bar: BacktestBar,
    instruments: dict[UUID, BacktestInstrument],
    calendar: BacktestCalendar,
    as_of: datetime,
) -> bool:
    instrument = instruments.get(bar.instrument_id)
    if instrument is None or bar.available_at > as_of:
        return False
    if not all(
        _is_quantized(value, instrument.price_quantum)
        for value in (bar.open, bar.high, bar.low, bar.close)
    ) or not _is_quantized(bar.volume, instrument.quantity_quantum):
        return False
    matches = [
        session
        for session in calendar.sessions
        if session.mic == instrument.mic
        and session.opens_at <= bar.opens_at < bar.closes_at <= session.closes_at
    ]
    if len(matches) != 1:
        return False
    session = matches[0]
    if session.break_start is None or session.break_end is None:
        return True
    return bar.closes_at <= session.break_start or bar.opens_at >= session.break_end


def _job_inputs_are_valid(job: BacktestJob) -> bool:
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    order_keys = tuple((item.issued_at, item.sequence, item.order_id.hex) for item in job.orders)
    expected_sequences = tuple(range(1, len(job.orders) + 1))
    cash_keys = tuple(item.currency for item in job.initial_cash)
    position_keys = tuple(item.instrument_id.hex for item in job.initial_positions)
    return bool(
        order_keys == tuple(sorted(order_keys))
        and tuple(item.sequence for item in job.orders) == expected_sequences
        and len({item.order_id for item in job.orders}) == len(job.orders)
        and all(
            _order_matches_instrument(item, instruments.get(item.instrument_id))
            for item in job.orders
        )
        and cash_keys == (job.base_currency,)
        and position_keys == tuple(sorted(item.hex for item in instruments))
        and all(
            item.quantity_quantum == instruments[item.instrument_id].quantity_quantum
            for item in job.initial_positions
        )
        and all(item.currency == job.base_currency for item in job.dataset.instruments)
    )


def _order_matches_instrument(order: BacktestOrder, instrument: BacktestInstrument | None) -> bool:
    return bool(
        instrument is not None
        and _is_quantized(order.quantity, instrument.quantity_quantum)
        and (
            order.limit_price is None or _is_quantized(order.limit_price, instrument.price_quantum)
        )
    )


def _is_quantized(value: Decimal, quantum: Decimal) -> bool:
    return value % quantum == 0
