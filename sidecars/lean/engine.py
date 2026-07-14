"""Constrained subprocess adapter for the pinned QuantConnect LEAN engine."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from sidecars.lean.adapter import (
    EngineFailure,
    EngineFillTrace,
    EngineOrderTrace,
    EngineRunTrace,
)
from stonks_contracts.backtest import (
    BacktestBar,
    BacktestCalendarIndex,
    BacktestInstrument,
    BacktestJob,
    BacktestOrder,
    BacktestOrderSide,
    BacktestOrderType,
    BacktestTimeInForce,
)
from stonks_contracts.backtest_math import (
    canonical_fill_price,
    canonical_outcome_status,
    floor_quantum,
)
from stonks_contracts.common import stable_payload_hash

LEAN_ENGINE_VERSION = "17917+c22774e49ee80ecef5ca84f57616f6b66fad8bc5"
_NEW_YORK = ZoneInfo("America/New_York")
_SUPPORTED_MICS = frozenset({"XNAS", "XNYS"})
_MAX_TRACE_EVENTS_PER_CHILD = 8
_TRACE_KEYS = frozenset({"engine_version", "events"})
_EVENT_KEYS = frozenset(
    {
        "child_id",
        "lean_order_id",
        "order_event_id",
        "status",
        "direction",
        "fill_quantity",
        "fill_price",
        "order_fee",
        "utc_time",
    }
)


class _ScheduleTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduledChild:
    canonical_order_id: UUID
    child_id: str
    symbol: str
    side: BacktestOrderSide
    order_type: BacktestOrderType
    time_in_force: BacktestTimeInForce
    quantity: Decimal
    native_limit_price: Decimal | None
    source_bar_id: UUID
    source_opens_at: datetime


@dataclass(frozen=True, slots=True)
class LeanProcessRequest:
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: float
    trace_path: Path
    child_ids: tuple[str, ...]


class LeanProcessRunner(Protocol):
    def run(self, process: LeanProcessRequest) -> int: ...


class SubprocessRunner:
    """Run LEAN without a shell, inherited secrets, stdin, or captured output."""

    def run(self, process: LeanProcessRequest) -> int:
        try:
            completed = subprocess.run(
                process.command,
                cwd=process.cwd,
                env=process.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=process.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("LEAN process exceeded its deadline") from error
        return completed.returncode


class LeanEngineBackend:
    """Run one fresh, backtest-only LEAN process and return authority-free traces."""

    def __init__(
        self,
        *,
        launcher: Path = Path("/opt/lean/QuantConnect.Lean.Launcher.dll"),
        algorithm: Path = Path("/opt/lean/Stonks.Lean.Algorithm.dll"),
        template: Path | None = None,
        base_data: Path | None = Path("/opt/lean/Data"),
        workspace_root: Path = Path("/tmp/lean-jobs"),
        process_runner: LeanProcessRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        max_schedule_children: int = 100_000,
        max_engine_seconds: int = 120,
        max_trace_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if min(max_schedule_children, max_engine_seconds, max_trace_bytes) < 1:
            raise ValueError("LEAN runtime limits must be positive")
        self.launcher = launcher.resolve()
        self.algorithm = algorithm.resolve()
        self.template = template or Path(__file__).with_name(
            "appsettings.template.json"
        )
        self.base_data = base_data
        self.workspace_root = workspace_root
        self.process_runner = process_runner or SubprocessRunner()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_schedule_children = max_schedule_children
        self.max_engine_seconds = max_engine_seconds
        self.max_trace_bytes = max_trace_bytes
        self.engine_version = LEAN_ENGINE_VERSION

    def run(self, job: BacktestJob) -> EngineRunTrace | EngineFailure:
        rejection = self._preflight(job)
        if rejection is not None:
            return rejection
        try:
            schedule = _schedule(job, self.max_schedule_children)
        except _ScheduleTooLargeError:
            return EngineFailure("job_too_large", "LEAN native schedule exceeds limits")
        except ValueError:
            return EngineFailure("engine_unsupported_job", "LEAN job mapping rejected")
        if not schedule:
            return _empty_trace(job)
        try:
            return self._run_process(job, schedule)
        except TimeoutError:
            return EngineFailure("engine_timeout", "LEAN process deadline exceeded")
        except (OSError, ValueError, json.JSONDecodeError):
            return EngineFailure(
                "engine_invalid_output",
                "LEAN process output failed strict validation",
            )

    def _preflight(self, job: BacktestJob) -> EngineFailure | None:
        if job.runtime.engine_version != self.engine_version:
            return EngineFailure("runtime_mismatch", "LEAN engine version changed")
        now = self.clock()
        if now > job.deadline:
            return EngineFailure("deadline_expired", "LEAN job deadline expired")
        try:
            _validate_supported_job(job)
        except ValueError:
            return EngineFailure(
                "engine_unsupported_job",
                "LEAN only accepts bounded canonical US equity replay",
            )
        if not self.launcher.is_file() or not self.algorithm.is_file():
            return EngineFailure("engine_unavailable", "LEAN runtime files are missing")
        return None

    def _run_process(
        self,
        job: BacktestJob,
        schedule: tuple[ScheduledChild, ...],
    ) -> EngineRunTrace | EngineFailure:
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="job-",
            dir=self.workspace_root,
        ) as temporary:
            root = Path(temporary)
            _write_job_inputs(root, job, schedule, self.base_data)
            config = _write_config(root, self.template, self.algorithm)
            timeout = min(
                float(self.max_engine_seconds),
                (job.deadline - self.clock()).total_seconds(),
            )
            if timeout <= 0:
                return EngineFailure("deadline_expired", "LEAN job deadline expired")
            process = LeanProcessRequest(
                command=("dotnet", str(self.launcher), "--config", str(config)),
                cwd=root,
                env=_process_environment(),
                timeout_seconds=timeout,
                trace_path=root / "trace.json",
                child_ids=tuple(item.child_id for item in schedule),
            )
            if self.process_runner.run(process) != 0:
                return EngineFailure("engine_failed", "LEAN process failed safely")
            trace_path = process.trace_path
            if (
                not trace_path.is_file()
                or trace_path.stat().st_size > self.max_trace_bytes
            ):
                raise ValueError("LEAN trace is missing or too large")
            payload = json.loads(trace_path.read_text("utf-8"))
            return _parse_trace(job, schedule, payload)


def _schedule(
    job: BacktestJob,
    max_children: int,
) -> tuple[ScheduledChild, ...]:
    if max_children < 1:
        raise ValueError("LEAN schedule exceeds configured limits")
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    calendar = BacktestCalendarIndex.create(job.dataset.calendar)
    bars = sorted(job.dataset.bars, key=lambda item: (item.opens_at, item.bar_id.hex))
    remaining = {item.order_id: item.quantity for item in job.orders}
    attempts: set[UUID] = set()
    sessions = _day_sessions(job, calendar, instruments)
    child_counts: defaultdict[UUID, int] = defaultdict(int)
    result: list[ScheduledChild] = []
    for bar in bars:
        available = _bar_capacity(job, bar)
        for order in job.orders:
            available = _schedule_order_on_bar(
                job,
                order,
                bar,
                available,
                remaining,
                attempts,
                sessions,
                calendar,
                child_counts,
                result,
                max_children,
            )
    return tuple(sorted(result, key=lambda item: (item.source_opens_at, item.child_id)))


def _schedule_order_on_bar(
    job: BacktestJob,
    order: BacktestOrder,
    bar: BacktestBar,
    available: Decimal,
    remaining: dict[UUID, Decimal],
    attempts: set[UUID],
    sessions: dict[UUID, date],
    calendar: BacktestCalendarIndex,
    child_counts: defaultdict[UUID, int],
    result: list[ScheduledChild],
    maximum: int,
) -> Decimal:
    if remaining[order.order_id] <= 0 or order.order_id in attempts:
        return available
    if not _is_order_opportunity(order, bar):
        return available
    instrument = next(
        item
        for item in job.dataset.instruments
        if item.instrument_id == bar.instrument_id
    )
    session = calendar.session_for_bar(bar, instrument)
    if session is None:
        raise ValueError("LEAN bar session mapping changed")
    if (
        order.time_in_force is BacktestTimeInForce.DAY
        and sessions[order.order_id] != session.session_date
    ):
        return available
    if order.time_in_force is BacktestTimeInForce.IOC:
        attempts.add(order.order_id)
    price = canonical_fill_price(order, bar, instrument, job.cost_model)
    if not bar.tradable or price is None:
        return available
    quantity = min(remaining[order.order_id], available)
    if quantity <= 0:
        return available
    if len(result) >= maximum:
        raise _ScheduleTooLargeError("LEAN schedule exceeds configured limits")
    child_counts[order.order_id] += 1
    result.append(
        _scheduled_child(
            order,
            bar,
            instrument.symbol,
            instrument.price_quantum,
            quantity,
            child_counts[order.order_id],
        )
    )
    remaining[order.order_id] -= quantity
    return available - quantity


def _day_sessions(
    job: BacktestJob,
    calendar: BacktestCalendarIndex,
    instruments: Mapping[UUID, BacktestInstrument],
) -> dict[UUID, date]:
    result: dict[UUID, date] = {}
    for order in job.orders:
        if order.time_in_force is not BacktestTimeInForce.DAY:
            continue
        first = calendar.first_session_date(order, instruments[order.instrument_id])
        if first is None:
            raise ValueError("LEAN DAY order has no canonical session")
        result[order.order_id] = first
    return result


def _bar_capacity(job: BacktestJob, bar: BacktestBar) -> Decimal:
    instrument = next(
        item
        for item in job.dataset.instruments
        if item.instrument_id == bar.instrument_id
    )
    return floor_quantum(
        bar.volume * job.cost_model.max_volume_participation,
        instrument.quantity_quantum,
    )


def _is_order_opportunity(order: BacktestOrder, bar: BacktestBar) -> bool:
    return bool(
        bar.instrument_id == order.instrument_id
        and order.issued_at < bar.opens_at < order.valid_until
    )


def _scheduled_child(
    order: BacktestOrder,
    bar: BacktestBar,
    symbol: str,
    price_quantum: Decimal,
    quantity: Decimal,
    index: int,
) -> ScheduledChild:
    limit = order.limit_price
    if order.order_type is BacktestOrderType.LIMIT and limit is not None:
        if order.side is BacktestOrderSide.BUY:
            limit = max(limit, bar.open + price_quantum)
        else:
            limit = min(limit, bar.open - price_quantum)
    return ScheduledChild(
        canonical_order_id=order.order_id,
        child_id=f"B{order.order_id.hex}-{index:07d}",
        symbol=symbol,
        side=order.side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        quantity=quantity,
        native_limit_price=limit,
        source_bar_id=bar.bar_id,
        source_opens_at=bar.opens_at,
    )


def _validate_supported_job(job: BacktestJob) -> None:
    symbols: set[str] = set()
    for instrument in job.dataset.instruments:
        if (
            instrument.asset_class != "equity"
            or instrument.currency != "USD"
            or instrument.mic not in _SUPPORTED_MICS
            or instrument.symbol in symbols
            or instrument.quantity_quantum != instrument.quantity_quantum.to_integral()
            or instrument.price_quantum < Decimal("0.0001")
        ):
            raise ValueError("unsupported LEAN instrument")
        symbols.add(instrument.symbol)
    for bar in job.dataset.bars:
        values = (bar.open, bar.high, bar.low, bar.close)
        if any(value * 10_000 != (value * 10_000).to_integral() for value in values):
            raise ValueError("LEAN equity data supports four price decimals")
        if bar.volume != bar.volume.to_integral():
            raise ValueError("LEAN equity volume must be integral")
    if any(order.quantity != order.quantity.to_integral() for order in job.orders):
        raise ValueError("LEAN equity order quantity must be integral")


def _write_job_inputs(
    root: Path,
    job: BacktestJob,
    schedule: tuple[ScheduledChild, ...],
    base_data: Path | None,
) -> None:
    data = root / "data"
    _copy_metadata(data, base_data)
    _write_equity_metadata(data, job)
    _write_equity_bars(data, job)
    payload = {
        "engine_version": LEAN_ENGINE_VERSION,
        "start_date": min(item.opens_at for item in job.dataset.bars)
        .date()
        .isoformat(),
        "end_date": max(item.closes_at for item in job.dataset.bars).date().isoformat(),
        "cash": "1000000000000000",
        "instruments": [
            {"symbol": item.symbol}
            for item in sorted(job.dataset.instruments, key=lambda item: item.symbol)
        ],
        "children": [_child_payload(item) for item in schedule],
    }
    (root / "schedule.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _copy_metadata(data: Path, base_data: Path | None) -> None:
    targets = (
        ("market-hours", "market-hours-database.json"),
        ("symbol-properties", "security-database.csv"),
        ("symbol-properties", "symbol-properties-database.csv"),
    )
    for directory, filename in targets:
        target = data / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        source = base_data / directory / filename if base_data is not None else None
        if source is not None and source.is_file():
            shutil.copy2(source, target)
        elif filename.endswith(".json"):
            target.write_text("{}", encoding="utf-8")
        else:
            target.write_text("", encoding="utf-8")


def _write_equity_metadata(data: Path, job: BacktestJob) -> None:
    map_root = data / "equity" / "usa" / "map_files"
    factor_root = data / "equity" / "usa" / "factor_files"
    map_root.mkdir(parents=True, exist_ok=True)
    factor_root.mkdir(parents=True, exist_ok=True)
    for instrument in job.dataset.instruments:
        symbol = instrument.symbol.lower()
        (map_root / f"{symbol}.csv").write_text(
            f"19980101,{symbol},Q\n20501231,{symbol},Q\n",
            encoding="ascii",
        )
        (factor_root / f"{symbol}.csv").write_text(
            "19980101,1,1,1\n20501231,1,1,1\n",
            encoding="ascii",
        )


def _write_equity_bars(data: Path, job: BacktestJob) -> None:
    instruments = {item.instrument_id: item for item in job.dataset.instruments}
    grouped: defaultdict[tuple[str, str], list[BacktestBar]] = defaultdict(list)
    for bar in job.dataset.bars:
        symbol = instruments[bar.instrument_id].symbol.lower()
        local_date = bar.opens_at.astimezone(_NEW_YORK).strftime("%Y%m%d")
        grouped[(symbol, local_date)].append(bar)
    for (symbol, local_date), bars in grouped.items():
        directory = data / "equity" / "usa" / "minute" / symbol
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / f"{local_date}_trade.zip"
        entry = f"{local_date}_{symbol}_minute_trade.csv"
        rows = "\n".join(
            _lean_bar_row(item) for item in sorted(bars, key=lambda x: x.opens_at)
        )
        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as target:
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, rows.encode("ascii"))


def _lean_bar_row(bar: BacktestBar) -> str:
    local = bar.opens_at.astimezone(_NEW_YORK)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    milliseconds = int((local - midnight).total_seconds() * 1000)
    scaled = [
        str(int(value * 10_000)) for value in (bar.open, bar.high, bar.low, bar.close)
    ]
    return ",".join((str(milliseconds), *scaled, str(int(bar.volume))))


def _child_payload(child: ScheduledChild) -> dict[str, str | None]:
    return {
        "child_id": child.child_id,
        "canonical_order_id": str(child.canonical_order_id),
        "symbol": child.symbol,
        "side": child.side.value,
        "order_type": child.order_type.value,
        "time_in_force": child.time_in_force.value,
        "quantity": str(child.quantity),
        "native_limit_price": (
            str(child.native_limit_price)
            if child.native_limit_price is not None
            else None
        ),
        "source_bar_id": str(child.source_bar_id),
        "source_opens_at_utc": child.source_opens_at.isoformat(),
        "submit_at_utc": (child.source_opens_at - timedelta(minutes=1)).isoformat(),
    }


def _write_config(root: Path, template: Path, algorithm: Path) -> Path:
    config = json.loads(template.read_text("utf-8"))
    config.update(
        {
            "algorithm-location": str(algorithm),
            "data-folder": str(root / "data"),
            "results-destination-folder": str(root / "results"),
            "parameters": {
                "schedule_path": str(root / "schedule.json"),
                "trace_path": str(root / "trace.json"),
            },
        }
    )
    (root / "results").mkdir()
    target = root / "appsettings.json"
    target.write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return target


def _process_environment() -> dict[str, str]:
    return {
        "COMPlus_EnableDiagnostics": "0",
        "DOTNET_EnableDiagnostics": "0",
        "DOTNET_ROOT": "/usr/share/dotnet",
        "HOME": "/tmp/sidecar",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }


def _parse_trace(
    job: BacktestJob,
    schedule: tuple[ScheduledChild, ...],
    payload: object,
) -> EngineRunTrace:
    if not isinstance(payload, dict) or frozenset(payload) != _TRACE_KEYS:
        raise ValueError("LEAN trace envelope changed")
    if payload["engine_version"] != LEAN_ENGINE_VERSION:
        raise ValueError("LEAN trace engine identity changed")
    events = payload["events"]
    if (
        not isinstance(events, list)
        or len(events) > len(schedule) * _MAX_TRACE_EVENTS_PER_CHILD
    ):
        raise ValueError("LEAN trace event count is invalid")
    children = {item.child_id: item for item in schedule}
    fills = _parse_fill_events(events, children)
    totals: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    for fill in fills:
        totals[fill.order_id] += fill.quantity
    orders = tuple(
        _order_trace(item, totals[item.order_id], job.dataset.as_of)
        for item in job.orders
    )
    return EngineRunTrace(orders=orders, fills=fills)


def _parse_fill_events(
    events: list[object],
    children: Mapping[str, ScheduledChild],
) -> tuple[EngineFillTrace, ...]:
    result: list[EngineFillTrace] = []
    seen: set[str] = set()
    for value in events:
        if not isinstance(value, dict) or frozenset(value) != _EVENT_KEYS:
            raise ValueError("LEAN trace event shape changed")
        child_id = value["child_id"]
        if not isinstance(child_id, str) or child_id not in children:
            raise ValueError("LEAN trace child identity changed")
        quantity = abs(Decimal(_required_text(value, "fill_quantity")))
        if quantity == 0:
            continue
        if value["status"] not in {"Filled", "PartiallyFilled"}:
            raise ValueError("LEAN non-fill event has quantity")
        child = children[child_id]
        expected_direction = "Buy" if child.side is BacktestOrderSide.BUY else "Sell"
        if value["direction"] != expected_direction:
            raise ValueError("LEAN fill direction changed")
        identifier = f"L-{_required_int(value, 'lean_order_id')}-{_required_int(value, 'order_event_id')}"
        if identifier in seen:
            raise ValueError("LEAN fill identity duplicated")
        seen.add(identifier)
        result.append(
            EngineFillTrace(
                external_fill_id=identifier,
                order_id=child.canonical_order_id,
                quantity=quantity,
                raw_price=Decimal(_required_text(value, "fill_price")),
                raw_fees=Decimal(_required_text(value, "order_fee")),
                raw_event_hash=stable_payload_hash(value),
                occurred_at=child.source_opens_at,
                source_bar_id=child.source_bar_id,
            )
        )
    return tuple(
        sorted(result, key=lambda item: (item.occurred_at, item.external_fill_id))
    )


def _required_text(value: Mapping[str, object], key: str) -> str:
    selected = value[key]
    if not isinstance(selected, str) or not selected:
        raise ValueError(f"LEAN trace {key} is invalid")
    return selected


def _required_int(value: Mapping[str, object], key: str) -> int:
    selected = value[key]
    if not isinstance(selected, int) or selected < 0:
        raise ValueError(f"LEAN trace {key} is invalid")
    return selected


def _order_trace(
    order: BacktestOrder,
    filled: Decimal,
    as_of: datetime,
) -> EngineOrderTrace:
    status, reason = canonical_outcome_status(order, filled, as_of)
    return EngineOrderTrace(order_id=order.order_id, status=status, reason=reason)


def _empty_trace(job: BacktestJob) -> EngineRunTrace:
    return EngineRunTrace(
        orders=tuple(
            _order_trace(item, Decimal("0"), job.dataset.as_of) for item in job.orders
        ),
        fills=(),
    )
