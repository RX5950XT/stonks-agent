"""Authorize paper reads and persist only ledger-bound NAV snapshots."""

from __future__ import annotations

from datetime import datetime

from stonks_agent.domain._trading import failure
from stonks_agent.domain.auth import (
    AccessTarget,
    LocalPrincipal,
    Permission,
    ResourceKind,
    authorize_target,
)
from stonks_agent.domain.errors import ErrorCode, Failure, Result, Success
from stonks_agent.domain.monitoring import PortfolioValuation
from stonks_agent.domain.projections import PortfolioProjection, RiskProjection
from stonks_agent.ports.paper_projections import PaperProjectionUnitOfWorkFactory
from stonks_agent.ports.trading_unit_of_work import TradingCommitError


def read_portfolio_projection(
    principal: LocalPrincipal,
    account_id: str,
    unit_of_work: PaperProjectionUnitOfWorkFactory,
) -> Result[PortfolioProjection]:
    denied = _account_read_denied(principal, account_id)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.projections.get_portfolio(account_id)


def read_nav_projection(
    principal: LocalPrincipal,
    account_id: str,
    unit_of_work: PaperProjectionUnitOfWorkFactory,
) -> Result[PortfolioValuation]:
    denied = _account_read_denied(principal, account_id)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.projections.get_nav(account_id)


def read_risk_projection(
    principal: LocalPrincipal,
    account_id: str,
    *,
    as_of: datetime,
    unit_of_work: PaperProjectionUnitOfWorkFactory,
) -> Result[RiskProjection]:
    denied = _account_read_denied(principal, account_id)
    if denied is not None:
        return denied
    with unit_of_work() as transaction:
        return transaction.projections.get_risk(account_id, as_of=as_of)


def record_nav_projection(
    valuation: PortfolioValuation,
    unit_of_work: PaperProjectionUnitOfWorkFactory,
) -> Result[PortfolioValuation]:
    with unit_of_work() as transaction:
        ledger = transaction.ledger.get_projection(valuation.account_id)
        if isinstance(ledger, Failure):
            return ledger
        current = ledger.value
        if (
            valuation.ledger_sequence != current.ledger_sequence
            or valuation.ledger_hash != current.ledger_hash
            or valuation.ledger_projection_hash != current.projection_hash
        ):
            return failure(ErrorCode.CONFLICT, "NAV ledger binding is stale")
        saved = transaction.projections.save_valuation(valuation)
        if isinstance(saved, Failure):
            return saved
        if saved.value != valuation:
            return failure(ErrorCode.CONFLICT, "Stored NAV identity changed")
        try:
            transaction.commit()
        except TradingCommitError:
            return failure(ErrorCode.INTERNAL_ERROR, "NAV projection did not commit")
        return Success(saved.value)


def _account_read_denied(
    principal: LocalPrincipal,
    account_id: str,
) -> Failure | None:
    result = authorize_target(
        principal,
        Permission.READ,
        AccessTarget(kind=ResourceKind.ACCOUNT, identifier=account_id),
    )
    return result if isinstance(result, Failure) else None
