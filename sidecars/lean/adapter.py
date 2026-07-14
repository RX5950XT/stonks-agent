"""Canonical mapping boundary around the isolated QuantConnect LEAN runtime."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from stonks_contracts.backtest import (
    BacktestBar,
    BacktestCashBalance,
    BacktestEngineKind,
    BacktestFill,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderOutcome,
    BacktestOrderSide,
    BacktestOrderStatus,
    BacktestPosition,
    BacktestResult,
    BacktestRuntimeIdentity,
)
from stonks_contracts.backtest_math import (
    canonical_fill_fee,
    canonical_fill_price,
    canonical_outcome_status,
)
from stonks_contracts.common import stable_payload_hash

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")
_RUNTIME_FILES = (
    "Dockerfile",
    "NOTICE.md",
    "adapter.py",
    "app.py",
    "appsettings.template.json",
    "distribution-manifest.yaml",
    "engine.py",
    "runtime_app.py",
    "pyproject.toml",
    "uv.lock",
)
_RUNTIME_TREES = ("dotnet-locks", "engine", "patches")


@dataclass(frozen=True, slots=True)
class EngineFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class EngineOrderTrace:
    order_id: UUID
    status: BacktestOrderStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class EngineFillTrace:
    external_fill_id: str
    order_id: UUID
    quantity: Decimal
    raw_price: Decimal
    raw_fees: Decimal
    raw_event_hash: str
    occurred_at: datetime
    source_bar_id: UUID


@dataclass(frozen=True, slots=True)
class EngineRunTrace:
    orders: tuple[EngineOrderTrace, ...]
    fills: tuple[EngineFillTrace, ...]


class LeanBackend(Protocol):
    def run(self, job: BacktestJob) -> EngineRunTrace | EngineFailure: ...


@dataclass(frozen=True, slots=True)
class AdapterPolicy:
    runtime: BacktestRuntimeIdentity
    max_orders: int
    max_bars: int
    max_order_bar_evaluations: int = 5_000_000

    def __post_init__(self) -> None:
        if self.runtime.engine is not BacktestEngineKind.LEAN:
            raise ValueError("LEAN adapter requires a LEAN runtime")
        if (
            self.max_orders < 1
            or self.max_bars < 1
            or self.max_order_bar_evaluations < 1
        ):
            raise ValueError("LEAN adapter limits must be positive")


@dataclass(frozen=True, slots=True)
class WorkerError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    error: WorkerError


@dataclass(frozen=True, slots=True)
class WorkerSuccess:
    value: BacktestResult


@dataclass(frozen=True, slots=True)
class LeanAdapter:
    policy: AdapterPolicy
    backend: LeanBackend
    clock: Callable[[], datetime]

    def run(self, job: BacktestJob) -> WorkerSuccess | WorkerFailure:
        rejection = self._preflight(job)
        if rejection is not None:
            return rejection
        try:
            trace = self.backend.run(job)
        except Exception:
            return _failure("engine_unavailable", "LEAN engine failed safely")
        if isinstance(trace, EngineFailure):
            return _failure(trace.code, trace.message)
        generated_at = self.clock()
        if generated_at > job.deadline:
            return _failure("deadline_expired", "LEAN job deadline expired")
        try:
            result = _map_result(job, trace, generated_at=generated_at)
            result.validate_against(job)
        except (ArithmeticError, KeyError, ValueError):
            return _failure(
                "invalid_engine_output",
                "LEAN output failed canonical validation",
            )
        return WorkerSuccess(result)

    def _preflight(self, job: BacktestJob) -> WorkerFailure | None:
        if job.runtime != self.policy.runtime:
            return _failure("runtime_mismatch", "LEAN runtime identity changed")
        if (
            len(job.orders) > self.policy.max_orders
            or len(job.dataset.bars) > self.policy.max_bars
            or len(job.orders) * len(job.dataset.bars)
            > self.policy.max_order_bar_evaluations
        ):
            return _failure("job_too_large", "LEAN job exceeds configured limits")
        now = self.clock()
        if now < job.requested_at:
            return _failure("job_not_ready", "LEAN job is not ready")
        if now > job.deadline:
            return _failure("deadline_expired", "LEAN job deadline expired")
        return None


def compute_runtime_hash(root: Path) -> str:
    """Hash adapter, lock, build policy, and the shared canonical contracts."""

    digest = hashlib.sha256()
    paths = [root / name for name in _RUNTIME_FILES]
    for tree_name in _RUNTIME_TREES:
        tree = root / tree_name
        if not tree.is_dir():
            raise ValueError(f"LEAN runtime tree is missing: {tree_name}")
        paths.extend(path for path in tree.rglob("*") if path.is_file())
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if not path.is_file():
            raise ValueError(f"LEAN runtime file is missing: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    contracts = root.parents[1] / "packages" / "contracts"
    contract_files = [
        contracts / "pyproject.toml",
        *sorted((contracts / "src").rglob("*.py")),
    ]
    for path in contract_files:
        if not path.is_file():
            raise ValueError("canonical contracts runtime input is missing")
        name = path.relative_to(root.parents[1]).as_posix()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _map_result(
    job: BacktestJob,
    trace: EngineRunTrace,
    *,
    generated_at: datetime,
) -> BacktestResult:
    _validate_trace(job, trace)
    orders = {item.order_id: item for item in job.orders}
    bars = {item.bar_id: item for item in job.dataset.bars}
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    fills = tuple(
        sorted(
            (_map_fill(job, item, orders, bars, instruments) for item in trace.fills),
            key=lambda item: (item.occurred_at, item.fill_id.hex),
        )
    )
    outcomes = _map_outcomes(job, fills)
    final_cash, final_positions = _project(job, fills)
    trace_hash = _trace_hash(trace)
    return BacktestResult.create(
        result_id=uuid5(
            NAMESPACE_URL,
            f"stonks-lean-result:{job.job_hash}:{trace_hash}:{generated_at.isoformat()}",
        ),
        job=job,
        order_outcomes=outcomes,
        fills=fills,
        final_cash=final_cash,
        final_positions=final_positions,
        total_fees=sum((item.fees for item in fills), _ZERO),
        generated_at=generated_at,
    )


def _validate_trace(job: BacktestJob, trace: EngineRunTrace) -> None:
    expected = {item.order_id for item in job.orders}
    actual = tuple(item.order_id for item in trace.orders)
    fill_ids = tuple(item.external_fill_id for item in trace.fills)
    if set(actual) != expected or len(actual) != len(set(actual)):
        raise ValueError("LEAN order trace coverage changed")
    if len(fill_ids) != len(set(fill_ids)) or any(not item for item in fill_ids):
        raise ValueError("LEAN fill trace IDs are invalid")
    if any(
        item.order_id not in expected
        or not item.quantity.is_finite()
        or not item.raw_price.is_finite()
        or not item.raw_fees.is_finite()
        or item.quantity <= 0
        or item.raw_price <= 0
        or item.raw_fees < 0
        or not _SHA256.fullmatch(item.raw_event_hash)
        for item in trace.fills
    ):
        raise ValueError("LEAN fill trace is invalid")


def _map_fill(
    job: BacktestJob,
    trace: EngineFillTrace,
    orders: dict[UUID, BacktestOrder],
    bars: dict[UUID, BacktestBar],
    instruments: dict[UUID, BacktestInstrument],
) -> BacktestFill:
    order = orders[trace.order_id]
    bar = bars[trace.source_bar_id]
    instrument = instruments[order.instrument_id]
    price = canonical_fill_price(order, bar, instrument, job.cost_model)
    if price is None:
        raise ValueError("LEAN fill has no canonical price")
    fee = canonical_fill_fee(trace.quantity, price, job.cost_model)
    fill_id = uuid5(
        NAMESPACE_URL,
        f"stonks-lean-fill:{job.input_hash}:{trace.order_id}:{trace.source_bar_id}:{trace.external_fill_id}",
    )
    return BacktestFill.create(
        fill_id=fill_id,
        order_id=trace.order_id,
        order_hash=order.order_hash,
        instrument_id=order.instrument_id,
        side=order.side,
        quantity=trace.quantity,
        quantity_quantum=instrument.quantity_quantum,
        price=price,
        price_quantum=instrument.price_quantum,
        fee_currency=instrument.currency,
        fees=fee,
        fee_quantum=job.cost_model.fee_quantum,
        slippage=price - bar.open,
        occurred_at=trace.occurred_at,
        source_bar_id=trace.source_bar_id,
        external_ref=(
            f"lean:{trace.external_fill_id}:raw-sha256:{trace.raw_event_hash}"
        ),
    )


def _map_outcomes(
    job: BacktestJob,
    fills: tuple[BacktestFill, ...],
) -> tuple[BacktestOrderOutcome, ...]:
    totals: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    for fill in fills:
        totals[fill.order_id] += fill.quantity
    outcomes = []
    for order in job.orders:
        filled = totals[order.order_id]
        if filled > order.quantity:
            raise ValueError("LEAN filled quantity exceeds command")
        status, reason = canonical_outcome_status(order, filled, job.dataset.as_of)
        outcomes.append(
            BacktestOrderOutcome(
                order_id=order.order_id,
                order_hash=order.order_hash,
                status=status,
                command_quantity=order.quantity,
                filled_quantity=filled,
                remaining_quantity=order.quantity - filled,
                reason=reason,
            )
        )
    return tuple(sorted(outcomes, key=lambda item: item.order_id.hex))


def _project(
    job: BacktestJob,
    fills: tuple[BacktestFill, ...],
) -> tuple[tuple[BacktestCashBalance, ...], tuple[BacktestPosition, ...]]:
    cash = {item.currency: item.amount for item in job.initial_cash}
    positions = {item.instrument_id: item.quantity for item in job.initial_positions}
    for fill in fills:
        notional = fill.quantity * fill.price
        if fill.side is BacktestOrderSide.BUY:
            cash[fill.fee_currency] -= notional + fill.fees
            positions[fill.instrument_id] += fill.quantity
        else:
            cash[fill.fee_currency] += notional - fill.fees
            positions[fill.instrument_id] -= fill.quantity
    opening_cash = {item.currency: item for item in job.initial_cash}
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    final_cash = tuple(
        BacktestCashBalance(
            currency=currency,
            amount=amount,
            quantum=opening_cash[currency].quantum,
        )
        for currency, amount in sorted(cash.items())
    )
    final_positions = tuple(
        BacktestPosition(
            instrument_id=instrument_id,
            quantity=quantity,
            quantity_quantum=instruments[instrument_id].quantity_quantum,
        )
        for instrument_id, quantity in sorted(
            positions.items(), key=lambda item: item[0].hex
        )
    )
    return final_cash, final_positions


def _trace_hash(trace: EngineRunTrace) -> str:
    return stable_payload_hash(
        {
            "orders": [
                {
                    "order_id": str(item.order_id),
                    "status": item.status.value,
                    "reason": item.reason,
                }
                for item in trace.orders
            ],
            "fills": [
                {
                    "external_fill_id": item.external_fill_id,
                    "order_id": str(item.order_id),
                    "quantity": str(item.quantity),
                    "raw_price": str(item.raw_price),
                    "raw_fees": str(item.raw_fees),
                    "raw_event_hash": item.raw_event_hash,
                    "occurred_at": item.occurred_at.isoformat(),
                    "source_bar_id": str(item.source_bar_id),
                }
                for item in trace.fills
            ],
        }
    )


def _failure(code: str, message: str) -> WorkerFailure:
    return WorkerFailure(WorkerError(code=code, message=message))
