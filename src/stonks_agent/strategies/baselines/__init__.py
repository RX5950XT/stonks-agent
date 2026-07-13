"""Deterministic forecast baselines used by every strategy evaluation."""

from stonks_agent.strategies.baselines.last_value import LastValueBaseline
from stonks_agent.strategies.baselines.linear import LinearBaseline
from stonks_agent.strategies.baselines.moving_average import MovingAverageBaseline

__all__ = ["LastValueBaseline", "LinearBaseline", "MovingAverageBaseline"]
