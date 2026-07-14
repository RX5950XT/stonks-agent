"""NautilusTrader 1.230.0 runtime implementation for canonical backtests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib.metadata import version
from typing import Any
from uuid import UUID

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import (  # type: ignore[import-not-found]
    BacktestEngine,
)
from nautilus_trader.config import LoggingConfig, RiskEngineConfig, StrategyConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos, unix_nanos_to_dt
from nautilus_trader.model import Currency
from nautilus_trader.model.data import (  # type: ignore[import-not-found]
    Bar,
    BarSpecification,
    BarType,
    QuoteTick,
)
from nautilus_trader.model.enums import (
    AccountType,
    AggregationSource,
    BarAggregation,
    OmsType,
    OrderSide,
    OrderStatus,
    PriceType,
    TimeInForce,
)
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
    ClientOrderId,
    InstrumentId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import (  # type: ignore[import-not-found]
    Money,
    Price,
    Quantity,
)
from nautilus_trader.trading.strategy import Strategy  # type: ignore[import-not-found]

from sidecars.nautilus.adapter import (
    EngineFailure,
    EngineFillTrace,
    EngineOrderTrace,
    EngineRunTrace,
)
from stonks_contracts.backtest import (
    BacktestBar,
    BacktestCalendarIndex,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestTimeInForce,
)
from stonks_contracts.backtest_math import canonical_fill_price, floor_quantum

_SUPPORTED_VERSION = "1.230.0"
_INTERVAL = re.compile(r"^([1-9][0-9]*)([mhd])$")


class _UnsupportedFillTimingError(ValueError):
    pass


class _ScheduleTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduledChild:
    canonical_order_id: UUID
    client_order_id: str
    instrument_id: InstrumentId
    side: BacktestOrderSide
    order_type: BacktestOrderType
    time_in_force: BacktestTimeInForce
    quantity: Decimal
    limit_price: Decimal | None
    source_bar_id: UUID
    source_opens_at_ns: int


class ScheduledOrderStrategy(Strategy):  # type: ignore[misc]
    """Submit bounded engine children only at their canonical source-bar open."""

    def __init__(self, schedule: tuple[ScheduledChild, ...]) -> None:
        super().__init__(StrategyConfig(strategy_id="SCHEDULED-001"))
        grouped: defaultdict[tuple[str, int], list[ScheduledChild]] = defaultdict(list)
        for child in schedule:
            grouped[(str(child.instrument_id), child.source_opens_at_ns)].append(child)
        self.schedule = {
            key: tuple(sorted(value, key=lambda item: item.client_order_id))
            for key, value in grouped.items()
        }
        self.children = {item.client_order_id: item for item in schedule}

    def on_start(self) -> None:
        for instrument_id in sorted(
            {item.instrument_id for item in self.children.values()}, key=str
        ):
            self.subscribe_quote_ticks(instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        key = (str(tick.instrument_id), tick.ts_event)
        for child in self.schedule.get(key, ()):
            self.submit_order(self._make_order(child))

    def _make_order(self, child: ScheduledChild) -> Any:
        common = {
            "instrument_id": child.instrument_id,
            "order_side": _order_side(child.side),
            "quantity": Quantity.from_decimal(child.quantity),
            "time_in_force": _time_in_force(child.time_in_force),
            "client_order_id": ClientOrderId(child.client_order_id),
        }
        if child.order_type is BacktestOrderType.MARKET:
            return self.order_factory.market(**common)
        if child.limit_price is None:
            raise ValueError("scheduled limit child has no limit")
        return self.order_factory.limit(
            **common,
            price=Price.from_decimal(child.limit_price),
        )


class NautilusEngineBackend:
    """Run one fresh Nautilus engine per job and return authority-free traces."""

    def __init__(self, *, max_schedule_children: int = 100_000) -> None:
        if max_schedule_children < 1:
            raise ValueError("max_schedule_children must be positive")
        self.max_schedule_children = max_schedule_children
        self.engine_version = version("nautilus_trader")
        if not _is_compatible_version(self.engine_version):
            raise RuntimeError("unexpected NautilusTrader version")

    def run(self, job: BacktestJob) -> EngineRunTrace | EngineFailure:
        if job.runtime.engine_version != self.engine_version:
            return EngineFailure(
                code="runtime_mismatch",
                message="Nautilus engine version changed",
            )
        engine: BacktestEngine | None = None
        try:
            interval = _bar_specification(job.dataset.interval)
            instrument_ids = _instrument_ids(job)
            schedule = _schedule(job, instrument_ids, self.max_schedule_children)
            engine = _build_engine(job, interval, instrument_ids, schedule)
            engine.run()
            return _extract_trace(job, engine, schedule)
        except _UnsupportedFillTimingError:
            return EngineFailure(
                code="engine_unsupported_fill_timing",
                message="Nautilus fill did not occur at the canonical bar open",
            )
        except _ScheduleTooLargeError:
            return EngineFailure(
                code="job_too_large",
                message="Nautilus schedule exceeds configured limits",
            )
        except (KeyError, RuntimeError, TypeError, ValueError):
            return EngineFailure(
                code="engine_invalid_job",
                message="Nautilus engine rejected the canonical job",
            )
        finally:
            if engine is not None:
                engine.dispose()


def _build_engine(
    job: BacktestJob,
    interval: BarSpecification,
    instrument_ids: dict[UUID, InstrumentId],
    schedule: tuple[ScheduledChild, ...],
) -> BacktestEngine:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )
    currency = Currency.from_str(job.base_currency)
    opening_cash = job.initial_cash[0].amount
    for mic in sorted({item.mic for item in job.dataset.instruments}):
        engine.add_venue(
            venue=Venue(mic),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_decimal(opening_cash, currency)],
            base_currency=currency,
            default_leverage=Decimal("1"),
            bar_execution=True,
            use_random_ids=False,
        )
    for item in job.dataset.instruments:
        engine.add_instrument(_equity(item, instrument_ids[item.instrument_id]))
    engine.add_data(_engine_data(job, interval, instrument_ids), sort=True)
    engine.add_strategy(ScheduledOrderStrategy(schedule))
    return engine


def _instrument_ids(job: BacktestJob) -> dict[UUID, InstrumentId]:
    return {
        item.instrument_id: InstrumentId.from_str(f"{item.symbol}.{item.mic}")
        for item in job.dataset.instruments
    }


def _equity(item: BacktestInstrument, instrument_id: InstrumentId) -> Equity:
    return Equity(
        instrument_id=instrument_id,
        raw_symbol=Symbol(item.symbol),
        currency=Currency.from_str(item.currency),
        price_precision=_precision(item.price_quantum),
        price_increment=Price.from_decimal(item.price_quantum),
        lot_size=Quantity.from_decimal(item.quantity_quantum),
        ts_event=0,
        ts_init=0,
    )


def _engine_data(
    job: BacktestJob,
    interval: BarSpecification,
    instrument_ids: dict[UUID, InstrumentId],
) -> list[Bar | QuoteTick]:
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    result: list[Bar | QuoteTick] = []
    for item in job.dataset.bars:
        instrument = instruments[item.instrument_id]
        instrument_id = instrument_ids[item.instrument_id]
        bar_type = BarType(instrument_id, interval, AggregationSource.EXTERNAL)
        price_precision = _precision(instrument.price_quantum)
        volume = max(item.volume, instrument.quantity_quantum)
        quantity = Quantity.from_str(
            _fixed(volume, _precision(instrument.quantity_quantum))
        )
        result.append(
            QuoteTick(
                instrument_id,
                _price(item.open, price_precision),
                _price(item.open, price_precision),
                quantity,
                quantity,
                dt_to_unix_nanos(item.opens_at),
                dt_to_unix_nanos(item.opens_at),
            )
        )
        result.append(
            Bar(
                bar_type,
                _price(item.open, price_precision),
                _price(item.high, price_precision),
                _price(item.low, price_precision),
                _price(item.close, price_precision),
                quantity,
                dt_to_unix_nanos(item.closes_at),
                dt_to_unix_nanos(item.available_at),
            )
        )
    return result


def _schedule(
    job: BacktestJob,
    instrument_ids: dict[UUID, InstrumentId],
    max_children: int,
) -> tuple[ScheduledChild, ...]:
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    calendar_index = BacktestCalendarIndex.create(job.dataset.calendar)
    bars = tuple(
        sorted(job.dataset.bars, key=lambda item: (item.opens_at, item.bar_id.hex))
    )
    remaining = {item.order_id: item.quantity for item in job.orders}
    attempted_ioc: set[UUID] = set()
    day_session = {
        order.order_id: calendar_index.first_session_date(
            order, instruments[order.instrument_id]
        )
        for order in job.orders
        if order.time_in_force is BacktestTimeInForce.DAY
    }
    child_counts: defaultdict[UUID, int] = defaultdict(int)
    result: list[ScheduledChild] = []
    for bar in bars:
        instrument = instruments[bar.instrument_id]
        available = floor_quantum(
            bar.volume * job.cost_model.max_volume_participation,
            instrument.quantity_quantum,
        )
        for order in job.orders:
            if remaining[order.order_id] <= 0 or order.order_id in attempted_ioc:
                continue
            if not _is_order_opportunity(order, bar):
                continue
            session = calendar_index.session_for_bar(bar, instrument)
            if session is None:
                raise ValueError("Nautilus bar session mapping changed")
            if (
                order.time_in_force is BacktestTimeInForce.DAY
                and day_session[order.order_id] != session.session_date
            ):
                continue
            if order.time_in_force is BacktestTimeInForce.IOC:
                attempted_ioc.add(order.order_id)
            if not bar.tradable:
                continue
            if canonical_fill_price(order, bar, instrument, job.cost_model) is not None:
                quantity = min(remaining[order.order_id], available)
                if quantity > 0:
                    if len(result) >= max_children:
                        raise _ScheduleTooLargeError
                    child_counts[order.order_id] += 1
                    result.append(
                        _scheduled_child(
                            order,
                            bar,
                            instrument_ids[order.instrument_id],
                            quantity,
                            child_counts[order.order_id],
                        )
                    )
                    remaining[order.order_id] -= quantity
                    available -= quantity
    return tuple(
        sorted(result, key=lambda item: (item.source_opens_at_ns, item.client_order_id))
    )


def _is_order_opportunity(
    order: BacktestOrder,
    bar: BacktestBar,
) -> bool:
    return bool(
        bar.instrument_id == order.instrument_id
        and order.issued_at < bar.opens_at < order.valid_until
    )


def _scheduled_child(
    order: BacktestOrder,
    bar: BacktestBar,
    instrument_id: InstrumentId,
    quantity: Decimal,
    index: int,
) -> ScheduledChild:
    return ScheduledChild(
        canonical_order_id=order.order_id,
        client_order_id=f"B{order.order_id.hex}-{index:07d}",
        instrument_id=instrument_id,
        side=order.side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        quantity=quantity,
        limit_price=order.limit_price,
        source_bar_id=bar.bar_id,
        source_opens_at_ns=dt_to_unix_nanos(bar.opens_at),
    )


def _extract_trace(
    job: BacktestJob,
    engine: BacktestEngine,
    schedule: tuple[ScheduledChild, ...],
) -> EngineRunTrace:
    child_by_id = {item.client_order_id: item for item in schedule}
    fills = []
    statuses: defaultdict[UUID, list[OrderStatus]] = defaultdict(list)
    for engine_order in engine.cache.orders():
        child = child_by_id.get(str(engine_order.client_order_id))
        if child is None:
            continue
        statuses[child.canonical_order_id].append(engine_order.status)
        for event in engine_order.events:
            if isinstance(event, OrderFilled):
                fills.append(_fill_trace(child, event))
    totals: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    for fill in fills:
        totals[fill.order_id] += fill.quantity
    orders = tuple(
        _order_trace(
            item, totals[item.order_id], statuses[item.order_id], job.dataset.as_of
        )
        for item in job.orders
    )
    return EngineRunTrace(
        orders=tuple(sorted(orders, key=lambda item: item.order_id.hex)),
        fills=tuple(
            sorted(fills, key=lambda item: (item.occurred_at, item.external_fill_id))
        ),
    )


def _fill_trace(child: ScheduledChild, event: OrderFilled) -> EngineFillTrace:
    if event.ts_event != child.source_opens_at_ns:
        raise _UnsupportedFillTimingError
    raw = OrderFilled.to_dict(event)
    raw.pop("event_id", None)
    encoded = json.dumps(raw, default=str, sort_keys=True, separators=(",", ":"))
    return EngineFillTrace(
        external_fill_id=str(event.trade_id),
        order_id=child.canonical_order_id,
        quantity=event.last_qty.as_decimal(),
        raw_price=event.last_px.as_decimal(),
        raw_fees=event.commission.as_decimal(),
        raw_event_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        occurred_at=unix_nanos_to_dt(child.source_opens_at_ns),
        source_bar_id=child.source_bar_id,
    )


def _order_trace(
    order: BacktestOrder,
    filled: Decimal,
    child_statuses: list[OrderStatus],
    as_of: datetime,
) -> EngineOrderTrace:
    if filled == order.quantity:
        status = BacktestOrderStatus.FILLED
        reason = None
    elif filled > 0:
        status = BacktestOrderStatus.PARTIALLY_FILLED
        reason = "nautilus_partial_fill"
    elif any(item is OrderStatus.REJECTED for item in child_statuses):
        status = BacktestOrderStatus.REJECTED
        reason = "nautilus_child_rejected"
    elif order.time_in_force is BacktestTimeInForce.IOC:
        status = BacktestOrderStatus.CANCELLED
        reason = "nautilus_ioc_unfilled"
    elif as_of >= order.valid_until:
        status = BacktestOrderStatus.EXPIRED
        reason = "nautilus_validity_expired"
    else:
        status = BacktestOrderStatus.ACCEPTED
        reason = "nautilus_pending"
    return EngineOrderTrace(order_id=order.order_id, status=status, reason=reason)


def _bar_specification(value: str) -> BarSpecification:
    match = _INTERVAL.fullmatch(value)
    if match is None:
        raise ValueError("unsupported Nautilus bar interval")
    aggregation = {
        "m": BarAggregation.MINUTE,
        "h": BarAggregation.HOUR,
        "d": BarAggregation.DAY,
    }[match.group(2)]
    return BarSpecification(int(match.group(1)), aggregation, PriceType.LAST)


def _precision(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("Nautilus quantum must be finite")
    return max(0, -exponent)


def _fixed(value: Decimal, precision: int) -> str:
    return format(value, f".{precision}f")


def _price(value: Decimal, precision: int) -> Price:
    return Price.from_str(_fixed(value, precision))


def _order_side(value: BacktestOrderSide) -> OrderSide:
    return OrderSide.BUY if value is BacktestOrderSide.BUY else OrderSide.SELL


def _time_in_force(value: BacktestTimeInForce) -> TimeInForce:
    return {
        BacktestTimeInForce.DAY: TimeInForce.DAY,
        BacktestTimeInForce.GTC: TimeInForce.GTC,
        BacktestTimeInForce.IOC: TimeInForce.IOC,
    }[value]


def _is_compatible_version(value: str) -> bool:
    return value == _SUPPORTED_VERSION or value.startswith(f"{_SUPPORTED_VERSION}+")
