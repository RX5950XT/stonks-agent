from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from uuid import UUID

from application.monitoring.helpers import ACCOUNT_ID, HASH_A, mark, projection
from stonks_agent.application.monitoring.mark_to_market import mark_to_market
from stonks_agent.application.projections.queries import (
    read_nav_projection,
    read_portfolio_projection,
    read_risk_projection,
    record_nav_projection,
)
from stonks_agent.domain.auth import LocalPrincipal, Role
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.monitoring import MarkToMarketCommand
from stonks_agent.domain.projections import PortfolioProjection, RiskProjection

NOW = datetime(2026, 7, 14, 5, tzinfo=UTC)
VIEWER = LocalPrincipal(subject="viewer:one", roles=frozenset({Role.VIEWER}))


def _valuation():  # type: ignore[no-untyped-def]
    ledger = projection(at=NOW, sequence=1)
    result = mark_to_market(
        MarkToMarketCommand(
            valuation_id=UUID("75000000-0000-4000-8000-000000000001"),
            account_id=ACCOUNT_ID,
            base_currency="USD",
            as_of=NOW,
            ledger=ledger,
            marks=(mark(at=NOW),),
            currency_quantum=Decimal("0.01"),
        )
    )
    assert isinstance(result, Success)
    return ledger, result.value


class Repository:
    def __init__(self) -> None:
        self.saved = None
        self.portfolio = PortfolioProjection.create(
            account_id=ACCOUNT_ID,
            base_currency="USD",
            as_of=NOW,
            account_aggregate_sequence=1,
            portfolio_sequence=1,
            ledger_sequence=1,
            ledger_hash=HASH_A,
            cash=(),
            positions=(),
            pending_order_ids=(),
            latest_target_ref=None,
        )
        from application.monitoring.helpers import decision

        value = decision()
        self.risk = RiskProjection.create(
            decision=value,
            observed_account_sequence=value.account_aggregate_sequence,
            observed_portfolio_sequence=value.portfolio_sequence,
            as_of=NOW,
        )
        self.nav = _valuation()[1]

    def save_valuation(self, valuation):  # type: ignore[no-untyped-def]
        self.saved = valuation
        return Success(valuation)

    def get_portfolio(self, account_id):  # type: ignore[no-untyped-def]
        return Success(self.portfolio)

    def get_nav(self, account_id):  # type: ignore[no-untyped-def]
        return Success(self.nav)

    def get_risk(self, account_id, *, as_of):  # type: ignore[no-untyped-def]
        return Success(self.risk)


class Ledger:
    def __init__(self, value) -> None:  # type: ignore[no-untyped-def]
        self.value = value

    def get_projection(self, account_id):  # type: ignore[no-untyped-def]
        return Success(self.value)


class UnitOfWork:
    def __init__(self, ledger) -> None:  # type: ignore[no-untyped-def]
        self.projections = Repository()
        self.ledger = Ledger(ledger)
        self.commits = 0

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


class Factory:
    def __init__(self, ledger) -> None:  # type: ignore[no-untyped-def]
        self.uow = UnitOfWork(ledger)

    def __call__(self):  # type: ignore[no-untyped-def]
        return self.uow


def test_viewer_reads_portfolio_nav_and_risk_without_commit() -> None:
    ledger, _ = _valuation()
    factory = Factory(ledger)

    portfolio = read_portfolio_projection(VIEWER, ACCOUNT_ID, factory)
    nav = read_nav_projection(VIEWER, ACCOUNT_ID, factory)
    risk = read_risk_projection(VIEWER, ACCOUNT_ID, as_of=NOW, unit_of_work=factory)

    assert isinstance(portfolio, Success)
    assert isinstance(nav, Success)
    assert isinstance(risk, Success)
    assert factory.uow.commits == 0


def test_record_nav_requires_exact_current_ledger_binding_before_commit() -> None:
    ledger, valuation = _valuation()
    factory = Factory(ledger)

    stored = record_nav_projection(valuation, factory)

    assert isinstance(stored, Success)
    assert factory.uow.projections.saved == valuation
    assert factory.uow.commits == 1

    drifted = Factory(projection(at=NOW, sequence=2))
    rejected = record_nav_projection(valuation, drifted)
    assert isinstance(rejected, Failure)
    assert rejected.error.code is ErrorCode.CONFLICT
    assert drifted.uow.commits == 0
    assert drifted.uow.projections.saved is None
