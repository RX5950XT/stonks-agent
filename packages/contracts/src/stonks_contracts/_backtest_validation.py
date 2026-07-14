"""Cross-model economic validation for canonical backtest results."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from .backtest import (
    BacktestBar,
    BacktestCalendar,
    BacktestCalendarIndex,
    BacktestCostModel,
    BacktestFill,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderOutcome,
    BacktestOrderSide,
    BacktestResult,
    BacktestTimeInForce,
)
from .backtest_math import (
    canonical_fill_fee,
    canonical_fill_price,
    canonical_outcome_status,
    floor_quantum,
)

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
    if any(
        not _outcome_matches_order(outcomes[key], value, job.dataset.as_of)
        for key, value in orders.items()
    ):
        raise ValueError("backtest outcome does not match input order")
    _validate_fills(result, job, orders, bars, instruments, outcomes)
    _validate_projection(result, job, instruments, orders)


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


def _outcome_matches_order(
    outcome: BacktestOrderOutcome,
    order: BacktestOrder,
    as_of: datetime,
) -> bool:
    if outcome.order_hash != order.order_hash or outcome.command_quantity != order.quantity:
        return False
    status, reason = canonical_outcome_status(order, outcome.filled_quantity, as_of)
    return outcome.status is status and outcome.reason == reason


def _validate_fills(
    result: BacktestResult,
    job: BacktestJob,
    orders: dict[UUID, BacktestOrder],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    outcomes: dict[UUID, BacktestOrderOutcome],
) -> None:
    quantities: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    per_bar: defaultdict[UUID, Decimal] = defaultdict(Decimal)
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
        per_bar[fill.source_bar_id] += fill.quantity
        fills_by_order[fill.order_id].append(fill)
    if any(quantities[key] != outcome.filled_quantity for key, outcome in outcomes.items()):
        raise ValueError("backtest outcome fill quantity mismatch")
    _validate_next_bar_sequence(
        fills_by_order,
        orders,
        bars,
        instruments,
        job.dataset.calendar,
        job.cost_model,
    )
    _validate_volume_caps(per_bar, bars, instruments, job.cost_model)


def _validate_next_bar_sequence(
    fills_by_order: dict[UUID, list[BacktestFill]],
    orders: dict[UUID, BacktestOrder],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    calendar: BacktestCalendar,
    cost: BacktestCostModel,
) -> None:
    ordered_bars = tuple(sorted(bars.values(), key=lambda item: (item.opens_at, item.bar_id.hex)))
    expected = _expected_fill_schedules(orders, ordered_bars, instruments, calendar, cost)
    for order_id in orders:
        actual = tuple(
            (fill.source_bar_id, fill.quantity) for fill in fills_by_order.get(order_id, ())
        )
        if actual != expected[order_id]:
            raise ValueError("backtest fills do not match the deterministic schedule")


def _expected_fill_schedules(
    orders: dict[UUID, BacktestOrder],
    bars: tuple[BacktestBar, ...],
    instruments: dict[UUID, BacktestInstrument],
    calendar: BacktestCalendar,
    cost: BacktestCostModel,
) -> dict[UUID, tuple[tuple[UUID, Decimal], ...]]:
    calendar_index = BacktestCalendarIndex.create(calendar)
    remaining = {key: order.quantity for key, order in orders.items()}
    attempted_ioc: set[UUID] = set()
    day_session = {
        order_id: calendar_index.first_session_date(order, instruments[order.instrument_id])
        for order_id, order in orders.items()
        if order.time_in_force is BacktestTimeInForce.DAY
    }
    planned: defaultdict[UUID, list[tuple[UUID, Decimal]]] = defaultdict(list)
    for bar in bars:
        instrument = instruments[bar.instrument_id]
        available = floor_quantum(
            bar.volume * cost.max_volume_participation,
            instrument.quantity_quantum,
        )
        for order_id, order in orders.items():
            if remaining[order_id] <= 0 or order_id in attempted_ioc:
                continue
            if not _is_order_opportunity(order, bar):
                continue
            session = calendar_index.session_for_bar(bar, instrument)
            if session is None:
                raise ValueError("backtest bar session mapping changed")
            if (
                order.time_in_force is BacktestTimeInForce.DAY
                and day_session[order_id] != session.session_date
            ):
                continue
            if order.time_in_force is BacktestTimeInForce.IOC:
                attempted_ioc.add(order_id)
            if not bar.tradable:
                continue
            if canonical_fill_price(order, bar, instrument, cost) is not None:
                quantity = min(remaining[order_id], available)
                if quantity > 0:
                    planned[order_id].append((bar.bar_id, quantity))
                    remaining[order_id] -= quantity
                    available -= quantity
    return {key: tuple(planned[key]) for key in orders}


def _is_order_opportunity(
    order: BacktestOrder,
    bar: BacktestBar,
) -> bool:
    return bool(
        bar.instrument_id == order.instrument_id
        and order.issued_at < bar.opens_at < order.valid_until
    )


def _validate_volume_caps(
    quantities: dict[UUID, Decimal],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
    cost: BacktestCostModel,
) -> None:
    for bar_id, quantity in quantities.items():
        bar = bars[bar_id]
        instrument = instruments[bar.instrument_id]
        cap = floor_quantum(
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
    expected_price = canonical_fill_price(order, bar, instrument, cost)
    expected_fee = canonical_fill_fee(fill.quantity, fill.price, cost)
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


def _validate_projection(
    result: BacktestResult,
    job: BacktestJob,
    instruments: dict[UUID, BacktestInstrument],
    orders: dict[UUID, BacktestOrder],
) -> None:
    cash = {item.currency: item.amount for item in job.initial_cash}
    positions = {item.instrument_id: item.quantity for item in job.initial_positions}
    ordered_fills = sorted(
        result.fills,
        key=lambda item: (item.occurred_at, orders[item.order_id].sequence, item.fill_id.hex),
    )
    for fill in ordered_fills:
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
