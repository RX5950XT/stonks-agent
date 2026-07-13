"""Hard-risk policy strategy boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot, PortfolioTarget
from stonks_agent.domain.risk import RiskDecision


@runtime_checkable
class RiskPolicyPort(Protocol):
    def evaluate(
        self,
        snapshot: AccountPortfolioSnapshot,
        target: PortfolioTarget,
        *,
        at: datetime,
    ) -> Result[RiskDecision]: ...
