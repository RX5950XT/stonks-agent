from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Timer
from time import monotonic

import httpx
import pytest
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from contracts.workers import test_kronos as kronos_fixtures  # noqa: E402
from contracts.workers import test_qlib as quant_fixtures  # noqa: E402
from contracts.workers import test_tradingagents as ta_fixtures  # noqa: E402
from fixtures.service_auth import (  # noqa: E402
    ExactServiceAuthenticator,
    authorization_headers,
)

from stonks_agent.adapters.forecast.kronos import (  # noqa: E402
    KronosWorkerConfiguration,
)
from stonks_agent.adapters.research.tradingagents_http import (  # noqa: E402
    TradingAgentsWorkerPolicy,
)
from stonks_agent.config.capacity import load_capacity_policy  # noqa: E402
from stonks_agent.domain.capacity import ProcessBudget, ProcessBudgetId  # noqa: E402
from stonks_service_auth import ServiceReceiver  # noqa: E402
from workers.kronos.adapter import KronosWorker  # noqa: E402
from workers.kronos.app import create_app as create_kronos_app  # noqa: E402
from workers.kronos.model_loader import WarmOnceModelLoader  # noqa: E402
from workers.quant_lab.app import create_app as create_quant_app  # noqa: E402
from workers.tradingagents.adapter import TradingAgentsWorker  # noqa: E402
from workers.tradingagents.app import create_app as create_ta_app  # noqa: E402


class BlockingTradingRuntime(ta_fixtures.RecordingRuntime):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def run(self, request: object):  # type: ignore[no-untyped-def, override]
        self._started.set()
        self._release.wait(timeout=2)
        return super().run(request)  # type: ignore[arg-type]


class BlockingKronosRuntime(kronos_fixtures.FakePathRuntime):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def predict_path(self, request, *, seed):  # type: ignore[no-untyped-def, override]
        if not self.seeds:
            self._started.set()
            self._release.wait(timeout=2)
        return super().predict_path(request, seed=seed)


class BlockingQuantRuntime(quant_fixtures._FakeRuntime):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def fit_predict(self, job):  # type: ignore[no-untyped-def, override]
        self._started.set()
        self._release.wait(timeout=2)
        return super().fit_predict(job)


async def _assert_non_blocking_saturation(
    *, app: object, path: str, content: str, started: Event, release: Event
) -> None:
    timer = Timer(1, release.set)
    timer.start()
    began = monotonic()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://worker.test"
        ) as client:
            first = asyncio.create_task(
                client.post(
                    path,
                    content=content,
                    headers={
                        **authorization_headers(),
                        "content-type": "application/json",
                    },
                )
            )
            assert await asyncio.to_thread(started.wait, 0.5)
            event_loop_delay = monotonic() - began
            busy = await client.post(
                path,
                content=content,
                headers={
                    **authorization_headers(),
                    "content-type": "application/json",
                },
            )
            release.set()
            accepted = await first
    finally:
        release.set()
        timer.cancel()
    assert event_loop_delay < 0.3
    assert accepted.status_code == 200
    assert busy.status_code == 429
    assert busy.json() == {
        "success": False,
        "status": 429,
        "data": None,
        "error": {"code": "worker_busy", "message": "Worker is at capacity"},
        "metadata": None,
    }


@pytest.mark.asyncio
async def test_tradingagents_offloads_and_rejects_saturation() -> None:
    started, release = Event(), Event()
    request = ta_fixtures.analysis_request()
    worker = TradingAgentsWorker(
        policy=ta_fixtures.policy(),
        runtime=BlockingTradingRuntime(started, release),
        artifacts=ta_fixtures.FakeArtifacts(),
        clock=lambda: ta_fixtures.NOW,
    )
    app = create_ta_app(
        worker=worker,
        authenticator=ExactServiceAuthenticator.for_request(
            request, receiver=ServiceReceiver.TRADINGAGENTS
        ),
        max_concurrency=1,
    )

    await _assert_non_blocking_saturation(
        app=app,
        path="/v1/analyze",
        content=request.model_dump_json(),
        started=started,
        release=release,
    )


@pytest.mark.asyncio
async def test_kronos_offloads_and_rejects_saturation(tmp_path: Path) -> None:
    started, release = Event(), Event()
    root, manifest = kronos_fixtures._model_root(tmp_path)
    runtime = BlockingKronosRuntime(started, release)
    loader = WarmOnceModelLoader(
        root=root,
        manifest=manifest,
        profile="cpu",
        factory=lambda _paths, _profile: runtime,
    )
    policy = kronos_fixtures._policy(manifest)
    worker = KronosWorker(
        policy=policy,
        loader=loader,
        clock=lambda: datetime(2026, 1, 9, 21, 1, tzinfo=UTC),
    )
    loader.warm()
    request = kronos_fixtures._forecast_request(policy)
    app = create_kronos_app(
        worker=worker,
        authenticator=ExactServiceAuthenticator.for_request(
            request, receiver=ServiceReceiver.KRONOS
        ),
        max_concurrency=1,
    )

    await _assert_non_blocking_saturation(
        app=app,
        path="/v1/forecast",
        content=request.model_dump_json(),
        started=started,
        release=release,
    )


@pytest.mark.asyncio
async def test_quant_lab_offloads_and_rejects_saturation() -> None:
    started, release = Event(), Event()
    request = quant_fixtures._job()
    worker = quant_fixtures._worker(BlockingQuantRuntime(started, release))
    app = create_quant_app(
        worker=worker,
        authenticator=ExactServiceAuthenticator.for_request(
            request, receiver=ServiceReceiver.QUANT_LAB
        ),
        max_concurrency=1,
    )

    await _assert_non_blocking_saturation(
        app=app,
        path="/v1/research",
        content=request.model_dump_json(),
        started=started,
        release=release,
    )


@pytest.mark.parametrize("invalid", (0, 2, 17))
def test_heavy_worker_concurrency_config_fails_closed(invalid: int) -> None:
    ta_request = ta_fixtures.analysis_request()
    ta_worker = TradingAgentsWorker(
        policy=ta_fixtures.policy(),
        runtime=ta_fixtures.RecordingRuntime(),
        artifacts=ta_fixtures.FakeArtifacts(),
        clock=lambda: ta_fixtures.NOW,
    )
    ta_auth = ExactServiceAuthenticator.for_request(
        ta_request, receiver=ServiceReceiver.TRADINGAGENTS
    )
    quant_request = quant_fixtures._job()
    quant_auth = ExactServiceAuthenticator.for_request(
        quant_request, receiver=ServiceReceiver.QUANT_LAB
    )

    with pytest.raises(ValueError, match="max_concurrency"):
        create_ta_app(
            worker=ta_worker,
            authenticator=ta_auth,
            max_concurrency=invalid,
        )
    with pytest.raises(ValueError, match="max_concurrency"):
        create_quant_app(
            worker=quant_fixtures._worker(),
            authenticator=quant_auth,
            max_concurrency=invalid,
        )
    with pytest.raises(ValueError, match="max_concurrency"):
        create_kronos_app(
            worker=object(),  # type: ignore[arg-type]
            authenticator=ta_auth,
            max_concurrency=invalid,
        )


def test_worker_configs_and_compose_enforce_bounded_resources() -> None:
    policy = load_capacity_policy(ROOT / "config" / "capacity.yaml")
    budgets = {item.process_id: item for item in policy.process_budgets}
    ta_config = _yaml("config/workers/tradingagents.yaml")
    ta_compose = _yaml("infra/compose.tradingagents.yaml")
    for service in ta_compose["services"].values():
        _assert_resources(
            service,
            budgets[ProcessBudgetId.TRADINGAGENTS],
            "STONKS_TRADINGAGENTS_MAX_CONCURRENCY",
        )
    assert (
        ta_config["max_concurrency"]
        == budgets[ProcessBudgetId.TRADINGAGENTS].in_flight_ceiling
    )

    kronos_compose = _yaml("infra/compose.kronos.yaml")
    for profile in ("cpu", "cuda"):
        budget_id = ProcessBudgetId(f"kronos_{profile}")
        configuration = _yaml(f"config/workers/kronos_{profile}.yaml")
        service = kronos_compose["services"][f"kronos-{profile}"]
        assert (
            configuration["policy"]["max_concurrency"]
            == budgets[budget_id].in_flight_ceiling
        )
        _assert_resources(service, budgets[budget_id], "STONKS_KRONOS_MAX_CONCURRENCY")
    assert budgets[ProcessBudgetId.KRONOS_CUDA].gpu_vram_enforced is False
    assert "vram" not in str(kronos_compose).lower()

    quant_config = _yaml("config/workers/quant_lab.yaml")
    quant_service = _yaml("infra/compose.quant-lab.yaml")["services"]["quant-lab"]
    quant_budget = budgets[ProcessBudgetId.QUANT_LAB]
    assert quant_config["max_concurrency"] == quant_budget.in_flight_ceiling
    _assert_resources(
        quant_service,
        quant_budget,
        "STONKS_QUANT_LAB_MAX_CONCURRENCY",
    )


@pytest.mark.parametrize("invalid", ("1", 0, 2, True))
def test_typed_worker_policy_rejects_malformed_concurrency(invalid: object) -> None:
    ta_payload = _yaml("config/workers/tradingagents.yaml")
    ta_payload["max_concurrency"] = invalid
    with pytest.raises(ValidationError):
        TradingAgentsWorkerPolicy.model_validate(ta_payload)

    kronos_payload = _yaml("config/workers/kronos_cpu.yaml")
    kronos_payload["policy"]["max_concurrency"] = invalid
    with pytest.raises(ValidationError):
        KronosWorkerConfiguration.model_validate(kronos_payload)


def _yaml(path: str) -> dict[str, object]:
    payload = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _assert_resources(
    service: object, budget: ProcessBudget, concurrency_environment: str
) -> None:
    assert isinstance(service, dict)
    assert service["cpus"] * 1000 == budget.cpu_millicores_ceiling
    assert service["mem_limit"] == f"{budget.ram_mebibytes_ceiling // 1024}g"
    assert service["pids_limit"] == budget.pid_ceiling
    assert service["environment"][concurrency_environment] == str(
        budget.in_flight_ceiling
    )
