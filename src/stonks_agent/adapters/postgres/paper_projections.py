"""PostgreSQL-backed current portfolio, NAV, and risk read models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.ledger_repository import PostgresLedgerRepository
from stonks_agent.adapters.postgres.models import (
    OrderEventRow,
    OrderIntentRow,
    PaperAccountRow,
    PaperPortfolioValuationRow,
    PortfolioTargetRow,
    RiskDecisionRow,
)
from stonks_agent.adapters.postgres.trading_repository import (
    PostgresTradingRepository,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.monitoring import PortfolioValuation
from stonks_agent.domain.orders import OrderStatus
from stonks_agent.domain.portfolio import PortfolioTarget
from stonks_agent.domain.projections import (
    PortfolioProjection,
    ProjectedCashBalance,
    ProjectedPositionBalance,
    RiskProjection,
)
from stonks_agent.domain.risk import RiskDecision
from stonks_contracts.report import ReportReference

_PENDING_STATUSES = frozenset(
    {
        OrderStatus.CREATED.value,
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    }
)


class PostgresPaperProjectionRepository:
    """Validate all JSON payloads before exposing a read projection."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_valuation(
        self, valuation: PortfolioValuation
    ) -> Result[PortfolioValuation]:
        existing = self._session.get(PaperPortfolioValuationRow, valuation.valuation_id)
        if existing is not None:
            return _valuation_from_row(existing, expected=valuation)
        try:
            account = self._session.scalar(
                select(PaperAccountRow)
                .where(PaperAccountRow.account_id == valuation.account_id)
                .with_for_update()
            )
            if account is None:
                return _failure(ErrorCode.NOT_FOUND, "Paper account was not found")
            current = PostgresLedgerRepository(self._session).get_projection(
                valuation.account_id
            )
            if isinstance(current, Failure):
                return current
            if not _valuation_is_current(
                valuation, account, current.value.projection_hash
            ):
                return _failure(ErrorCode.CONFLICT, "NAV ledger binding is stale")
            self._session.add(
                PaperPortfolioValuationRow(
                    valuation_id=valuation.valuation_id,
                    account_id=valuation.account_id,
                    base_currency=valuation.base_currency,
                    as_of=valuation.as_of,
                    ledger_sequence=valuation.ledger_sequence,
                    ledger_hash=valuation.ledger_hash,
                    ledger_projection_hash=valuation.ledger_projection_hash,
                    valuation_hash=valuation.valuation_hash,
                    payload=valuation.model_dump(mode="json"),
                )
            )
            self._session.flush()
        except (DBAPIError, IntegrityError):
            return _failure(ErrorCode.CONFLICT, "NAV projection conflicted")
        return Success(valuation)

    def get_portfolio(self, account_id: str) -> Result[PortfolioProjection]:
        state = PostgresTradingRepository(self._session).get_account(account_id)
        if isinstance(state, Failure):
            return state
        target = self._latest_target(account_id)
        if isinstance(target, Failure):
            return target
        try:
            value = state.value
            projection = PortfolioProjection.create(
                account_id=value.account_id,
                base_currency=value.base_currency,
                as_of=value.updated_at,
                account_aggregate_sequence=value.account_aggregate_sequence,
                portfolio_sequence=value.portfolio_sequence,
                ledger_sequence=value.ledger_sequence,
                ledger_hash=value.ledger_hash,
                cash=tuple(
                    ProjectedCashBalance.from_balance(item) for item in value.cash
                ),
                positions=tuple(
                    ProjectedPositionBalance.from_balance(item)
                    for item in value.positions
                ),
                pending_order_ids=self._pending_order_ids(account_id),
                latest_target_ref=(
                    ReportReference(
                        ref_id=target.value.target_id,
                        content_hash=target.value.calculation_hash,
                    )
                    if target.value is not None
                    else None
                ),
            )
        except (DBAPIError, ValidationError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Portfolio projection is invalid")
        return Success(projection)

    def get_nav(self, account_id: str) -> Result[PortfolioValuation]:
        account = self._session.get(PaperAccountRow, account_id)
        if account is None:
            return _failure(ErrorCode.NOT_FOUND, "Paper account was not found")
        try:
            row = self._session.scalar(
                select(PaperPortfolioValuationRow)
                .where(PaperPortfolioValuationRow.account_id == account_id)
                .order_by(
                    PaperPortfolioValuationRow.as_of.desc(),
                    PaperPortfolioValuationRow.valuation_id.desc(),
                )
                .limit(1)
            )
        except DBAPIError:
            return _failure(ErrorCode.INTERNAL_ERROR, "NAV projection query failed")
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "NAV projection was not found")
        loaded = _valuation_from_row(row)
        if isinstance(loaded, Failure):
            return loaded
        ledger = PostgresLedgerRepository(self._session).get_projection(account_id)
        if isinstance(ledger, Failure):
            return ledger
        if not _valuation_is_current(
            loaded.value, account, ledger.value.projection_hash
        ):
            return _failure(ErrorCode.CONFLICT, "Latest NAV projection is stale")
        return loaded

    def get_risk(self, account_id: str, *, as_of: datetime) -> Result[RiskProjection]:
        state = PostgresTradingRepository(self._session).get_account(account_id)
        if isinstance(state, Failure):
            return state
        try:
            row = self._session.scalar(
                select(RiskDecisionRow)
                .where(RiskDecisionRow.account_id == account_id)
                .order_by(
                    RiskDecisionRow.decided_at.desc(),
                    RiskDecisionRow.decision_id.desc(),
                )
                .limit(1)
            )
        except DBAPIError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Risk projection query failed")
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Risk projection was not found")
        decision = _risk_from_row(row)
        if isinstance(decision, Failure):
            return decision
        try:
            projection = RiskProjection.create(
                decision=decision.value,
                observed_account_sequence=state.value.account_aggregate_sequence,
                observed_portfolio_sequence=state.value.portfolio_sequence,
                as_of=as_of,
            )
        except (ValidationError, ValueError):
            return _failure(ErrorCode.CONFLICT, "Risk projection is invalid")
        return Success(projection)

    def _latest_target(self, account_id: str) -> Result[PortfolioTarget | None]:
        try:
            row = self._session.scalar(
                select(PortfolioTargetRow)
                .where(PortfolioTargetRow.account_id == account_id)
                .order_by(
                    PortfolioTargetRow.created_at.desc(),
                    PortfolioTargetRow.target_id.desc(),
                )
                .limit(1)
            )
        except DBAPIError:
            return _failure(ErrorCode.INTERNAL_ERROR, "Target projection query failed")
        if row is None:
            return Success(None)
        return _target_from_row(row)

    def _pending_order_ids(self, account_id: str) -> tuple[UUID, ...]:
        intents = self._session.scalars(
            select(OrderIntentRow)
            .where(OrderIntentRow.account_id == account_id)
            .order_by(OrderIntentRow.intent_id)
        ).all()
        if not intents:
            return ()
        latest_sequences = (
            select(
                OrderEventRow.order_intent_id,
                func.max(OrderEventRow.sequence).label("sequence"),
            )
            .where(
                OrderEventRow.order_intent_id.in_(
                    tuple(item.intent_id for item in intents)
                )
            )
            .group_by(OrderEventRow.order_intent_id)
            .subquery()
        )
        events = self._session.scalars(
            select(OrderEventRow).join(
                latest_sequences,
                (OrderEventRow.order_intent_id == latest_sequences.c.order_intent_id)
                & (OrderEventRow.sequence == latest_sequences.c.sequence),
            )
        ).all()
        states = {item.order_intent_id: item.to_status for item in events}
        return tuple(
            item.intent_id
            for item in intents
            if states.get(item.intent_id, OrderStatus.CREATED.value)
            in _PENDING_STATUSES
        )


def _valuation_is_current(
    valuation: PortfolioValuation,
    account: PaperAccountRow,
    projection_hash: str,
) -> bool:
    return (
        valuation.account_id == account.account_id
        and valuation.base_currency == account.base_currency
        and valuation.ledger_sequence == account.ledger_sequence
        and valuation.ledger_hash == account.ledger_hash
        and valuation.ledger_projection_hash == projection_hash
    )


def _valuation_from_row(
    row: PaperPortfolioValuationRow,
    *,
    expected: PortfolioValuation | None = None,
) -> Result[PortfolioValuation]:
    try:
        value = PortfolioValuation.model_validate(row.payload)
    except (ValidationError, ValueError):
        return _failure(ErrorCode.CONFLICT, "NAV projection payload is invalid")
    if (
        value.valuation_id != row.valuation_id
        or value.account_id != row.account_id
        or value.base_currency != row.base_currency
        or value.as_of != row.as_of
        or value.ledger_sequence != row.ledger_sequence
        or value.ledger_hash != row.ledger_hash
        or value.ledger_projection_hash != row.ledger_projection_hash
        or value.valuation_hash != row.valuation_hash
        or (expected is not None and value != expected)
    ):
        return _failure(ErrorCode.CONFLICT, "NAV projection identity changed")
    return Success(value)


def _target_from_row(row: PortfolioTargetRow) -> Result[PortfolioTarget]:
    try:
        value = PortfolioTarget.model_validate(row.payload)
    except (ValidationError, ValueError):
        return _failure(ErrorCode.CONFLICT, "Target projection payload is invalid")
    if (
        value.target_id != row.target_id
        or value.account_id != row.account_id
        or value.portfolio_snapshot_id != row.portfolio_snapshot_id
        or value.account_aggregate_sequence != row.account_aggregate_sequence
        or value.portfolio_sequence != row.portfolio_sequence
        or value.calculation_hash != row.calculation_hash
        or value.policy_hash != row.policy_hash
        or value.as_of != row.created_at
    ):
        return _failure(ErrorCode.CONFLICT, "Target projection identity changed")
    return Success(value)


def _risk_from_row(row: RiskDecisionRow) -> Result[RiskDecision]:
    try:
        value = RiskDecision.model_validate(row.payload)
    except (ValidationError, ValueError):
        return _failure(ErrorCode.CONFLICT, "Risk projection payload is invalid")
    if (
        value.decision_id != row.decision_id
        or value.portfolio_target_id != row.portfolio_target_id
        or value.account_id != row.account_id
        or value.account_aggregate_sequence != row.account_aggregate_sequence
        or value.portfolio_sequence != row.portfolio_sequence
        or value.approved != row.approved
        or value.decision_hash != row.decision_hash
        or value.input_target_hash != row.input_target_hash
        or value.authorized_target_hash != row.authorized_target_hash
        or value.policy_hash != row.policy_hash
        or value.decided_at != row.decided_at
        or value.expires_at != row.expires_at
    ):
        return _failure(ErrorCode.CONFLICT, "Risk projection identity changed")
    return Success(value)


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
