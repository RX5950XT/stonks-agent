"""Artifact-only evaluation boundary; promotion remains core-owned."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.evaluation import EvaluationReport, EvaluationRequest


@runtime_checkable
class StrategyLabPort(Protocol):
    def evaluate(self, request: EvaluationRequest) -> Result[EvaluationReport]: ...
