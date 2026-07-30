"""Local research composition for the authenticated Kronos CPU worker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from stonks_agent.adapters.forecast.kronos import (
    KronosHttpAdapter,
    KronosHttpPolicy,
    load_kronos_worker_configuration,
)
from stonks_agent.adapters.forecast.research_kronos import (
    ResearchKronosForecaster,
)
from stonks_agent.application.signals.kronos_to_alpha import (
    load_kronos_strategy_configuration,
)
from stonks_agent.composition.runtime import LocalRuntime
from stonks_agent.composition.us_market import (
    XNAS_2026_VALID_FROM,
    XNAS_2026_VALID_THROUGH,
    xnas_2026_calendar,
)
from stonks_agent.ports.service_credentials import ServiceCredentialProvider
from stonks_contracts.kronos import KronosSamplingPolicy


def build_research_kronos_forecaster(
    runtime: LocalRuntime,
    *,
    root: Path,
    credentials: ServiceCredentialProvider,
    origin: str,
    clock: Callable[[], datetime],
) -> ResearchKronosForecaster:
    worker = load_kronos_worker_configuration(
        root / "config" / "workers" / "kronos_cpu.yaml"
    )
    strategy = load_kronos_strategy_configuration(
        root / "config" / "strategies" / "kronos.yaml"
    )
    policy = KronosHttpPolicy.model_validate(
        worker.policy.model_dump() | {"origin": origin}
    )
    return ResearchKronosForecaster(
        adapter=KronosHttpAdapter(
            client=runtime.http_client,
            artifacts=runtime.artifacts,
            policy=policy,
            credentials=credentials,
            clock=clock,
        ),
        configuration=worker,
        calendar=xnas_2026_calendar(),
        sampling=KronosSamplingPolicy(
            seed_policy="explicit-sequential-v1",
            seeds=(17, 18, 19),
            temperature=Decimal(1),
            top_k=0,
            top_p=Decimal("0.9"),
        ),
        horizon_bars=strategy.feature_spec.horizon_bars,
        clock=clock,
        calendar_valid_from=XNAS_2026_VALID_FROM,
        calendar_valid_through=XNAS_2026_VALID_THROUGH,
    )
