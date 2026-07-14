"""Validate isolated backtest output before it enters evaluation workflows."""

from __future__ import annotations

from pydantic import ValidationError

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.ports.backtest_engine import BacktestEnginePort
from stonks_contracts.backtest import BacktestJob, BacktestResult


def run_backtest(
    job: BacktestJob,
    engine: BacktestEnginePort,
) -> Result[BacktestResult]:
    response = engine.run(job)
    if isinstance(response, Failure):
        return response
    try:
        candidate = BacktestResult.model_validate(
            response.value.model_dump(mode="json")
        )
        candidate.validate_against(job)
    except (ValidationError, ValueError):
        return Failure(
            StructuredError(
                code=ErrorCode.MODEL_OUTPUT_INVALID,
                message="Backtest engine result failed canonical validation",
            )
        )
    return Success(candidate)
