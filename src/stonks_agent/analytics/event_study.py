"""Pure-Python market-model event study with deterministic statistics.

The market-model, CAR, t-test, and seeded bootstrap design is selectively
derived from virattt/ai-hedge-fund commit
3a18702cb25777fb4bdb4b2527a0c868bc8297f4 (MIT). External data access and
upstream application code are intentionally not included.
"""

from __future__ import annotations

import math
import random
from datetime import date
from decimal import Decimal, localcontext
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_contracts.common import DecimalString, UnitDecimal, UTCDateTime


class AlignedReturn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trading_day: date
    stock_return: DecimalString
    market_return: DecimalString
    available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available_at.date() < self.trading_day:
            raise ValueError("return cannot be available before its trading day")
        return self


class EventStudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    evidence_id: UUID
    instrument_id: UUID
    filing_available_at: UTCDateTime
    event_day: date
    as_of: UTCDateTime

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.filing_available_at > self.as_of:
            raise ValueError("event filing is not available as of the study")
        if self.event_day < self.filing_available_at.date():
            raise ValueError("event day cannot precede filing availability")
        if self.event_day > self.as_of.date():
            raise ValueError("event day cannot be in the future")
        return self


class EventStudyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    estimation_start: int = Field(default=-250, ge=-500, le=-2)
    estimation_end: int = Field(default=-11, ge=-499, le=-1)
    minimum_estimation_observations: int = Field(default=120, ge=2, le=500)
    event_window_end: int = Field(default=20, ge=1, le=60)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if self.estimation_start > self.estimation_end:
            raise ValueError("estimation window is reversed")
        if self.estimation_end >= 0:
            raise ValueError("estimation window must precede the event")
        return self


class MarketModelFit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alpha: DecimalString
    beta: DecimalString
    r_squared: UnitDecimal
    n_observations: int = Field(ge=2)


class EventCAR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    evidence_id: UUID
    instrument_id: UUID
    event_day: date
    as_of: UTCDateTime
    market_model: MarketModelFit
    daily_abnormal_returns: tuple[DecimalString, ...]
    car_0_1: DecimalString | None
    car_0_2: DecimalString | None
    car_0_5: DecimalString | None
    car_0_20: DecimalString | None


class BootstrapInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lower: DecimalString
    upper: DecimalString
    confidence: UnitDecimal
    n_bootstrap: int = Field(ge=100, le=1_000_000)
    seed: int


class WindowStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window: str = Field(pattern=r"^\[0,\+[0-9]+\]$")
    n_events: int = Field(ge=2)
    mean_car: DecimalString
    sample_std: DecimalString
    t_statistic: DecimalString
    p_value: UnitDecimal
    bootstrap_ci: BootstrapInterval


def analyze_event(
    request: EventStudyRequest,
    observations: tuple[AlignedReturn, ...],
    *,
    policy: EventStudyPolicy | None = None,
) -> Result[EventCAR]:
    selected_policy = policy or EventStudyPolicy()
    validated = _validated_observations(request, observations)
    if isinstance(validated, Failure):
        return validated
    aligned = validated.value
    day_to_index = {item.trading_day: index for index, item in enumerate(aligned)}
    event_index = day_to_index.get(request.event_day)
    if event_index is None:
        return _failure(ErrorCode.INVALID_INPUT, "Event day is not in aligned returns")
    if aligned[event_index].available_at < request.filing_available_at:
        return _failure(
            ErrorCode.CONFLICT,
            "Event return predates filing availability",
        )
    start = event_index + selected_policy.estimation_start
    end = event_index + selected_policy.estimation_end
    if start < 0 or end < start:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Estimation window is unavailable")
    estimation = aligned[start : end + 1]
    if len(estimation) < selected_policy.minimum_estimation_observations:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Estimation window is too short")
    model = fit_market_model(
        tuple(item.stock_return for item in estimation),
        tuple(item.market_return for item in estimation),
    )
    event_end = min(event_index + selected_policy.event_window_end, len(aligned) - 1)
    event_window = aligned[event_index : event_end + 1]
    abnormal = compute_abnormal_returns(
        tuple(item.stock_return for item in event_window),
        tuple(item.market_return for item in event_window),
        alpha=model.alpha,
        beta=model.beta,
    )
    return Success(
        EventCAR(
            event_id=request.event_id,
            evidence_id=request.evidence_id,
            instrument_id=request.instrument_id,
            event_day=request.event_day,
            as_of=request.as_of,
            market_model=model,
            daily_abnormal_returns=abnormal,
            car_0_1=_car_if_available(abnormal, 1),
            car_0_2=_car_if_available(abnormal, 2),
            car_0_5=_car_if_available(abnormal, 5),
            car_0_20=_car_if_available(abnormal, 20),
        )
    )


def fit_market_model(
    stock_returns: tuple[Decimal, ...],
    market_returns: tuple[Decimal, ...],
) -> MarketModelFit:
    _require_finite_paired(stock_returns, market_returns, minimum=2)
    count = len(stock_returns)
    with localcontext() as context:
        context.prec = 34
        n = Decimal(count)
        market_mean = sum(market_returns, Decimal(0)) / n
        stock_mean = sum(stock_returns, Decimal(0)) / n
        market_variance = sum(
            ((value - market_mean) ** 2 for value in market_returns), Decimal(0)
        )
        covariance = sum(
            (
                (market - market_mean) * (stock - stock_mean)
                for stock, market in zip(stock_returns, market_returns, strict=True)
            ),
            Decimal(0),
        )
        beta: Decimal = (
            Decimal(0) if market_variance == 0 else covariance / market_variance
        )
        alpha = stock_mean - beta * market_mean
        predictions = tuple(alpha + beta * value for value in market_returns)
        residual = sum(
            (
                (actual - predicted) ** 2
                for actual, predicted in zip(stock_returns, predictions, strict=True)
            ),
            Decimal(0),
        )
        total = sum(((value - stock_mean) ** 2 for value in stock_returns), Decimal(0))
        r_squared = Decimal(0) if total == 0 else Decimal(1) - residual / total
    return MarketModelFit(
        alpha=_clean_decimal(alpha),
        beta=_clean_decimal(beta),
        r_squared=_clean_decimal(max(Decimal(0), min(Decimal(1), r_squared))),
        n_observations=count,
    )


def compute_abnormal_returns(
    stock_returns: tuple[Decimal, ...],
    market_returns: tuple[Decimal, ...],
    *,
    alpha: Decimal,
    beta: Decimal,
) -> tuple[Decimal, ...]:
    _require_finite_paired(stock_returns, market_returns, minimum=1)
    if not alpha.is_finite() or not beta.is_finite():
        raise ValueError("market model coefficients must be finite")
    return tuple(
        _clean_decimal(stock - (alpha + beta * market))
        for stock, market in zip(stock_returns, market_returns, strict=True)
    )


def bootstrap_mean_ci(
    values: tuple[Decimal, ...],
    *,
    n_bootstrap: int = 10_000,
    confidence: Decimal = Decimal("0.95"),
    seed: int,
) -> BootstrapInterval:
    _require_values(values, minimum=2)
    if not 100 <= n_bootstrap <= 1_000_000:
        raise ValueError("bootstrap count is outside supported bounds")
    if not confidence.is_finite() or not Decimal(0) < confidence < Decimal(1):
        raise ValueError("bootstrap confidence is invalid")
    rng = random.Random(seed)
    count = len(values)
    means = sorted(
        sum((rng.choice(values) for _ in range(count)), Decimal(0)) / Decimal(count)
        for _ in range(n_bootstrap)
    )
    tail = (Decimal(1) - confidence) / Decimal(2)
    return BootstrapInterval(
        lower=_percentile(means, tail),
        upper=_percentile(means, Decimal(1) - tail),
        confidence=confidence,
        n_bootstrap=n_bootstrap,
        seed=seed,
    )


def aggregate_window(
    window: str,
    values: tuple[Decimal, ...],
    *,
    n_bootstrap: int = 10_000,
    seed: int,
) -> WindowStatistics:
    _require_values(values, minimum=2)
    count = len(values)
    mean = sum(values, Decimal(0)) / Decimal(count)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
        count - 1
    )
    sample_std = variance.sqrt()
    if sample_std == 0:
        t_statistic, p_value = Decimal(0), Decimal(1)
    else:
        t_statistic = mean / (sample_std / Decimal(count).sqrt())
        p_value = _decimal_from_float(
            _student_t_two_sided(float(t_statistic), count - 1)
        )
    return WindowStatistics(
        window=window,
        n_events=count,
        mean_car=_clean_decimal(mean),
        sample_std=_clean_decimal(sample_std),
        t_statistic=_clean_decimal(t_statistic),
        p_value=_clean_decimal(p_value),
        bootstrap_ci=bootstrap_mean_ci(
            values,
            n_bootstrap=n_bootstrap,
            confidence=Decimal("0.95"),
            seed=seed,
        ),
    )


def _validated_observations(
    request: EventStudyRequest,
    observations: tuple[AlignedReturn, ...],
) -> Result[tuple[AlignedReturn, ...]]:
    if not observations:
        return _failure(ErrorCode.DATA_UNAVAILABLE, "Aligned returns are unavailable")
    days = tuple(item.trading_day for item in observations)
    if len(days) != len(set(days)):
        return _failure(ErrorCode.CONFLICT, "Aligned return days are duplicated")
    if any(
        item.available_at > request.as_of or item.trading_day > request.as_of.date()
        for item in observations
    ):
        return _failure(ErrorCode.CONFLICT, "Aligned returns contain future data")
    return Success(tuple(sorted(observations, key=lambda item: item.trading_day)))


def _car_if_available(values: tuple[Decimal, ...], end: int) -> Decimal | None:
    return (
        _clean_decimal(sum(values[: end + 1], Decimal(0)))
        if len(values) > end
        else None
    )


def _require_finite_paired(
    left: tuple[Decimal, ...],
    right: tuple[Decimal, ...],
    *,
    minimum: int,
) -> None:
    if len(left) != len(right) or len(left) < minimum:
        raise ValueError("return series lengths are invalid")
    _require_values((*left, *right), minimum=minimum * 2)


def _require_values(values: tuple[Decimal, ...], *, minimum: int) -> None:
    if len(values) < minimum or any(not value.is_finite() for value in values):
        raise ValueError("statistics require finite observations")


def _percentile(values: list[Decimal], probability: Decimal) -> Decimal:
    position = probability * Decimal(len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - Decimal(lower)
    return _clean_decimal(values[lower] + (values[upper] - values[lower]) * fraction)


def _clean_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("statistical result is not finite")
    return Decimal(0) if value == 0 else value.normalize()


def _decimal_from_float(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ValueError("statistical result is not finite")
    return Decimal(format(value, ".16g"))


def _student_t_two_sided(t_statistic: float, degrees_of_freedom: int) -> float:
    if degrees_of_freedom < 1 or not math.isfinite(t_statistic):
        raise ValueError("t-test inputs are invalid")
    x = degrees_of_freedom / (degrees_of_freedom + t_statistic * t_statistic)
    return min(1.0, max(0.0, _regularized_beta(x, degrees_of_freedom / 2, 0.5)))


def _regularized_beta(x: float, a: float, b: float) -> float:
    if not 0 <= x <= 1 or a <= 0 or b <= 0:
        raise ValueError("beta function inputs are invalid")
    if x in {0.0, 1.0}:
        return x
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(x, a, b) / a
    return 1 - front * _beta_fraction(1 - x, b, a) / b


def _beta_fraction(x: float, a: float, b: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    result = d
    for iteration in range(1, 201):
        even = 2 * iteration
        numerator = iteration * (b - iteration) * x / ((qam + even) * (a + even))
        d, c, result = _beta_step(numerator, d, c, result, tiny)
        numerator = (
            -(a + iteration) * (qab + iteration) * x / ((a + even) * (qap + even))
        )
        d, c, updated = _beta_step(numerator, d, c, result, tiny)
        if abs(updated - result) <= 3e-14 * abs(updated):
            return updated
        result = updated
    raise ValueError("beta continued fraction did not converge")


def _beta_step(
    numerator: float,
    denominator: float,
    coefficient: float,
    result: float,
    tiny: float,
) -> tuple[float, float, float]:
    denominator = 1.0 + numerator * denominator
    denominator = 1.0 / (tiny if abs(denominator) < tiny else denominator)
    coefficient = 1.0 + numerator / coefficient
    coefficient = tiny if abs(coefficient) < tiny else coefficient
    return denominator, coefficient, result * denominator * coefficient


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
