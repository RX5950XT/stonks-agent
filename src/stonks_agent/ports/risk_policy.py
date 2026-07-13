"""Hard-risk policy strategy boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.risk import RiskDecision
from stonks_agent.domain.risk_evaluation import BuildRiskDecisionCommand


@runtime_checkable
class RiskPolicyPort(Protocol):
    def evaluate(
        self,
        command: BuildRiskDecisionCommand,
    ) -> Result[RiskDecision]: ...
