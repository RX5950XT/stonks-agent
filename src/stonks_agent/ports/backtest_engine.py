"""Engine-neutral simulation boundary with no paper execution authority."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_contracts.backtest import BacktestJob, BacktestResult


@runtime_checkable
class BacktestEnginePort(Protocol):
    def run(self, job: BacktestJob) -> Result[BacktestResult]: ...
