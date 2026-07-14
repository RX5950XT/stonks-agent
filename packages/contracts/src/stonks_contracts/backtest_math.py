"""Public deterministic economics shared by canonical backtest adapters."""

from __future__ import annotations

from datetime import datetime
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    localcontext,
)

from .backtest import (
    BacktestBar,
    BacktestCostModel,
    BacktestInstrument,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestOrderType,
    BacktestTimeInForce,
)

_BPS = Decimal("10000")
_ZERO = Decimal("0")
_CONTEXT = Context(prec=128, rounding=ROUND_HALF_EVEN, Emin=-128, Emax=128)
_MAX_DIGITS = 64
_MAX_ADJUSTED_EXPONENT = 64


def canonical_fill_price(
    order: BacktestOrder,
    bar: BacktestBar,
    instrument: BacktestInstrument,
    cost: BacktestCostModel,
) -> Decimal | None:
    """Return the reference next-bar open price, or ``None`` if a limit does not cross."""

    operands = (
        order.quantity,
        bar.open,
        bar.volume,
        instrument.price_quantum,
        cost.max_volume_participation,
        cost.half_spread_bps,
        cost.base_slippage_bps,
        cost.market_impact_bps_at_max_participation,
    )
    _require_bounded(*operands)
    with localcontext(_CONTEXT):
        participation = min(
            cost.max_volume_participation,
            order.quantity / bar.volume if bar.volume else _ZERO,
        )
        impact = (
            cost.market_impact_bps_at_max_participation
            * participation
            / cost.max_volume_participation
        )
        adverse_bps = cost.half_spread_bps + cost.base_slippage_bps + impact
        direction = Decimal("1") if order.side is BacktestOrderSide.BUY else Decimal("-1")
        adjusted = bar.open * (Decimal("1") + direction * adverse_bps / _BPS)
        adjusted = _adverse_quantize(adjusted, instrument.price_quantum, order.side)
        if adjusted <= 0:
            raise ValueError("canonical fill must have a positive price")
        if order.order_type is BacktestOrderType.MARKET:
            return adjusted
        limit = order.limit_price
        if limit is None:
            return None
        _require_bounded(limit)
        if order.side is BacktestOrderSide.BUY:
            return min(adjusted, limit) if bar.open <= limit else None
        return max(adjusted, limit) if bar.open >= limit else None


def canonical_fill_fee(
    quantity: Decimal,
    price: Decimal,
    cost: BacktestCostModel,
) -> Decimal:
    """Return the reference fee rounded adversely to the configured fee quantum."""

    _require_bounded(
        quantity,
        price,
        cost.fee_bps,
        cost.per_unit_fee,
        cost.minimum_fee,
        cost.fee_quantum,
    )
    with localcontext(_CONTEXT):
        raw = quantity * price * (cost.fee_bps / _BPS) + quantity * cost.per_unit_fee
        _require_bounded(raw)
        return ceil_quantum(max(raw, cost.minimum_fee), cost.fee_quantum)


def canonical_outcome_status(
    order: BacktestOrder,
    filled: Decimal,
    as_of: datetime,
) -> tuple[BacktestOrderStatus, str | None]:
    """Return the unique engine-neutral status and reason for an order result."""

    if filled == order.quantity:
        return BacktestOrderStatus.FILLED, None
    if filled > 0:
        return BacktestOrderStatus.PARTIALLY_FILLED, "canonical_partial_fill"
    if order.time_in_force is BacktestTimeInForce.IOC:
        return BacktestOrderStatus.CANCELLED, "canonical_ioc_unfilled"
    if order.time_in_force is BacktestTimeInForce.DAY:
        return BacktestOrderStatus.CANCELLED, "canonical_day_unfilled"
    if as_of >= order.valid_until:
        return BacktestOrderStatus.EXPIRED, "canonical_validity_expired"
    return BacktestOrderStatus.ACCEPTED, "canonical_pending"


def floor_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    """Round a non-negative value down to an exact quantum."""

    _require_bounded(value, quantum)
    with localcontext(_CONTEXT):
        return (value / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum


def ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    """Round a non-negative value up to an exact quantum."""

    _require_bounded(value, quantum)
    with localcontext(_CONTEXT):
        return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _adverse_quantize(
    value: Decimal,
    quantum: Decimal,
    side: BacktestOrderSide,
) -> Decimal:
    rounding = ROUND_CEILING if side is BacktestOrderSide.BUY else ROUND_FLOOR
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


def _require_bounded(*values: Decimal) -> None:
    for value in values:
        parts = value.as_tuple()
        if len(parts.digits) > _MAX_DIGITS or abs(value.adjusted()) > _MAX_ADJUSTED_EXPONENT:
            raise ValueError("canonical backtest decimal exceeds supported bounds")
