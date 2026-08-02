from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.reporting.jinja import JinjaReportRenderer
from stonks_agent.application.research.process_research_lease import (
    process_research_lease,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.job import JobLease
from stonks_agent.domain.operational_budget import (
    BudgetScope,
    BudgetStatus,
    BudgetThreshold,
    BudgetUsage,
    OperationalBudgetPolicy,
    evaluate_budget,
)
from stonks_agent.domain.research_job import (
    ResearchLeaseInput,
    SnapshotForecastContext,
)
from stonks_agent.domain.research_run import ResearchRunRequest
from stonks_agent.domain.signal import ForecastOutputArtifact
from stonks_contracts.market_data import DataQuality, DataQualityStatus
from stonks_contracts.signal import ForecastSignal

from .helpers import EVIDENCE_ID, NOW, ScriptedLLM, evidence
from .test_tool_loop import final_turn

RUN_ID = UUID("00000000-0000-4000-8000-000000000011")


class Preflight:
    def __init__(self, value: ResearchLeaseInput | Failure) -> None:
        self.value = value

    def preflight(
        self,
        lease: JobLease,
        *,
        now: object,
    ) -> Success[ResearchLeaseInput] | Failure:
        del lease, now
        return self.value if isinstance(self.value, Failure) else Success(self.value)


class Budget:
    def evaluate(
        self,
        scope: BudgetScope,
        *,
        previous_status: BudgetStatus = BudgetStatus.WITHIN,
    ) -> object:
        return evaluate_budget(
            OperationalBudgetPolicy(
                scope=scope,
                degraded=BudgetThreshold(
                    action=BudgetStatus.DEGRADED,
                    max_cost_usd=Decimal("2"),
                    max_elapsed_seconds=Decimal("30"),
                ),
                failed=BudgetThreshold(
                    action=BudgetStatus.FAILED,
                    max_cost_usd=Decimal("5"),
                    max_elapsed_seconds=Decimal("60"),
                ),
            ),
            BudgetUsage(
                cost_usd=Decimal("0"),
                monotonic_started_seconds=Decimal("1"),
                monotonic_observed_seconds=Decimal("1"),
            ),
            previous_status=previous_status,
        )


class Forecast:
    def __init__(
        self,
        result: Success[ForecastOutputArtifact] | Failure,
    ) -> None:
        self.result = result
        self.calls: list[tuple[JobLease, ResearchLeaseInput]] = []

    def forecast(
        self,
        lease: JobLease,
        value: ResearchLeaseInput,
    ) -> Success[ForecastOutputArtifact] | Failure:
        self.calls.append((lease, value))
        return self.result


def snapshot_context() -> SnapshotForecastContext:
    return SnapshotForecastContext(
        snapshot_id=run_request().snapshot_id,
        manifest_artifact_hash="a" * 64,
        content_hash="a" * 64,
        provider="fixture",
        endpoint="/fixture",
    )


def forecast_output() -> ForecastOutputArtifact:
    return ForecastOutputArtifact(
        request_id=UUID("00000000-0000-4000-8000-000000000014"),
        forecast=ForecastSignal(
            forecast_id=UUID("00000000-0000-4000-8000-000000000015"),
            instrument_id=UUID("00000000-0000-4000-8000-000000000016"),
            as_of=NOW,
            interval="1d",
            horizon_bars=1,
            expected_return=Decimal("0.01"),
            median_return=Decimal("0.009"),
            direction_probability=Decimal("0.67"),
            expected_volatility=Decimal("0.02"),
            downside_quantile=Decimal("-0.03"),
            max_drawdown_quantile=Decimal("-0.04"),
            path_count=3,
            dispersion=Decimal("0.01"),
            input_quality=DataQuality(
                status=DataQualityStatus.AVAILABLE,
                completeness=Decimal(1),
            ),
            model_id="NeoQuasar/Kronos-small",
            model_revision="9" * 40,
            tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
            tokenizer_revision="8" * 40,
            device="cpu",
            seed_policy="explicit-sequential-v1",
            inference_code_version="kronos-path-retention/1.0.0",
            dataset_snapshot_id=run_request().snapshot_id,
            input_window_start=NOW - timedelta(days=2),
            input_window_end=NOW - timedelta(days=1),
            generated_at=NOW,
            latency_ms=10,
        ),
        raw_output_artifact_ref=f"sha256:{'b' * 64}",
        sampled_paths_artifact_ref=f"sha256:{'c' * 64}",
        model_artifact_hash="d" * 64,
        tokenizer_artifact_hash="e" * 64,
        runtime_hash="f" * 64,
        data_hash="a" * 64,
        stochastic=True,
        created_at=NOW,
    )


def lease() -> JobLease:
    return JobLease(
        job_id=UUID("00000000-0000-4000-8000-000000000012"),
        run_id=RUN_ID,
        job_type="research_pipeline",
        payload=run_request().model_dump(mode="json", exclude={"requested_at"}),
        attempt_generation=1,
        attempt_nonce="nonce",
        lease_owner="worker-1",
        lease_until=NOW + timedelta(minutes=5),
        attempts=1,
        deadline_at=NOW + timedelta(minutes=5),
    )


def run_request() -> ResearchRunRequest:
    return ResearchRunRequest(
        instrument_id="instrument:aapl",
        symbol="AAPL",
        as_of=NOW,
        snapshot_id=UUID("00000000-0000-4000-8000-000000000013"),
        research_profile_id="balanced-v1",
        model_policy_id="research-models-v1",
        language="zh-TW",
        idempotency_key="test-research",
        owner_subject="research-owner",
        requested_at=NOW,
    )


def report_turn() -> dict[str, object]:
    return {
        "outlook": "bullish_outlook",
        "score": "0.7",
        "confidence": "0.6",
        "claims": [
            {
                "assertion": "The cited market evidence supports the outlook.",
                "certainty": "observed",
                "data_quality": "available",
                "evidence_refs": [str(EVIDENCE_ID)],
            }
        ],
        "risks": ["The evidence window is narrow."],
        "catalysts": [],
        "scenarios": [],
        "signal_attribution": ["bounded-research-orchestrator"],
        "data_limitations": [],
    }


def evidence_tool_turn() -> dict[str, object]:
    return {
        "action": "tools",
        "tool_calls": [
            {
                "tool_name": "read_evidence",
                "arguments": {"evidence_id": str(EVIDENCE_ID)},
                "instrument_ids": ["instrument:aapl"],
                "evidence_ids": [str(EVIDENCE_ID)],
                "timeout_ms": 2_000,
                "output_limit_bytes": 16_384,
            }
        ],
        "claims": [],
        "confidence": None,
        "counterarguments": [],
        "risks": [],
        "warnings": [],
    }


def test_research_lease_runs_tool_capable_agent_and_degraded_report_pipeline() -> None:
    artifacts = MemoryArtifactStore()
    item = evidence(
        subject="instrument:aapl",
        kind="market_data",
        sensitivity="internal",
        license_tag="provider-terms",
        redistribution_tag="internal-use-only",
    )
    preflight = Preflight(
        ResearchLeaseInput(
            request=run_request(),
            snapshot=snapshot_context(),
            evidence=(item,),
        )
    )
    forecast = Forecast(
        Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Kronos unavailable",
            )
        )
    )

    result = process_research_lease(
        lease(),
        preflight=preflight,
        llm=ScriptedLLM([evidence_tool_turn(), final_turn(), report_turn()]),
        artifacts=artifacts,
        renderer=JinjaReportRenderer(
            template_directory=Path("templates"),
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        budget=Budget(),  # type: ignore[arg-type]
        forecast=forecast,
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.result.status.value == "degraded"
    assert result.value.result.research_artifact.allowed_evidence_ids == frozenset(
        {EVIDENCE_ID}
    )
    assert result.value.result.pipeline.report is not None
    assert result.value.result.pipeline.report.evidence_refs == (EVIDENCE_ID,)
    assert result.value.result.pipeline.issues[0].stage.value == "tradingagents"
    assert result.value.result.kronos is not None
    assert result.value.result.kronos.status == "failed"
    assert len(forecast.calls) == 1
    assert artifacts.is_finalized(result.value.manifest.content_hash)
    assert result.value.manifest.metadata.source == "stonks-agent-research-worker"


def test_preflight_failure_never_invokes_model_or_creates_artifact() -> None:
    artifacts = MemoryArtifactStore()
    llm = ScriptedLLM([final_turn()])
    failure = Failure(StructuredError(code=ErrorCode.CONFLICT, message="stale lease"))
    forecast = Forecast(
        Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Kronos unavailable",
            )
        )
    )

    result = process_research_lease(
        lease(),
        preflight=Preflight(failure),
        llm=llm,
        artifacts=artifacts,
        renderer=JinjaReportRenderer(
            template_directory=Path("templates"),
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        budget=Budget(),  # type: ignore[arg-type]
        forecast=forecast,
        clock=lambda: NOW,
    )

    assert result is failure
    assert llm.requests == []
    assert forecast.calls == []


def test_research_lease_persists_actual_snapshot_bound_forecast() -> None:
    artifacts = MemoryArtifactStore()
    item = evidence(
        subject="instrument:aapl",
        kind="market_data",
        sensitivity="internal",
        license_tag="provider-terms",
        redistribution_tag="internal-use-only",
    )
    preflight = Preflight(
        ResearchLeaseInput(
            request=run_request(),
            snapshot=snapshot_context(),
            evidence=(item,),
        )
    )

    result = process_research_lease(
        lease(),
        preflight=preflight,
        llm=ScriptedLLM([evidence_tool_turn(), final_turn(), report_turn()]),
        artifacts=artifacts,
        renderer=JinjaReportRenderer(
            template_directory=Path("templates"),
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        budget=Budget(),  # type: ignore[arg-type]
        forecast=Forecast(Success(forecast_output())),
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.result.schema_version == "1.1.0"
    assert result.value.result.kronos is not None
    assert result.value.result.kronos.status == "succeeded"
    assert result.value.result.kronos.actual_model_inference is True
    assert result.value.result.kronos.alpha_signal is None
    assert result.value.result.kronos.eligibility.weight == 0
    assert (
        dict(result.value.manifest.metadata.attributes)["schema"]
        == "research-worker-result/1.1.0"
    )
