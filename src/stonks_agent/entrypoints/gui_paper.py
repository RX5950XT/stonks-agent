"""Optional PostgreSQL paper composition for the local Stonks Terminal.

The console stays read-only: this module owns the database lifecycle, the
one-shot migration and the account bootstrap, then hands the GUI a reader that
can only project what the canonical ledger already committed.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import Engine, create_engine

from stonks_agent.adapters.postgres.unit_of_work import PostgresUnitOfWork
from stonks_agent.application.operations.activate_kill_switch import read_kill_switch
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
)
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind, Role
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import ErrorCode, Failure, Result
from stonks_agent.domain.gui_paper import (
    GuiPaperCashView,
    GuiPaperIntegrityView,
    GuiPaperNavView,
    GuiPaperPortfolioView,
    GuiPaperPositionView,
    GuiPaperRiskView,
    GuiPaperSafetyView,
)
from stonks_agent.domain.monitoring import PortfolioValuation
from stonks_agent.domain.operations import KillSwitchScope, PaperKillSwitchState
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot, CashBalance
from stonks_agent.domain.projections import PortfolioProjection, RiskProjection
from stonks_agent.entrypoints.api.gui import PaperCapability, PaperRow
from stonks_agent.ports.paper_operations import PaperOperationsUnitOfWork
from stonks_agent.ports.paper_projections import (
    PaperProjectionUnitOfWork,
    PaperProjectionUnitOfWorkFactory,
)
from stonks_agent.ports.unit_of_work import UnitOfWork

DEFAULT_ACCOUNT_ID = "paper-local"
DEFAULT_OPENING_CASH = Decimal("100000.00")
BASE_CURRENCY = "USD"
CASH_QUANTUM = Decimal("0.01")
_ACCOUNT_NAMESPACE = UUID("6f9f4a02-0d5a-4f1e-9b6c-3f5f2a8c7d10")
_PASSWORD_BYTES = 24
_MAX_ROWS = 12


class PaperStartupError(RuntimeError):
    """Public-safe failure while composing the optional paper stack."""


@dataclass(frozen=True, slots=True)
class PaperRuntime:
    database_url: str
    account_id: str
    environment: Mapping[str, str]


def prepare_paper_runtime(
    runtime_root: Path,
    *,
    port: int,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> PaperRuntime:
    """Generate or reuse the local database credential without printing it."""

    password_file = runtime_root / "postgres-password"
    if password_file.is_symlink():
        raise PaperStartupError("paper database credential path is invalid")
    if not password_file.is_file():
        password_file.write_text(
            secrets.token_urlsafe(_PASSWORD_BYTES),
            encoding="ascii",
        )
    password = password_file.read_text(encoding="ascii").strip()
    if not password:
        raise PaperStartupError("paper database credential is empty")
    return PaperRuntime(
        database_url=(
            f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/stonks"
        ),
        account_id=account_id,
        environment={
            "STONKS_GUI_DB_PORT": str(port),
            "STONKS_GUI_DB_PASSWORD_FILE": str(password_file.resolve()),
        },
    )


def migrate(database_url: str, *, root: Path) -> None:
    """Run the one-shot Alembic upgrade with the owner credential."""

    from alembic import command
    from alembic.config import Config

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def bootstrap_account(
    engine: Engine,
    *,
    account_id: str,
    opening_cash: Decimal = DEFAULT_OPENING_CASH,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Register the paper account once; re-running is a no-op, not a reset."""

    as_of = (clock or utc_now)()
    snapshot = AccountPortfolioSnapshot(
        snapshot_id=uuid5(_ACCOUNT_NAMESPACE, account_id),
        account_id=account_id,
        as_of=as_of,
        account_aggregate_sequence=0,
        portfolio_sequence=0,
        ledger_sequence=0,
        ledger_hash=None,
        cash=(
            CashBalance(
                currency=BASE_CURRENCY,
                settled_amount=opening_cash,
                reserved_amount=Decimal("0.00"),
                quantum=CASH_QUANTUM,
            ),
        ),
    )
    with cast(UnitOfWork, PostgresUnitOfWork(engine)) as unit_of_work:
        result = unit_of_work.trading.register_account(
            snapshot,
            base_currency=BASE_CURRENCY,
        )
        if isinstance(result, Failure):
            raise PaperStartupError("paper account bootstrap failed")
        unit_of_work.commit()


def paper_reader(
    engine: Engine,
    *,
    account_id: str,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[], PaperCapability]:
    """Build the read-only projection reader handed to the console."""

    principal = LocalPrincipal(
        subject="local-console-reader",
        roles=frozenset({Role.PAPER_OPERATOR}),
        targets=frozenset(
            {
                AccessTarget(kind=ResourceKind.ACCOUNT, identifier=account_id),
                AccessTarget(kind=ResourceKind.PAPER_GLOBAL, identifier="global"),
            }
        ),
    )

    def factory() -> PaperProjectionUnitOfWork:
        return cast(PaperProjectionUnitOfWork, PostgresUnitOfWork(engine))

    def read() -> PaperCapability:
        return _capability(
            principal,
            account_id,
            factory,
            as_of=(clock or utc_now)(),
            operations_factory=lambda: cast(
                PaperOperationsUnitOfWork,
                PostgresUnitOfWork(engine),
            ),
        )

    return read


def _capability(
    principal: LocalPrincipal,
    account_id: str,
    factory: PaperProjectionUnitOfWorkFactory,
    *,
    as_of: datetime | None = None,
    operations_factory: Callable[[], PaperOperationsUnitOfWork] | None = None,
) -> PaperCapability:
    portfolio = read_portfolio_projection(principal, account_id, factory)
    if isinstance(portfolio, Failure):
        return PaperCapability(
            state="unavailable",
            detail=(
                "無法讀取 paper 投資組合投影 "
                f"{portfolio.error.code.value}。不顯示可能過期的數字。"
            ),
        )
    projection = portfolio.value
    rows = list(_portfolio_rows(projection))
    nav = read_nav_projection(principal, account_id, factory)
    if isinstance(nav, Failure):
        rows.append(PaperRow(label="NAV", value="尚未估值"))
    else:
        rows.append(
            PaperRow(
                label="NAV",
                value=f"{nav.value.nav} {nav.value.base_currency}",
            )
        )
    risk = read_risk_projection(
        principal,
        account_id,
        as_of=as_of or utc_now(),
        unit_of_work=factory,
    )
    safety = (
        read_kill_switch(
            principal,
            KillSwitchScope.GLOBAL,
            None,
            operations_factory,
        )
        if operations_factory is not None
        else None
    )
    return PaperCapability(
        state="ready",
        detail="PostgreSQL canonical paper projections",
        account_id=account_id,
        rows=tuple(rows[:_MAX_ROWS]),
        portfolio=_portfolio_view(projection),
        nav=_nav_view(nav),
        risk=_risk_view(risk),
        integrity=GuiPaperIntegrityView(
            state="verified",
            account_sequence=projection.account_aggregate_sequence,
            portfolio_sequence=projection.portfolio_sequence,
            ledger_sequence=projection.ledger_sequence,
            ledger_hash=projection.ledger_hash,
            projection_hash=projection.projection_hash,
        ),
        safety=_safety_view(safety),
    )


def _portfolio_rows(projection: PortfolioProjection) -> Sequence[PaperRow]:
    rows = [
        PaperRow(label="帳戶序號", value=str(projection.portfolio_sequence)),
        PaperRow(label="Ledger 序號", value=str(projection.ledger_sequence)),
    ]
    for balance in projection.cash[:4]:
        rows.append(
            PaperRow(
                label=f"{balance.currency} 可用",
                value=f"{balance.available_amount}",
            )
        )
        rows.append(
            PaperRow(
                label=f"{balance.currency} 已保留",
                value=f"{balance.reserved_amount}",
            )
        )
    rows.append(PaperRow(label="持有標的", value=str(len(projection.positions))))
    rows.append(
        PaperRow(label="未結訂單", value=str(len(projection.pending_order_ids)))
    )
    return rows


def _portfolio_view(projection: PortfolioProjection) -> GuiPaperPortfolioView:
    return GuiPaperPortfolioView(
        base_currency=projection.base_currency,
        as_of=projection.as_of,
        cash=tuple(
            GuiPaperCashView(
                currency=item.currency,
                settled=item.settled_amount,
                reserved=item.reserved_amount,
                available=item.available_amount,
            )
            for item in projection.cash
        ),
        positions=tuple(
            GuiPaperPositionView(
                instrument_id=item.instrument_id,
                quantity=item.quantity,
                sellable=item.sellable_quantity,
                reserved=item.reserved_quantity,
                available=item.available_quantity,
            )
            for item in projection.positions[:128]
        ),
        position_count=len(projection.positions),
        pending_order_count=len(projection.pending_order_ids),
        latest_target=projection.latest_target_ref is not None,
    )


def _nav_view(result: Result[PortfolioValuation]) -> GuiPaperNavView:
    if isinstance(result, Failure):
        if result.error.code is ErrorCode.NOT_FOUND:
            return GuiPaperNavView(state="empty")
        return GuiPaperNavView(
            state="unavailable",
            error_code=result.error.code.value,
        )
    valuation = result.value
    return GuiPaperNavView(
        state="available",
        as_of=valuation.as_of,
        base_currency=valuation.base_currency,
        nav=valuation.nav,
        cash_value=valuation.cash_value,
        position_value=valuation.position_value,
        cumulative_fees=valuation.cumulative_fees,
        realized_pnl=valuation.realized_pnl,
    )


def _risk_view(result: Result[RiskProjection]) -> GuiPaperRiskView:
    if isinstance(result, Failure):
        if result.error.code is ErrorCode.NOT_FOUND:
            return GuiPaperRiskView(state="empty")
        return GuiPaperRiskView(
            state="unavailable",
            error_code=result.error.code.value,
        )
    risk = result.value
    return GuiPaperRiskView(
        state="available",
        approved=risk.approved,
        currently_authorized=risk.currently_authorized,
        failed_checks=risk.failed_checks[:64],
        policy_version=risk.policy_version,
        decided_at=risk.decided_at,
        expires_at=risk.expires_at,
    )


def _safety_view(
    result: Result[PaperKillSwitchState] | None,
) -> GuiPaperSafetyView:
    if result is None:
        return GuiPaperSafetyView(
            state="unavailable",
            error_code=ErrorCode.DATA_UNAVAILABLE.value,
        )
    if isinstance(result, Failure):
        return GuiPaperSafetyView(
            state="unavailable",
            error_code=result.error.code.value,
        )
    state = result.value
    return GuiPaperSafetyView(
        state="available",
        active=state.active,
        reason_code=state.reason_code,
        version=state.version,
        updated_at=state.updated_at,
    )


def open_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, pool_size=2, max_overflow=1)
