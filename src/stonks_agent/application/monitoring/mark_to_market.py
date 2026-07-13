"""Value a settled paper ledger from exact point-in-time marks."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from pydantic import ValidationError

from stonks_agent.domain._trading import failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.monitoring import (
    MarkToMarketCommand,
    PortfolioValuation,
    PositionValuation,
)


def mark_to_market(command: MarkToMarketCommand) -> Result[PortfolioValuation]:
    ledger = command.ledger
    if ledger.account_id != command.account_id:
        return failure(ErrorCode.CONFLICT, "Valuation account binding changed")
    if ledger.last_occurred_at > command.as_of:
        return failure(ErrorCode.CONFLICT, "Future ledger state cannot be valued")
    currency_error = _currency_error(command)
    if currency_error is not None:
        return currency_error
    quantities = _open_quantities(command)
    if isinstance(quantities, Failure):
        return quantities
    marks = {item.instrument_id: item for item in command.marks}
    if len(marks) != len(command.marks) or set(marks) != set(quantities.value):
        return failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Valuation requires one exact mark for every open position",
        )
    positions: list[PositionValuation] = []
    for instrument_id, quantity in sorted(
        quantities.value.items(), key=lambda x: str(x[0])
    ):
        mark = marks[instrument_id]
        if mark.currency != command.base_currency or mark.available_at > command.as_of:
            return failure(ErrorCode.CONFLICT, "Valuation mark is future or foreign")
        market_value = _money(quantity * mark.price, command.currency_quantum)
        try:
            positions.append(
                PositionValuation(
                    instrument_id=instrument_id,
                    quantity=quantity,
                    mark=mark,
                    market_value=market_value,
                    currency_quantum=command.currency_quantum,
                )
            )
        except ValidationError:
            return failure(ErrorCode.INVALID_INPUT, "Valuation mark is invalid")
    return _build_valuation(command, tuple(positions))


def _currency_error(command: MarkToMarketCommand) -> Failure | None:
    for item in command.ledger.balances:
        relevant = item.ledger_account.startswith(
            ("asset:cash:", "fee:execution:", "pnl:realized:")
        )
        if relevant and item.balance != 0 and item.commodity != command.base_currency:
            return failure(
                ErrorCode.CONFLICT,
                "Cross-currency valuation requires an approved FX policy",
            )
    return None


def _open_quantities(
    command: MarkToMarketCommand,
) -> Result[dict[UUID, Decimal]]:
    values: dict[UUID, Decimal] = {}
    for item in command.ledger.balances:
        prefix = "inventory:units:"
        if not item.ledger_account.startswith(prefix) or item.balance == 0:
            continue
        if item.balance < 0:
            return failure(ErrorCode.CONFLICT, "Valuation found a negative position")
        identity = item.ledger_account.removeprefix(prefix)
        try:
            instrument_id = UUID(identity)
        except ValueError:
            return failure(ErrorCode.CONFLICT, "Valuation found an unknown position")
        if item.commodity != str(instrument_id) or instrument_id in values:
            return failure(ErrorCode.CONFLICT, "Valuation position identity changed")
        values[instrument_id] = item.balance
    return Success(values)


def _build_valuation(
    command: MarkToMarketCommand,
    positions: tuple[PositionValuation, ...],
) -> Result[PortfolioValuation]:
    ledger = command.ledger
    cash = _money(ledger.cash(command.base_currency), command.currency_quantum)
    fees = _money(ledger.fees(command.base_currency), command.currency_quantum)
    realized = _money(
        ledger.realized_pnl(command.base_currency), command.currency_quantum
    )
    position_value = sum((item.market_value for item in positions), Decimal(0))
    nav = cash + position_value
    if min(cash, fees, nav) < 0:
        return failure(ErrorCode.CONFLICT, "Valuation ledger totals are invalid")
    try:
        value = PortfolioValuation.create(
            valuation_id=command.valuation_id,
            account_id=command.account_id,
            base_currency=command.base_currency,
            as_of=command.as_of,
            ledger_sequence=ledger.ledger_sequence,
            ledger_hash=ledger.ledger_hash,
            ledger_projection_hash=ledger.projection_hash,
            currency_quantum=command.currency_quantum,
            cash_value=cash,
            position_value=position_value,
            nav=nav,
            cumulative_fees=fees,
            realized_pnl=realized,
            positions=positions,
        )
    except (ValidationError, ValueError):
        return failure(ErrorCode.CONFLICT, "Valuation output failed integrity checks")
    return Success(value)


def _money(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)
