"""Deterministic economic postings for canonical paper fills."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import Field

from stonks_agent.domain._trading import TradingModel, failure
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.fills import Fill
from stonks_agent.domain.journal import JournalPosting, JournalSide, JournalTransaction
from stonks_agent.domain.ledger import LedgerProjection
from stonks_agent.domain.orders import OrderSide
from stonks_contracts.common import stable_payload_hash

ZERO = Decimal(0)


class LedgerPostingPolicy(TradingModel):
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    cost_basis_method: Literal["average"]
    monetary_rounding: Literal["ROUND_HALF_EVEN"]

    @property
    def policy_hash(self) -> str:
        return stable_payload_hash(self.model_dump(mode="json"))


def load_ledger_posting_policy(path: str | Path) -> LedgerPostingPolicy:
    try:
        return LedgerPostingPolicy.model_validate(
            yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise ValueError("ledger posting policy could not be loaded") from error


def build_fill_journal(
    fill: Fill,
    projection: LedgerProjection,
    policy: LedgerPostingPolicy,
) -> Result[JournalTransaction]:
    if fill.account_id != projection.account_id:
        return failure(ErrorCode.CONFLICT, "Fill belongs to another ledger account")
    if fill.occurred_at < projection.last_occurred_at:
        return failure(ErrorCode.CONFLICT, "Fill predates current ledger head")
    gross = (fill.quantity * fill.price).quantize(
        fill.fee_quantum, rounding=ROUND_HALF_EVEN
    )
    if gross <= 0:
        return failure(ErrorCode.INVALID_INPUT, "Fill notional is invalid")
    if fill.side is OrderSide.BUY:
        postings = _buy_postings(fill, gross, policy)
    else:
        result = _sell_postings(fill, gross, projection, policy)
        if isinstance(result, Failure):
            return result
        postings = result.value
    ordered = tuple(sorted(postings, key=lambda item: str(item.posting_id)))
    transaction_id = uuid5(
        NAMESPACE_URL, f"stonks:journal:{fill.fill_id}:{policy.policy_hash}"
    )
    try:
        return Success(
            JournalTransaction.create(
                transaction_id=transaction_id,
                account_id=fill.account_id,
                sequence=projection.ledger_sequence + 1,
                occurred_at=fill.occurred_at,
                previous_hash=projection.ledger_hash,
                source_order_intent_id=fill.order_intent_id,
                source_fill_id=fill.fill_id,
                postings=ordered,
            )
        )
    except ValueError:
        return failure(ErrorCode.CONFLICT, "Generated journal is invalid")


def _buy_postings(
    fill: Fill, gross: Decimal, policy: LedgerPostingPolicy
) -> tuple[JournalPosting, ...]:
    currency = fill.fee_currency
    instrument = str(fill.instrument_id)
    values = [
        (
            "inventory_value",
            f"inventory:value:{instrument}:{currency}",
            currency,
            JournalSide.DEBIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "clearing_payable",
            f"clearing:cash:{currency}",
            currency,
            JournalSide.CREDIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "clearing_settle",
            f"clearing:cash:{currency}",
            currency,
            JournalSide.DEBIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "cash",
            f"asset:cash:{currency}",
            currency,
            JournalSide.CREDIT,
            gross + fill.fees,
            fill.fee_quantum,
        ),
        (
            "inventory_units",
            f"inventory:units:{instrument}",
            instrument,
            JournalSide.DEBIT,
            fill.quantity,
            fill.quantity_quantum,
        ),
        (
            "clearing_units",
            f"clearing:units:{instrument}",
            instrument,
            JournalSide.CREDIT,
            fill.quantity,
            fill.quantity_quantum,
        ),
    ]
    if fill.fees > 0:
        values.append(
            (
                "fee",
                f"fee:execution:{currency}",
                currency,
                JournalSide.DEBIT,
                fill.fees,
                fill.fee_quantum,
            )
        )
    return tuple(_posting(fill, policy, *value) for value in values)


def _sell_postings(
    fill: Fill,
    gross: Decimal,
    projection: LedgerProjection,
    policy: LedgerPostingPolicy,
) -> Result[tuple[JournalPosting, ...]]:
    currency = fill.fee_currency
    instrument_id = fill.instrument_id
    instrument = str(instrument_id)
    quantity = projection.position(instrument_id)
    inventory_value = projection.inventory_value(instrument_id, currency)
    if (
        instrument_id in projection.unvalued_instrument_ids
        or quantity < fill.quantity
        or inventory_value <= 0
    ):
        return failure(ErrorCode.CONFLICT, "Sell cost basis is unavailable")
    cost = (
        inventory_value
        if quantity == fill.quantity
        else (inventory_value * fill.quantity / quantity).quantize(
            fill.fee_quantum, rounding=ROUND_HALF_EVEN
        )
    )
    if cost <= 0 or cost > inventory_value:
        return failure(ErrorCode.CONFLICT, "Sell cost basis is invalid")
    values = [
        (
            "clearing_units",
            f"clearing:units:{instrument}",
            instrument,
            JournalSide.DEBIT,
            fill.quantity,
            fill.quantity_quantum,
        ),
        (
            "inventory_units",
            f"inventory:units:{instrument}",
            instrument,
            JournalSide.CREDIT,
            fill.quantity,
            fill.quantity_quantum,
        ),
        (
            "clearing_receivable",
            f"clearing:cash:{currency}",
            currency,
            JournalSide.DEBIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "sale_pnl",
            f"pnl:realized:{currency}",
            currency,
            JournalSide.CREDIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "clearing_settle",
            f"clearing:cash:{currency}",
            currency,
            JournalSide.CREDIT,
            gross,
            fill.fee_quantum,
        ),
        (
            "cost_pnl",
            f"pnl:realized:{currency}",
            currency,
            JournalSide.DEBIT,
            cost,
            fill.fee_quantum,
        ),
        (
            "inventory_value",
            f"inventory:value:{instrument}:{currency}",
            currency,
            JournalSide.CREDIT,
            cost,
            fill.fee_quantum,
        ),
    ]
    net = gross - fill.fees
    if net > 0:
        values.append(
            (
                "cash",
                f"asset:cash:{currency}",
                currency,
                JournalSide.DEBIT,
                net,
                fill.fee_quantum,
            )
        )
    elif net < 0:
        values.append(
            (
                "cash",
                f"asset:cash:{currency}",
                currency,
                JournalSide.CREDIT,
                -net,
                fill.fee_quantum,
            )
        )
    if fill.fees > 0:
        values.append(
            (
                "fee",
                f"fee:execution:{currency}",
                currency,
                JournalSide.DEBIT,
                fill.fees,
                fill.fee_quantum,
            )
        )
    return Success(tuple(_posting(fill, policy, *value) for value in values))


def _posting(
    fill: Fill,
    policy: LedgerPostingPolicy,
    role: str,
    ledger_account: str,
    commodity: str,
    side: JournalSide,
    amount: Decimal,
    quantum: Decimal,
) -> JournalPosting:
    return JournalPosting(
        posting_id=uuid5(
            NAMESPACE_URL,
            f"stonks:posting:{fill.fill_id}:{policy.policy_hash}:{role}",
        ),
        ledger_account=ledger_account,
        commodity=commodity,
        side=side,
        amount=amount,
        quantum=quantum,
        memo=f"paper_fill:{fill.fill_id}",
    )
