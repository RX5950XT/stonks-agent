from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import scripts.capacity_probe_measurement as measurement
from scripts.capacity_probe_common import ProbeError
from scripts.capacity_probe_database import validate_capacity_database_url
from scripts.capacity_probe_local import (
    asgi_security_contract_once,
    forecast_contract_once,
    paper_cycle_once,
)
from scripts.capacity_probe_measurement import (
    _resident_bytes,
    measure_samples,
    nearest_rank_p95,
    workload_report,
)
from stonks_agent.config.capacity import load_capacity_policy
from stonks_agent.domain.capacity import CapacitySample, ResourceObservation

POLICY = load_capacity_policy(
    Path(__file__).resolve().parents[2] / "config" / "capacity.yaml"
)
pytestmark = pytest.mark.performance


def test_nearest_rank_p95_is_integer_and_uses_nearest_rank() -> None:
    samples = tuple(range(1, 21))

    assert nearest_rank_p95(samples) == 19
    assert nearest_rank_p95((7,)) == 7


@pytest.mark.parametrize("samples", [(), (1, -1), (True,)])
def test_nearest_rank_p95_rejects_missing_or_invalid_samples(
    samples: tuple[object, ...],
) -> None:
    with pytest.raises(ProbeError, match="timing samples are invalid"):
        nearest_rank_p95(samples)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:password@db.internal/stonks_capacity",
        "postgresql+psycopg://user:password@127.0.0.1/production",
        "postgresql+psycopg://probe:password@127.0.0.1/stonks_capacity",
        "postgresql+psycopg://user:password@127.0.0.1/stonks_capacity?sslmode=disable",
        "sqlite:///stonks_capacity",
    ],
)
def test_database_url_rejection_never_echoes_credentials(url: str) -> None:
    with pytest.raises(ProbeError) as captured:
        validate_capacity_database_url(url)

    rendered = str(captured.value)
    assert "password" not in rendered
    assert "user" not in rendered
    assert url not in rendered


def test_database_url_accepts_only_explicit_loopback_test_database() -> None:
    parsed = validate_capacity_database_url(
        "postgresql+psycopg://probe@127.0.0.1:5432/stonks_capacity"
    )

    assert parsed.database == "stonks_capacity"
    assert parsed.host == "127.0.0.1"


def test_measurement_is_bounded_and_uses_thread_pool() -> None:
    seen: list[int] = []

    def operation(index: int) -> None:
        seen.append(index)

    report = measure_samples(
        operation,
        sample_count=8,
        concurrency=2,
        maximum_sample_us=1_000_000,
        executor_type=ThreadPoolExecutor,
    )

    assert sorted(seen) == list(range(8))
    assert report.sample_count == 8
    assert len(report.samples_us) == 8
    assert all(
        type(sample) is int and 0 <= sample <= 1_000_000 for sample in report.samples_us
    )
    assert report.p95_us == nearest_rank_p95(report.samples_us)


def test_measurement_fails_closed_when_sample_exceeds_bound() -> None:
    with pytest.raises(ProbeError, match="timing sample exceeded bound"):
        measure_samples(
            lambda _index: None,
            sample_count=1,
            concurrency=1,
            maximum_sample_us=0,
            clock_ns=iter((0, 1_001)).__next__,
        )


def test_process_memory_observation_is_available() -> None:
    assert _resident_bytes() > 0


def test_process_memory_observation_fails_closed_when_platform_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(measurement.platform, "system", lambda: "Windows")
    monkeypatch.setattr(measurement, "_windows_resident_bytes", lambda: 0)

    with pytest.raises(ProbeError, match="process memory observation failed"):
        measurement._resident_bytes()


def test_windows_memory_api_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("sensitive platform detail")

    monkeypatch.setattr(ctypes, "WinDLL", unavailable, raising=False)

    with pytest.raises(ProbeError, match="process memory observation failed") as error:
        measurement._windows_resident_bytes()

    assert "sensitive" not in str(error.value)


def test_local_workloads_revalidate_archived_contracts() -> None:
    asgi_security_contract_once(0)
    first_cycle = paper_cycle_once(0)
    second_cycle = paper_cycle_once(0)
    first_forecast = forecast_contract_once(0)
    second_forecast = forecast_contract_once(0)

    assert first_cycle == second_cycle
    assert first_forecast == second_forecast
    assert len(first_cycle) == 64
    assert len(first_forecast) == 64


def test_workload_claims_include_interval_peak_concurrency() -> None:
    resources = ResourceObservation(
        cpu_millicores=100,
        ram_mebibytes=64,
        pid_count=1,
        process_count=1,
        in_flight_count=1,
    )
    samples = (
        CapacitySample(
            sample_id=1,
            started_at_microseconds=0,
            finished_at_microseconds=10,
            resources=resources,
        ),
        CapacitySample(
            sample_id=2,
            started_at_microseconds=2,
            finished_at_microseconds=5,
            resources=resources,
        ),
    )
    definition = POLICY.workloads[0].model_copy(update={"sample_count": 2})

    report = workload_report(POLICY, definition, samples)

    assert report.claimed_p95_microseconds == 10
    assert report.claimed_wall_microseconds == 10
    assert report.claimed_peak_resources.in_flight_count == 2
    assert report.claimed_pass is True
