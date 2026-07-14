"""Cross-model economic validation for canonical backtest results."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import UUID

from .backtest import (
    BacktestBar,
    BacktestCostModel,
    BacktestFill,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderOutcome,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestResult,
)

_BPS = Decimal("10000")
_ZERO = Decimal("0")


def validate_backtest_result(result: BacktestResult, job: BacktestJob) -> None:
    if not _result_binding_matches(result, job):
        raise ValueError("backtest result job or attempt binding changed")
    orders = {item.order_id: item for item in job.orders}
    bars = {item.bar_id: item for item in job.dataset.bars}
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    outcomes = {item.order_id: item for item in result.order_outcomes}
    if set(outcomes) != set(orders):
        raise ValueError("backtest result must cover every input order exactly once")
    if any(not _outcome_matches_order(outcomes[key], value) for key, value in orders.items()):
        raise ValueError("backtest outcome does not match input order")
    _validate_fills(result, job, orders, bars, instruments, outcomes)
    _validate_projection(result, job, instruments)


def _result_binding_matches(result: BacktestResult, job: BacktestJob) -> bool:
    return bool(
        result.request_id == job.request_id
        and result.run_id == job.run_id
        and result.job_id == job.job_id
        and result.attempt_generation == job.attempt_generation
        and result.attempt_nonce == job.attempt_nonce
        and result.runtime == job.runtime
        and result.job_hash == job.job_hash
        and result.input_hash == job.input_hash
        and result.dataset_hash == job.dataset.payload_hash()
        and result.calendar_hash == job.dataset.calendar.calendar_hash
        and result.cost_model_hash == job.cost_model.cost_model_hash
        and job.requested_at <= result.generated_at <= job.deadline
    )


def _outcome_matches_order(outcome: BacktestOrderOutcome, order: BacktestOrder) -> bool:
    if outcome.order_hash != order.order_hash or outcome.command_quantity != order.quantity:
        return False
    if outcome.filled_quantity == outcome.command_quantity:
        return outcome.status is BacktestOrderStatus.FILLED
    return outcome.status is not BacktestOrderStatus.FILLED


def _validate_fills(
    result: BacktestResult,
    job: BacktestJob,
    orders: dict[UUID, BacktestOrder],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    outcomes: dict[UUID, BacktestOrderOutcome],
) -> None:
    quantities: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    per_bar_order: defaultdict[tuple[UUID, UUID], Decimal] = defaultdict(Decimal)
    fills_by_order: defaultdict[UUID, list[BacktestFill]] = defaultdict(list)
    for fill in result.fills:
        order = orders.get(fill.order_id)
        bar = bars.get(fill.source_bar_id)
        instrument = instruments.get(fill.instrument_id)
        if order is None or bar is None or instrument is None:
            raise ValueError("backtest fill refers to unknown input")
        if not _fill_matches_input(fill, order, bar, instrument, job.cost_model):
            raise ValueError("backtest fill economics or next-bar binding changed")
        quantities[fill.order_id] += fill.quantity
        per_bar_order[(fill.source_bar_id, fill.order_id)] += fill.quantity
        fills_by_order[fill.order_id].append(fill)
    if any(quantities[key] != outcome.filled_quantity for key, outcome in outcomes.items()):
        raise ValueError("backtest outcome fill quantity mismatch")
    _validate_next_bar_sequence(fills_by_order, orders, bars, instruments, job.cost_model)
    _validate_volume_caps(per_bar_order, bars, instruments, job.cost_model)


def _validate_next_bar_sequence(
    fills_by_order: dict[UUID, list[BacktestFill]],
    orders: dict[UUID, BacktestOrder],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    cost: BacktestCostModel,
) -> None:
    ordered_bars = tuple(sorted(bars.values(), key=lambda item: (item.opens_at, item.bar_id.hex)))
    for order_id, fills in fills_by_order.items():
        order = orders[order_id]
        instrument = instruments[order.instrument_id]
        cursor = order.issued_at
        for fill in fills:
            expected = _next_fillable_bar(order, ordered_bars, instrument, cost, cursor)
            if expected is None or fill.source_bar_id != expected.bar_id:
                raise ValueError("backtest fill skipped the next fillable bar")
            cursor = expected.opens_at


def _next_fillable_bar(
    order: BacktestOrder,
    bars: tuple[BacktestBar, ...],
    instrument: BacktestInstrument,
    cost: BacktestCostModel,
    cursor: datetime,
) -> BacktestBar | None:
    return next(
        (
            bar
            for bar in bars
            if bar.instrument_id == order.instrument_id
            and bar.tradable
            and bar.opens_at > cursor
            and bar.opens_at < order.valid_until
            and _expected_price(order, bar, instrument.price_quantum, cost) is not None
        ),
        None,
    )


def _validate_volume_caps(
    quantities: dict[tuple[UUID, UUID], Decimal],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    cost: BacktestCostModel,
) -> None:
    for (bar_id, _), quantity in quantities.items():
        bar = bars[bar_id]
        instrument = instruments[bar.instrument_id]
        cap = _floor_quantum(
            bar.volume * cost.max_volume_participation,
            instrument.quantity_quantum,
        )
        if quantity > cap:
            raise ValueError("backtest fills exceed source bar participation")


def _fill_matches_input(
    fill: BacktestFill,
    order: BacktestOrder,
    bar: BacktestBar,
    instrument: BacktestInstrument,
    cost: BacktestCostModel,
) -> bool:
    expected_price = _expected_price(order, bar, instrument.price_quantum, cost)
    expected_fee = _expected_fee(fill.quantity, fill.price, cost)
    return bool(
        bar.tradable
        and fill.order_hash == order.order_hash
        and fill.instrument_id == order.instrument_id == bar.instrument_id
        and fill.side is order.side
        and fill.quantity_quantum == instrument.quantity_quantum
        and fill.price_quantum == instrument.price_quantum
        and fill.fee_currency == instrument.currency
        and fill.fee_quantum == cost.fee_quantum
        and bar.opens_at > order.issued_at
        and bar.opens_at < order.valid_until
        and fill.occurred_at == bar.opens_at
        and fill.quantity <= order.quantity
        and fill.price == expected_price
        and fill.slippage == fill.price - bar.open
        and fill.fees == expected_fee
    )


def _expected_price(
    order: BacktestOrder,
    bar: BacktestBar,
    price_quantum: Decimal,
    cost: BacktestCostModel,
) -> Decimal | None:
    participation = min(
        cost.max_volume_participation,
        order.quantity / bar.volume if bar.volume else _ZERO,
    )
    impact = (
        cost.market_impact_bps_at_max_participation * participation / cost.max_volume_participation
    )
    adverse_bps = cost.half_spread_bps + cost.base_slippage_bps + impact
    direction = Decimal("1") if order.side is BacktestOrderSide.BUY else Decimal("-1")
    adjusted = bar.open * (Decimal("1") + direction * adverse_bps / _BPS)
    adjusted = _adverse_quantize(adjusted, price_quantum, order.side)
    if order.order_type is BacktestOrderType.MARKET:
        return adjusted
    limit = order.limit_price
    if limit is None:
        return None
    if order.side is BacktestOrderSide.BUY:
        if bar.open <= limit:
            return min(adjusted, limit)
        return limit if bar.low <= limit else None
    if bar.open >= limit:
        return max(adjusted, limit)
    return limit if bar.high >= limit else None


def _expected_fee(quantity: Decimal, price: Decimal, cost: BacktestCostModel) -> Decimal:
    raw = quantity * price * cost.fee_bps / _BPS + quantity * cost.per_unit_fee
    return _ceil_quantum(max(raw, cost.minimum_fee), cost.fee_quantum)


def _validate_projection(
    result: BacktestResult,
    job: BacktestJob,
    instruments: dict[UUID, BacktestInstrument],
) -> None:
    cash = {item.currency: item.amount for item in job.initial_cash}
    positions = {item.instrument_id: item.quantity for item in job.initial_positions}
    for fill in result.fills:
        notional = fill.quantity * fill.price
        if fill.side is BacktestOrderSide.BUY:
            cash[fill.fee_currency] -= notional + fill.fees
            positions[fill.instrument_id] += fill.quantity
        else:
            cash[fill.fee_currency] += notional - fill.fees
            positions[fill.instrument_id] -= fill.quantity
        if cash[fill.fee_currency] < 0 or positions[fill.instrument_id] < 0:
            raise ValueError("backtest fill creates negative cash or position")
    final_cash = {item.currency: item.amount for item in result.final_cash}
    final_positions = {item.instrument_id: item.quantity for item in result.final_positions}
    if cash != final_cash or positions != final_positions:
        raise ValueError("backtest final cash or position projection mismatch")
    if set(final_positions) != set(instruments):
        raise ValueError("backtest final positions must cover the dataset universe")
    opening_cash = {item.currency: item for item in job.initial_cash}
    if any(item.quantum != opening_cash[item.currency].quantum for item in result.final_cash):
        raise ValueError("backtest final cash quantum changed")
    if any(
        item.quantity_quantum != instruments[item.instrument_id].quantity_quantum
        for item in result.final_positions
    ):
        raise ValueError("backtest final position quantum changed")


def _floor_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


def _ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _adverse_quantize(value: Decimal, quantum: Decimal, side: BacktestOrderSide) -> Decimal:
    rounding = ROUND_CEILING if side is BacktestOrderSide.BUY else ROUND_FLOOR
    return (value / quantum).to_integral_value(rounding=rounding) * quantum
