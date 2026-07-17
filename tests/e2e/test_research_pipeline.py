from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from support.budgets import FixedBudgetEvaluator
from support.telemetry import RecordingOperationRecorder

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.delivery.file import FileDeliveryAdapter
from stonks_agent.adapters.reporting.jinja import JinjaReportRenderer
from stonks_agent.application.research.pipeline import run_research_pipeline
from stonks_agent.domain.analysis_context import (
    AnalysisContextRequest,
    EvidenceRequirement,
)
from stonks_agent.domain.delivery import (
    DeliveryChannel,
    DeliveryCommand,
    DeliveryRequest,
)
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.operational_budget import BudgetStatus
from stonks_agent.domain.research import (
    AgentOpinion,
    OpinionRating,
    ResearchArtifact,
    ResearchClaim,
    ResearchClaimKind,
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.research_pipeline import (
    PipelineStatus,
    ResearchPipelineCommand,
)
from stonks_agent.domain.telemetry import ComponentName, OperationName
from stonks_agent.domain.usage_budget import UsageConsumption
from stonks_contracts.common import ConfidenceCalibration
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQuality, DataQualityStatus

NOW = datetime(2026, 7, 13, 9, tzinfo=UTC)
RUN_ID = UUID("40000000-0000-4000-8000-000000000001")
CONTEXT_ID = UUID("40000000-0000-4000-8000-000000000002")
EVIDENCE_ID = UUID("40000000-0000-4000-8000-000000000003")
ARTIFACT_ID = UUID("40000000-0000-4000-8000-000000000004")
OPINION_ID = UUID("40000000-0000-4000-8000-000000000005")
REPORT_ID = UUID("40000000-0000-4000-8000-000000000006")
REPORT_REQUEST_ID = UUID("40000000-0000-4000-8000-000000000007")


class Repository:
    def __init__(self, failure: Failure | None = None) -> None:
        self.failure = failure

    def query_available(self, *, subject: str, as_of: datetime) -> object:
        assert (subject, as_of) == ("AAPL", NOW)
        return self.failure or Success((evidence(),))


class Deterministic:
    def __init__(self, failure: Failure | None = None) -> None:
        self.failure = failure

    def research(self, context: object) -> object:
        del context
        return self.failure or Success(research_artifact())


class TradingAgents:
    def __init__(self, failure: Failure | None = None) -> None:
        self.failure = failure

    def analyze(self, context: object) -> object:
        del context
        return self.failure or Success(opinion())


class LLM:
    def __init__(self, failure: Failure | None = None) -> None:
        self.failure = failure

    def complete(self, request: StructuredLLMRequest) -> object:
        if self.failure:
            return self.failure
        return Success(
            StructuredLLMResponse(
                request_id=request.request_id,
                model="fake-report-v1",
                parsed_output={
                    "outlook": "bullish_outlook",
                    "score": "0.7",
                    "confidence": "0.5",
                    "claims": [
                        {
                            "assertion": "The PIT close is 100.",
                            "certainty": "observed",
                            "data_quality": "available",
                            "evidence_refs": [str(EVIDENCE_ID)],
                        }
                    ],
                    "risks": ["Narrow fixture evidence."],
                    "catalysts": [],
                    "scenarios": [],
                    "signal_attribution": ["deterministic", "tradingagents"],
                    "data_limitations": [],
                },
                raw_output_artifact_ref=f"sha256:{'c' * 64}",
                usage=UsageConsumption(input_tokens=10, output_tokens=20, elapsed_ms=3),
                created_at=NOW,
            )
        )


def test_snapshot_to_dual_research_report_render_and_file_delivery(
    tmp_path: Path,
) -> None:
    artifacts = MemoryArtifactStore()
    telemetry = RecordingOperationRecorder()
    result = run_research_pipeline(
        command(),
        repository=Repository(),  # type: ignore[arg-type]
        deterministic=Deterministic(),  # type: ignore[arg-type]
        tradingagents=TradingAgents(),  # type: ignore[arg-type]
        llm=LLM(),  # type: ignore[arg-type]
        renderer=JinjaReportRenderer(
            template_directory=Path("templates"),
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        artifacts=artifacts,
        budget=FixedBudgetEvaluator(),
        clock=lambda: NOW,
        telemetry=telemetry,
    )

    assert isinstance(result, Success)
    assert result.value.status is PipelineStatus.SUCCEEDED
    assert result.value.report is not None
    report = result.value.report
    assert report.signal_ids == tuple(sorted((ARTIFACT_ID, OPINION_ID), key=str))
    assert report.evidence_refs == (EVIDENCE_ID,)
    assert len(report.renderings) == 3
    assert artifacts.is_finalized(
        result.value.audit_artifact_ref.removeprefix("sha256:")
    )
    payload = result.value.model_dump(mode="json")
    assert not ({"order", "target", "risk_override"} & set(payload))
    assert telemetry.calls == [
        (ComponentName.PROVIDER, OperationName.FETCH),
        (ComponentName.MODEL, OperationName.INFER),
        (ComponentName.MODEL, OperationName.INFER),
        (ComponentName.LLM, OperationName.GENERATE),
        (ComponentName.DELIVERY, OperationName.GENERATE),
    ]

    rendering = next(
        item for item in report.renderings if item.format == "markdown_full"
    )
    content = artifacts.read(rendering.content_hash)
    assert isinstance(content, Success)
    delivery = FileDeliveryAdapter(
        output_directory=tmp_path, clock=lambda: NOW
    ).deliver(
        DeliveryCommand(
            request=DeliveryRequest(
                delivery_id=UUID("40000000-0000-4000-8000-000000000008"),
                report_id=REPORT_ID,
                channel=DeliveryChannel.FILE,
                format=rendering.format,
                content_hash=rendering.content_hash,
                idempotency_key=f"report:{REPORT_ID}:file",
                required=True,
            ),
            media_type="text/markdown",
            chunks=(content.value.decode(),),
        )
    )
    assert isinstance(delivery, Success)
    assert (tmp_path / str(delivery.value.provider_receipt_id)).is_file()


def test_outages_become_degraded_or_failed_audit_not_fake_success() -> None:
    outage = Failure(
        StructuredError(
            code=ErrorCode.DATA_UNAVAILABLE, message="Dependency unavailable"
        )
    )
    cases = (
        (
            Repository(outage),
            Deterministic(),
            TradingAgents(),
            LLM(),
            PipelineStatus.FAILED,
            False,
        ),
        (
            Repository(),
            Deterministic(outage),
            TradingAgents(),
            LLM(),
            PipelineStatus.FAILED,
            False,
        ),
        (
            Repository(),
            Deterministic(),
            TradingAgents(outage),
            LLM(),
            PipelineStatus.DEGRADED,
            True,
        ),
        (
            Repository(),
            Deterministic(),
            TradingAgents(),
            LLM(outage),
            PipelineStatus.FAILED,
            False,
        ),
    )
    for repository, deterministic, tradingagents, llm, expected, has_report in cases:
        artifacts = MemoryArtifactStore()
        result = run_research_pipeline(
            command(),
            repository=repository,  # type: ignore[arg-type]
            deterministic=deterministic,  # type: ignore[arg-type]
            tradingagents=tradingagents,  # type: ignore[arg-type]
            llm=llm,  # type: ignore[arg-type]
            renderer=JinjaReportRenderer(
                template_directory=Path("templates"),
                artifacts=artifacts,
                clock=lambda: NOW,
            ),
            artifacts=artifacts,
            budget=FixedBudgetEvaluator(),
            clock=lambda: NOW,
        )

        assert isinstance(result, Success)
        assert result.value.status is expected
        assert (result.value.report is not None) is has_report
        assert result.value.issues[0].code == "data_unavailable"
        audit = artifacts.read(result.value.audit_artifact_ref.removeprefix("sha256:"))
        assert isinstance(audit, Success)
        assert b'"status":"succeeded"' not in audit.value
        assert b"order" not in audit.value


def test_operational_budget_degradation_stops_before_external_research() -> None:
    class UnexpectedRepository:
        def query_available(self, *, subject: str, as_of: datetime) -> object:
            del subject, as_of
            raise AssertionError("budget gate must run before evidence access")

    artifacts = MemoryArtifactStore()
    result = run_research_pipeline(
        command(),
        repository=UnexpectedRepository(),  # type: ignore[arg-type]
        deterministic=Deterministic(),  # type: ignore[arg-type]
        tradingagents=TradingAgents(),  # type: ignore[arg-type]
        llm=LLM(),  # type: ignore[arg-type]
        renderer=JinjaReportRenderer(
            template_directory=Path("templates"),
            artifacts=artifacts,
            clock=lambda: NOW,
        ),
        artifacts=artifacts,
        budget=FixedBudgetEvaluator((BudgetStatus.DEGRADED,)),
        clock=lambda: NOW,
    )

    assert isinstance(result, Success)
    assert result.value.status is PipelineStatus.DEGRADED
    assert result.value.report is None
    assert tuple(item.code for item in result.value.issues) == ("budget_exhausted",)


def command() -> ResearchPipelineCommand:
    return ResearchPipelineCommand(
        run_id=RUN_ID,
        owner_subject="research-owner",
        context_request=AnalysisContextRequest(
            context_id=CONTEXT_ID,
            run_id=RUN_ID,
            subject="AAPL",
            as_of=NOW,
            requirements=(
                EvidenceRequirement(
                    capability="market",
                    kinds=(EvidenceKind.MARKET_DATA,),
                    required=True,
                    minimum_items=1,
                    maximum_items=10,
                    freshness_seconds=3600,
                ),
            ),
            allowed_sensitivities=(Sensitivity.PUBLIC,),
            allowed_license_tags=("Apache-2.0",),
            allowed_redistribution_tags=("internal-use",),
        ),
        report_request_id=REPORT_REQUEST_ID,
        report_id=REPORT_ID,
        language="zh-TW",
        report_type="equity_research",
        model="policy:models-v1",
        policy_version="report-policy/1.0.0",
        max_output_tokens=4096,
        deadline_at=NOW + timedelta(minutes=1),
    )


def evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id=EVIDENCE_ID,
        subject="AAPL",
        kind=EvidenceKind.MARKET_DATA,
        payload={"close": "100"},
        event_time=NOW - timedelta(minutes=10),
        published_at=NOW - timedelta(minutes=9),
        available_at=NOW - timedelta(minutes=8),
        observed_at=NOW,
        as_of=NOW,
        source="fixture",
        provider="replay",
        content_hash="a" * 64,
        raw_artifact_ref=f"sha256:{'a' * 64}",
        quality=DataQuality(
            status=DataQualityStatus.AVAILABLE, completeness=Decimal(1)
        ),
        sensitivity=Sensitivity.PUBLIC,
        license_tag="Apache-2.0",
        redistribution_tag="internal-use",
        untrusted_content=True,
    )


def research_artifact() -> ResearchArtifact:
    return ResearchArtifact(
        artifact_id=ARTIFACT_ID,
        request_id=UUID("40000000-0000-4000-8000-000000000009"),
        run_id=RUN_ID,
        instrument_ids=frozenset({"AAPL"}),
        as_of=NOW,
        allowed_evidence_ids=frozenset({EVIDENCE_ID}),
        claims=(
            ResearchClaim(
                claim_id=UUID("40000000-0000-4000-8000-000000000010"),
                kind=ResearchClaimKind.EVIDENCED,
                text="Close is 100.",
                evidence_ids=frozenset({EVIDENCE_ID}),
            ),
        ),
        confidence=Decimal("0.7"),
        raw_output_artifact_ref=f"sha256:{'d' * 64}",
        producer="deterministic-research",
        producer_version="1.0.0",
        usage=UsageConsumption(),
        created_at=NOW,
    )


def opinion() -> AgentOpinion:
    return AgentOpinion(
        opinion_id=OPINION_ID,
        artifact_id=ARTIFACT_ID,
        instrument_id="AAPL",
        as_of=NOW,
        horizon_days=20,
        rating=OpinionRating.BULLISH,
        thesis="Evidence supports a positive research outlook.",
        confidence=Decimal("0.5"),
        confidence_calibration=ConfidenceCalibration.UNCALIBRATED,
        evidence_ids=frozenset({EVIDENCE_ID}),
        producer="tradingagents-isolated-worker",
        producer_version="0.3.1",
        created_at=NOW,
    )
