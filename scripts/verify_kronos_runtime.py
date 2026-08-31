"""Run one authenticated actual Kronos CPU forecast and shadow alpha mapping."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.adapters.forecast.kronos import (
    KronosHttpAdapter,
    KronosHttpPolicy,
    KronosWorkerConfiguration,
    load_kronos_worker_configuration,
)
from stonks_agent.application.signals.kronos_to_alpha import (
    KronosToAlphaCommand,
    load_kronos_strategy_configuration,
    map_kronos_to_alpha,
)
from stonks_agent.domain.errors import Failure
from stonks_agent.domain.evaluation import (
    EvaluationCheck,
    EvaluationCheckKind,
    EvaluationCheckStatus,
    EvaluationMetric,
    EvaluationReport,
)
from stonks_agent.domain.signal import (
    AlphaSignal,
    ForecastOutputArtifact,
    ForecastRequest,
    SignalEligibilityDecision,
    evaluate_signal_eligibility,
)
from stonks_agent.domain.strategy import StrategyRegistryEntry
from stonks_agent.entrypoints.gui import prepare_ephemeral_openbb_runtime
from stonks_agent.ports.service_credentials import (
    ServiceCredentialProvider,
    ServiceReceiver,
)
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.kronos import (
    KronosBar,
    KronosSamplingPolicy,
    KronosWorkerRequest,
    VolumeQuality,
)

_REQUEST_ID = UUID("92100000-0000-4000-8000-000000000001")
_RUN_ID = UUID("92100000-0000-4000-8000-000000000002")
_JOB_ID = UUID("92100000-0000-4000-8000-000000000003")
_INSTRUMENT_ID = UUID("92100000-0000-4000-8000-000000000004")
_SNAPSHOT_ID = UUID("92100000-0000-4000-8000-000000000005")
_DATA_HASH = "b" * 64
_SNAPSHOT_REF = f"sha256:{'a' * 64}"
# A cold checkout builds the ~4 GB torch runtime inside this call, not just starts it.
_COMPOSE_UP_TIMEOUT_SECONDS = 1_800
_SAFE_HOST_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "COMPOSE_PARALLEL_LIMIT",
        "COMPOSE_PROGRESS",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


def main() -> None:
    arguments = _arguments()
    root = arguments.root.resolve()
    configuration = load_kronos_worker_configuration(
        root / "config" / "workers" / "kronos_cpu.yaml"
    )
    with tempfile.TemporaryDirectory(prefix="stonks-kronos-verify-") as raw:
        temporary = Path(raw)
        runtime = prepare_ephemeral_openbb_runtime(temporary / "auth")
        project_name = f"stonks-kronos-verify-{int(time.time())}"
        environment = _compose_environment(
            runtime.environment,
            model_root=root / ".data" / "models" / "kronos",
            project_name=project_name,
            port=arguments.port,
        )
        try:
            _start_container(
                root,
                environment,
            )
            ready = _wait_until_ready(arguments.port, arguments.startup_timeout)
            forecast, elapsed = _forecast(
                configuration=configuration,
                credentials=runtime.credentials,
                artifact_root=temporary / "artifacts",
                port=arguments.port,
            )
            alpha, eligibility = _map_shadow_alpha(
                forecast,
                root=root,
            )
            print(
                json.dumps(
                    {
                        "success": True,
                        "status": 200,
                        "data": {
                            "ready": ready,
                            "worker_profile": configuration.policy.profile,
                            "worker_runtime_hash": configuration.runtime.runtime_hash,
                            "model_id": forecast.forecast.model_id,
                            "model_revision": forecast.forecast.model_revision,
                            "path_count": forecast.forecast.path_count,
                            "horizon_bars": forecast.forecast.horizon_bars,
                            "expected_return": str(forecast.forecast.expected_return),
                            "direction_probability": str(
                                forecast.forecast.direction_probability
                            ),
                            "forecast_id": str(forecast.forecast.forecast_id),
                            "alpha_signal_id": str(alpha.signal_id),
                            "alpha_value": str(alpha.value),
                            "deployment_state": "shadow",
                            "paper_eligible": eligibility.eligible,
                            "paper_weight": str(eligibility.weight),
                            "elapsed_seconds": round(elapsed, 3),
                        },
                        "error": None,
                        "metadata": {
                            "execution_mode": "paper",
                            "actual_model_inference": True,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        finally:
            _cleanup(root, environment)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--port", type=int, default=17_200)
    parser.add_argument("--startup-timeout", type=float, default=600)
    parsed = parser.parse_args()
    if not 1_024 <= parsed.port <= 65_535:
        parser.error("port must be between 1024 and 65535")
    if not 1 <= parsed.startup_timeout <= 1_800:
        parser.error("startup timeout is outside the supported range")
    return parsed


def _compose_environment(
    runtime: Mapping[str, str],
    *,
    model_root: Path,
    project_name: str,
    port: int,
) -> dict[str, str]:
    values = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _SAFE_HOST_ENVIRONMENT
    }
    values.update(runtime)
    values.update(
        {
            "COMPOSE_PROJECT_NAME": project_name,
            "STONKS_KRONOS_CPU_PORT": str(port),
            "STONKS_KRONOS_MODEL_ROOT": model_root.resolve().as_posix(),
            "STONKS_KRONOS_SERVICE_OIDC_AUDIENCE": (
                f"stonks-gui-{ServiceReceiver.KRONOS.value}"
            ),
        }
    )
    return values


def _start_container(
    root: Path,
    environment: dict[str, str],
) -> None:
    command = _compose_up_command(root)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=_COMPOSE_UP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Kronos container failed to start: compose up exceeded "
            f"{_COMPOSE_UP_TIMEOUT_SECONDS}s"
        ) from error
    if result.returncode != 0:
        reason = " ".join(result.stderr.split())[:1_000]
        raise RuntimeError(f"Kronos container failed to start: {reason}")
    if not _attach_loopback_bridge(root, environment):
        raise RuntimeError("Kronos verification bridge failed to attach")


def _compose_up_command(root: Path) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "-f",
        str(root / "infra" / "compose.kronos.yaml"),
        "up",
        "--build",
        "--detach",
        "--no-deps",
        "kronos-cpu",
    )


def _attach_loopback_bridge(
    root: Path,
    environment: dict[str, str],
) -> bool:
    network = _verification_network_name(environment)
    created = subprocess.run(
        _network_create_command(network),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if created.returncode != 0:
        return False
    identified = subprocess.run(
        _compose_ps_command(root),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    container_id = identified.stdout.strip()
    if identified.returncode != 0 or not container_id:
        return False
    attached = subprocess.run(
        ("docker", "network", "connect", network, container_id),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return attached.returncode == 0


def _compose_ps_command(root: Path) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "-f",
        str(root / "infra" / "compose.kronos.yaml"),
        "ps",
        "--quiet",
        "kronos-cpu",
    )


def _network_create_command(network: str) -> tuple[str, ...]:
    return (
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--opt",
        "com.docker.network.bridge.enable_ip_masquerade=false",
        network,
    )


def _verification_network_name(environment: Mapping[str, str]) -> str:
    project = environment.get("COMPOSE_PROJECT_NAME", "")
    if (
        not project
        or len(project) > 96
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in project
        )
    ):
        raise ValueError("COMPOSE_PROJECT_NAME is invalid")
    return f"{project}-loopback"


def _wait_until_ready(port: int, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    client = httpx.Client(trust_env=False, timeout=2)
    try:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"http://127.0.0.1:{port}/readyz")
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("success") is True and payload.get("data") == {
                        "ready": True
                    }:
                        return cast(dict[str, object], payload["data"])
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
    finally:
        client.close()
    raise RuntimeError("Kronos readiness deadline exceeded")


def _forecast(
    *,
    configuration: KronosWorkerConfiguration,
    credentials: ServiceCredentialProvider,
    artifact_root: Path,
    port: int,
) -> tuple[ForecastOutputArtifact, float]:
    now = datetime.now(UTC)
    as_of = now - timedelta(minutes=1)
    bars = _bars(as_of)
    future = (_next_business_close(as_of),)
    request = ForecastRequest(
        request_id=_REQUEST_ID,
        run_id=_RUN_ID,
        instrument_id=_INSTRUMENT_ID,
        dataset_snapshot_id=_SNAPSHOT_ID,
        snapshot_artifact_ref=_SNAPSHOT_REF,
        data_hash=_DATA_HASH,
        as_of=as_of,
        interval="1d",
        horizon_bars=1,
        input_window_start=bars[0].event_time,
        input_window_end=bars[-1].event_time,
        model_id=configuration.runtime.model_id,
        model_revision=configuration.runtime.model_revision,
        model_artifact_hash=configuration.runtime.model_artifact_hash,
        tokenizer_id=configuration.runtime.tokenizer_id,
        tokenizer_revision=configuration.runtime.tokenizer_revision,
        tokenizer_artifact_hash=configuration.runtime.tokenizer_artifact_hash,
        runtime_hash=configuration.runtime.runtime_hash,
        requested_at=now,
        deadline_at=now + timedelta(minutes=5),
    )
    worker_request = KronosWorkerRequest(
        request_id=request.request_id,
        run_id=request.run_id,
        job_id=_JOB_ID,
        attempt_generation=1,
        attempt_nonce="kronos-actual-verification",
        profile="cpu",
        instrument_id=request.instrument_id,
        mic="XNAS",
        dataset_snapshot_id=request.dataset_snapshot_id,
        snapshot_artifact_ref=request.snapshot_artifact_ref,
        data_hash=request.data_hash,
        as_of=request.as_of,
        interval="1d",
        bars=bars,
        future_timestamps=future,
        runtime=configuration.runtime,
        sampling=KronosSamplingPolicy(
            seed_policy="explicit-sequential-v1",
            seeds=(17, 18, 19),
            temperature=Decimal("1"),
            top_k=0,
            top_p=Decimal("0.9"),
        ),
        deadline=request.deadline_at,
    )
    policy = KronosHttpPolicy.model_validate(
        configuration.policy.model_dump()
        | {
            "origin": f"http://127.0.0.1:{port}",
            "max_transient_retries": 0,
        }
    )
    client = httpx.Client(trust_env=False, follow_redirects=False)
    adapter = KronosHttpAdapter(
        client=client,
        artifacts=LocalArtifactStore(artifact_root),
        policy=policy,
        credentials=credentials,
        clock=lambda: datetime.now(UTC),
    )
    started = time.monotonic()
    try:
        result = adapter.forecast(worker_request, request)
    finally:
        client.close()
    if isinstance(result, Failure):
        raise RuntimeError(f"Kronos forecast failed closed: {result.error.code.value}")
    return result.value, time.monotonic() - started


def _bars(as_of: datetime) -> tuple[KronosBar, ...]:
    sessions: list[date] = []
    candidate = as_of.date() - timedelta(days=1)
    while len(sessions) < 64:
        if candidate.weekday() < 5:
            sessions.append(candidate)
        candidate -= timedelta(days=1)
    sessions.reverse()
    values = []
    for index, session in enumerate(sessions):
        close = Decimal("100") + Decimal(index) / Decimal("20")
        event_time = datetime.combine(session, wall_time(21), UTC)
        values.append(
            KronosBar(
                event_time=event_time,
                available_at=event_time,
                open=close - Decimal("0.2"),
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
                volume=Decimal("1000000") + index,
                amount=close * (Decimal("1000000") + index),
                volume_quality=VolumeQuality.OBSERVED,
            )
        )
    return tuple(values)


def _next_business_close(after: datetime) -> datetime:
    candidate = after.date()
    while True:
        close = datetime.combine(candidate, wall_time(21), UTC)
        if candidate.weekday() < 5 and close > after:
            return close
        candidate += timedelta(days=1)


def _alpha_generated_at(created_at: datetime, now: datetime) -> datetime:
    """The worker stamps the artifact on its own clock, milliseconds ahead of ours.

    An alpha derived from an artifact is never generated before it, so the mapping
    timestamp is the later of the two rather than this process's wall clock.
    """
    return max(now, created_at)


def _map_shadow_alpha(
    forecast: ForecastOutputArtifact,
    *,
    root: Path,
) -> tuple[AlphaSignal, SignalEligibilityDecision]:
    configuration = load_kronos_strategy_configuration(
        root / "config" / "strategies" / "kronos.yaml"
    )
    generated_at = _alpha_generated_at(forecast.created_at, datetime.now(UTC))
    checks = tuple(
        EvaluationCheck(
            kind=kind,
            status=EvaluationCheckStatus.PASSED,
        )
        for kind in EvaluationCheckKind
    )
    evaluation = EvaluationReport(
        report_id=UUID("92100000-0000-4000-8000-000000000006"),
        strategy_id=configuration.manifest.strategy_id,
        strategy_version=configuration.manifest.strategy_version,
        strategy_manifest_hash=configuration.manifest.manifest_hash,
        dataset_snapshot_id=UUID("92100000-0000-4000-8000-000000000007"),
        data_hash="c" * 64,
        runtime_hash=configuration.manifest.runtime_hash,
        evaluation_policy_hash=configuration.evaluation_policy_hash,
        as_of=generated_at - timedelta(days=10),
        window_start=generated_at - timedelta(days=100),
        window_end=generated_at - timedelta(days=11),
        checks=checks,
        metrics=(
            EvaluationMetric(
                name="net_alpha",
                value=Decimal("0.001"),
                unit="return",
            ),
        ),
        calibration=ConfidenceCalibration.CALIBRATED,
        baseline_ids=configuration.required_baselines,
        report_artifact_ref=f"sha256:{'d' * 64}",
        valid_until=generated_at + timedelta(days=1),
        created_at=generated_at - timedelta(days=9),
        passed=True,
    )
    registry = StrategyRegistryEntry(
        manifest=configuration.manifest,
        state=configuration.deployment_state,
        evaluation_report_id=evaluation.report_id,
        evaluation_hash=evaluation.evaluation_hash,
        version=1,
        created_at=generated_at - timedelta(days=30),
        updated_at=generated_at - timedelta(days=9),
    )
    mapped = map_kronos_to_alpha(
        KronosToAlphaCommand(
            forecast_output=forecast,
            registry=registry,
            evaluation=evaluation,
            generated_at=generated_at,
        ),
        configuration,
    )
    if isinstance(mapped, Failure):
        raise RuntimeError(
            f"Kronos alpha mapping failed closed: {mapped.error.code.value}"
        )
    eligibility = evaluate_signal_eligibility(
        mapped.value,
        registry=registry,
        evaluation=evaluation,
        at=generated_at,
    )
    return mapped.value, eligibility


def _cleanup(
    root: Path,
    environment: dict[str, str],
) -> None:
    subprocess.run(
        (
            "docker",
            "compose",
            "-f",
            str(root / "infra" / "compose.kronos.yaml"),
            "down",
            "--remove-orphans",
        ),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ("docker", "network", "rm", _verification_network_name(environment)),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


if __name__ == "__main__":
    main()
