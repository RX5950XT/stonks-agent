"""Executable composition for snapshot and research worker jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid5

from stonks_agent.adapters.auth.service_credentials import (
    load_rs256_service_credential_provider,
)
from stonks_agent.adapters.market_data.openbb_rest import OpenBBRestAdapter
from stonks_agent.adapters.market_data.openbb_snapshot import (
    OpenBBSnapshotMaterializationSource,
)
from stonks_agent.adapters.postgres.job_queue import PostgresJobQueue
from stonks_agent.adapters.postgres.late_result_audit import PostgresLateResultAudit
from stonks_agent.adapters.postgres.research_completion import (
    PostgresResearchLeasePreflight,
)
from stonks_agent.adapters.postgres.snapshot_completion import (
    PostgresSnapshotCompletionStore,
)
from stonks_agent.adapters.reporting.jinja import JinjaReportRenderer
from stonks_agent.application.data.process_snapshot_lease import (
    process_snapshot_lease,
)
from stonks_agent.application.operational_budget import OperationalBudgetEvaluator
from stonks_agent.application.research.complete_research_result import (
    complete_research_result,
)
from stonks_agent.application.research.process_research_lease import (
    process_research_lease,
)
from stonks_agent.composition.budget import (
    MonotonicBudgetUsage,
    UsageTrackingLLM,
)
from stonks_agent.composition.kronos import build_research_kronos_forecaster
from stonks_agent.composition.models import (
    LLMCompositionConfig,
    build_llm,
    build_model_http_client,
)
from stonks_agent.composition.runtime import LocalRuntime
from stonks_agent.config.budgets import load_budget_catalog
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
)
from stonks_agent.domain.job import FailJob, JobLease
from stonks_agent.domain.provider_policy import (
    ProviderPolicy,
    load_provider_policies,
)
from stonks_agent.domain.research_job import ResearchLeaseInput
from stonks_agent.domain.signal import ForecastOutputArtifact
from stonks_agent.ports.research_forecast import ResearchForecastPort
from stonks_agent.ports.service_credentials import (
    ServiceBearerCredential,
    ServiceCredentialProvider,
    ServiceCredentialRequest,
)

type WorkerJobHandler = Callable[[JobLease], Result[object]]
_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class WorkerComposition:
    runtime: LocalRuntime
    queue: PostgresJobQueue
    handlers: Mapping[str, WorkerJobHandler]

    def close(self) -> None:
        self.runtime.close()


def build_worker_composition(
    runtime: LocalRuntime,
    *,
    environment: Mapping[str, str],
    root: Path = _ROOT,
    credentials: ServiceCredentialProvider | None = None,
    kronos_origin: str | None = None,
    model_environment: Callable[[], Mapping[str, str]] | None = None,
) -> WorkerComposition:
    project_root = root.resolve(strict=True)
    queue = PostgresJobQueue(runtime.engine, recorder=runtime.telemetry)
    late_results = PostgresLateResultAudit(runtime.engine)
    policy = _provider_policy(project_root)
    provider = credentials or _service_credentials(environment)
    static_model_environment = dict(environment)
    model_environment_source = model_environment or (
        lambda: dict(static_model_environment)
    )
    forecast = _research_forecast(
        runtime,
        root=project_root,
        credentials=provider,
        origin=kronos_origin or environment.get("STONKS_KRONOS_ORIGIN"),
    )

    def snapshot(lease: JobLease) -> Result[object]:
        return process_snapshot_lease(
            lease,
            now=utc_now(),
            source=OpenBBSnapshotMaterializationSource(
                OpenBBRestAdapter(
                    client=runtime.http_client,
                    credentials=provider,
                    clock=utc_now,
                )
            ),
            artifacts=runtime.artifacts,
            completions=PostgresSnapshotCompletionStore(runtime.engine),
            policy=policy,
        )

    def research(lease: JobLease) -> Result[object]:
        try:
            selected_environment = dict(model_environment_source())
            config = LLMCompositionConfig.from_environment(
                selected_environment,
                runtime_environment=selected_environment.get(
                    "STONKS_ENVIRONMENT",
                    "production",
                ),
            )
            tracker = MonotonicBudgetUsage()
            with build_model_http_client(config) as model_client:
                llm = UsageTrackingLLM(
                    build_llm(
                        config,
                        environment=selected_environment,
                        artifacts=runtime.artifacts,
                        client=model_client,
                    ),
                    tracker,
                )
                product = process_research_lease(
                    lease,
                    preflight=PostgresResearchLeasePreflight(runtime.engine),
                    llm=llm,
                    artifacts=runtime.artifacts,
                    renderer=JinjaReportRenderer(
                        template_directory=project_root / "templates",
                        artifacts=runtime.artifacts,
                        clock=utc_now,
                    ),
                    budget=OperationalBudgetEvaluator(
                        catalog=load_budget_catalog(
                            project_root / "config" / "budgets.yaml"
                        ),
                        usage=tracker,
                    ),
                    forecast=forecast,
                    clock=utc_now,
                )
        except (OSError, TypeError, ValueError):
            return _fail_research(
                queue,
                lease,
                ErrorCode.CONFIGURATION_INVALID,
                "research_configuration_invalid",
            )
        if isinstance(product, Failure):
            return _fail_research(
                queue,
                lease,
                product.error.code,
                "research_handler_failed",
            )
        return complete_research_result(
            lease,
            request_id=uuid5(lease.run_id, "bounded-research-request"),
            manifest=product.value.manifest,
            now=utc_now(),
            queue=queue,
            late_results=late_results,
        )

    return WorkerComposition(
        runtime=runtime,
        queue=queue,
        handlers={
            "create_snapshot": snapshot,
            "research_pipeline": research,
        },
    )


def _provider_policy(root: Path) -> ProviderPolicy:
    policies = load_provider_policies(root / "config" / "providers" / "default.yaml")
    return next(
        policy
        for policy in policies
        if policy.market == "US"
        and policy.capability == "prices"
        and policy.policy_id == "us-prices/1"
    )


def _service_credentials(
    environment: Mapping[str, str],
) -> ServiceCredentialProvider:
    try:
        return load_rs256_service_credential_provider(environment)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _UnavailableServiceCredentials()


def _research_forecast(
    runtime: LocalRuntime,
    *,
    root: Path,
    credentials: ServiceCredentialProvider,
    origin: str | None,
) -> ResearchForecastPort:
    if origin is None:
        return _UnavailableResearchForecast()
    return build_research_kronos_forecaster(
        runtime,
        root=root,
        credentials=credentials,
        origin=origin,
        clock=utc_now,
    )


class _UnavailableServiceCredentials:
    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Result[ServiceBearerCredential]:
        del request
        return Failure(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Service credential is unavailable",
            )
        )


class _UnavailableResearchForecast:
    def forecast(
        self,
        lease: JobLease,
        value: ResearchLeaseInput,
    ) -> Result[ForecastOutputArtifact]:
        del lease, value
        return Failure(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Kronos research forecast is not composed",
            )
        )


def _fail_research(
    queue: PostgresJobQueue,
    lease: JobLease,
    code: ErrorCode,
    reason: str,
) -> Result[object]:
    return queue.fail(
        FailJob(
            job_id=lease.job_id,
            worker_id=lease.lease_owner,
            attempt_generation=lease.attempt_generation,
            attempt_nonce=lease.attempt_nonce,
            error_code=code,
            reason_code=reason,
        ),
        now=utc_now(),
    )
