"""Bounded monotonic measurement and resource evidence helpers."""

from __future__ import annotations

import math
import platform
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib import import_module
from threading import Lock
from time import perf_counter_ns, process_time_ns

from scripts.capacity_probe_common import ProbeError
from stonks_agent.domain.capacity import (
    MAX_MICROSECONDS,
    CapacityPolicy,
    CapacitySample,
    CapacityWorkloadDefinition,
    CapacityWorkloadReport,
    ResourceObservation,
)


@dataclass(frozen=True, slots=True)
class MeasuredSamples:
    sample_count: int
    samples_us: tuple[int, ...]
    p95_us: int


@dataclass(slots=True)
class _InFlight:
    current: int = 0
    _guard: Lock = field(default_factory=Lock, init=False, repr=False)

    def enter(self) -> int:
        with self._guard:
            self.current += 1
            return self.current

    def leave(self) -> None:
        with self._guard:
            self.current -= 1


def nearest_rank_p95(samples: Sequence[int]) -> int:
    if not samples or any(type(value) is not int or value < 0 for value in samples):
        raise ProbeError("timing samples are invalid")
    ordered = sorted(samples)
    rank = math.ceil(95 * len(ordered) / 100)
    return ordered[rank - 1]


def measure_samples(
    operation: Callable[[int], object],
    *,
    sample_count: int,
    concurrency: int,
    maximum_sample_us: int,
    clock_ns: Callable[[], int] = perf_counter_ns,
    executor_type: type[ThreadPoolExecutor] = ThreadPoolExecutor,
) -> MeasuredSamples:
    if (
        type(sample_count) is not int
        or not 1 <= sample_count <= 100
        or type(concurrency) is not int
        or not 1 <= concurrency <= min(sample_count, 16)
        or type(maximum_sample_us) is not int
        or not 0 <= maximum_sample_us <= 60_000_000
    ):
        raise ProbeError("measurement bounds are invalid")

    def timed(index: int) -> int:
        started = clock_ns()
        operation(index)
        elapsed_ns = clock_ns() - started
        if type(elapsed_ns) is not int or elapsed_ns < 0:
            raise ProbeError("monotonic timing is invalid")
        elapsed_us = (elapsed_ns + 999) // 1_000
        if elapsed_us > maximum_sample_us:
            raise ProbeError("timing sample exceeded bound")
        return elapsed_us

    with executor_type(max_workers=concurrency) as executor:
        samples = tuple(executor.map(timed, range(sample_count)))
    return MeasuredSamples(
        sample_count=sample_count,
        samples_us=samples,
        p95_us=nearest_rank_p95(samples),
    )


def measure_capacity_workload(
    policy: CapacityPolicy,
    definition: CapacityWorkloadDefinition,
    operation: Callable[[int], object],
) -> CapacityWorkloadReport:
    in_flight = _InFlight()
    origin_ns = perf_counter_ns()

    def timed(index: int) -> CapacitySample:
        active = in_flight.enter()
        started_ns = perf_counter_ns()
        cpu_started_ns = process_time_ns()
        try:
            operation(index)
            finished_ns = perf_counter_ns()
            cpu_finished_ns = process_time_ns()
            started_us = max(0, (started_ns - origin_ns) // 1_000)
            finished_us = max(started_us + 1, (finished_ns - origin_ns + 999) // 1_000)
            if finished_us > MAX_MICROSECONDS:
                raise ProbeError("capacity workload exceeded timing bound")
            resources = _resource_observation(
                cpu_ns=max(0, cpu_finished_ns - cpu_started_ns),
                wall_ns=max(1, finished_ns - started_ns),
                in_flight=active,
            )
            return CapacitySample(
                sample_id=index + 1,
                started_at_microseconds=started_us,
                finished_at_microseconds=finished_us,
                resources=resources,
            )
        finally:
            in_flight.leave()

    with ThreadPoolExecutor(max_workers=definition.concurrency) as executor:
        samples = tuple(executor.map(timed, range(definition.sample_count)))
    return workload_report(policy, definition, samples)


def _resource_observation(
    *, cpu_ns: int, wall_ns: int, in_flight: int
) -> ResourceObservation:
    cpu_millicores = min(64_000, math.ceil(cpu_ns * 1_000 / wall_ns))
    ram_mebibytes = min(1_048_576, math.ceil(_resident_bytes() / 1_048_576))
    return ResourceObservation(
        cpu_millicores=cpu_millicores,
        ram_mebibytes=ram_mebibytes,
        pid_count=1,
        process_count=1,
        in_flight_count=in_flight,
    )


def _resident_bytes() -> int:
    platform_name = platform.system()
    if platform_name == "Windows":
        value = _windows_resident_bytes()
        if value <= 0:
            raise ProbeError("process memory observation failed")
        return value
    try:
        resource = import_module("resource")
        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        raise ProbeError("process memory observation failed") from None
    value = maximum if platform_name == "Darwin" else maximum * 1_024
    if value <= 0:
        raise ProbeError("process memory observation failed")
    return value


def _windows_resident_bytes() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = (
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.K32GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        )
        kernel.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel.GetCurrentProcess()
        accepted = kernel.K32GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
        value = int(counters.WorkingSetSize)
        if not accepted or value <= 0:
            raise ProbeError("process memory observation failed")
        return value
    except (AttributeError, OSError, ValueError):
        raise ProbeError("process memory observation failed") from None


def workload_report(
    policy: CapacityPolicy,
    definition: CapacityWorkloadDefinition,
    samples: tuple[CapacitySample, ...],
) -> CapacityWorkloadReport:
    durations = sorted(
        sample.finished_at_microseconds - sample.started_at_microseconds
        for sample in samples
    )
    p95 = durations[(95 * len(durations) + 99) // 100 - 1]
    wall = max(sample.finished_at_microseconds for sample in samples) - min(
        sample.started_at_microseconds for sample in samples
    )
    peak_concurrency = _interval_peak_concurrency(samples)
    resources = ResourceObservation(
        cpu_millicores=max(sample.resources.cpu_millicores for sample in samples),
        ram_mebibytes=max(sample.resources.ram_mebibytes for sample in samples),
        pid_count=max(sample.resources.pid_count for sample in samples),
        process_count=max(sample.resources.process_count for sample in samples),
        in_flight_count=max(
            peak_concurrency,
            max(sample.resources.in_flight_count for sample in samples),
        ),
    )
    budget = policy.probe_runtime_budget
    passed = all(
        (
            p95 <= definition.p95_ceiling_microseconds,
            wall <= definition.wall_ceiling_microseconds,
            wall <= policy.measurement.wall_clock_ceiling_microseconds,
            peak_concurrency <= definition.concurrency,
            resources.cpu_millicores <= budget.cpu_millicores_ceiling,
            resources.ram_mebibytes <= budget.ram_mebibytes_ceiling,
            resources.pid_count <= budget.pid_ceiling,
            resources.process_count <= budget.process_ceiling,
            resources.in_flight_count <= budget.in_flight_ceiling,
        )
    )
    return CapacityWorkloadReport(
        workload=definition.workload,
        sample_count=definition.sample_count,
        concurrency=definition.concurrency,
        claimed_p95_microseconds=p95,
        claimed_wall_microseconds=wall,
        claimed_peak_resources=resources,
        claimed_pass=passed,
        samples=samples,
    )


def _interval_peak_concurrency(samples: tuple[CapacitySample, ...]) -> int:
    events = sorted(
        (
            *((sample.started_at_microseconds, 1) for sample in samples),
            *((sample.finished_at_microseconds, -1) for sample in samples),
        )
    )
    current = 0
    peak = 0
    for _, change in events:
        current += change
        peak = max(peak, current)
    return peak
