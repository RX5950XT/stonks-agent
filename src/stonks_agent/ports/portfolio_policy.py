"""Deterministic portfolio construction strategy boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.portfolio import PortfolioTarget
from stonks_agent.domain.portfolio_construction import BuildTargetCommand


@runtime_checkable
class PortfolioPolicyPort(Protocol):
    def build_target(
        self,
        command: BuildTargetCommand,
    ) -> Result[PortfolioTarget]: ...
