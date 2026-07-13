from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from stonks_agent.application.portfolio.build_target import build_target
from stonks_agent.application.risk.evaluate import load_risk_policy
from stonks_agent.domain.calendar import MarketSession
from stonks_agent.domain.errors import Success
from stonks_agent.domain.portfolio import PortfolioTarget
from stonks_agent.domain.risk_evaluation import (
    BuildRiskDecisionCommand,
    HardRiskPolicy,
    RiskInstrumentState,
    RiskKillSwitchState,
)
from stonks_agent.ports.ledger import LedgerHead
from stonks_contracts.instrument import AssetClass

from ..portfolio.helpers import (
    ACCOUNT_ID,
    HASH_A,
    INSTRUMENT_A,
    NOW,
    candidates,
    snapshot,
)
from ..portfolio.helpers import (
    command as portfolio_command,
)
from ..portfolio.helpers import (
    configured_policy as portfolio_policy,
)

ROOT = Path(__file__).resolve().parents[3]
RISK_AT = NOW + timedelta(minutes=2)


def configured_policy() -> HardRiskPolicy:
    return load_risk_policy(ROOT / "config" / "policies" / "risk_v1.yaml")


def target() -> PortfolioTarget:
    result = build_target(portfolio_command(), portfolio_policy())
    assert isinstance(result, Success)
    return result.value


def instrument_state(**changes: object) -> RiskInstrumentState:
    payload: dict[str, object] = {
        "instrument_id": INSTRUMENT_A,
        "asset_class": AssetClass.EQUITY,
        "sector": "technology",
        "mic": "XNAS",
        "currency": "USD",
        "mark_price": Decimal("100"),
        "mark_as_of": NOW,
        "quantity_quantum": Decimal("1"),
        "average_daily_volume": Decimal("10000"),
        "session": MarketSession(
            mic="XNAS",
            session_date=NOW.date(),
            opens_at=NOW - timedelta(hours=1),
            closes_at=NOW + timedelta(hours=6),
        ),
    }
    return RiskInstrumentState.model_validate(payload | changes)


def risk_command(**changes: object) -> BuildRiskDecisionCommand:
    payload: dict[str, object] = {
        "decision_id": "44000000-0000-4000-8000-000000000001",
        "snapshot": snapshot(),
        "target": target(),
        "instruments": (instrument_state(),),
        "signal_candidates": candidates(),
        "open_reservations": (),
        "ledger_head": LedgerHead(
            account_id=ACCOUNT_ID,
            sequence=11,
            transaction_hash=HASH_A,
        ),
        "high_watermark_nav": Decimal("11000.00"),
        "day_start_nav": Decimal("11000.00"),
        "kill_switch": RiskKillSwitchState(
            global_active=False,
            account_active=False,
            observed_at=RISK_AT,
        ),
        "at": RISK_AT,
    }
    return BuildRiskDecisionCommand.model_validate(payload | changes)
