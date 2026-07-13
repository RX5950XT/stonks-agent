"""Deterministic portfolio construction strategy boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.portfolio import AccountPortfolioSnapshot, PortfolioTarget
from stonks_agent.domain.signal import AlphaSignal


@runtime_checkable
class PortfolioPolicyPort(Protocol):
    def build_target(
        self,
        snapshot: AccountPortfolioSnapshot,
        signals: tuple[AlphaSignal, ...],
    ) -> Result[PortfolioTarget]: ...
