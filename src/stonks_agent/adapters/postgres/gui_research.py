"""Local GUI composition over durable snapshot and research jobs."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import Lock
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    DatasetSnapshotEvidenceRow,
    EvidenceItemRow,
    JobRow,
    RunDatasetSnapshotRow,
    WorkflowRunRow,
)
from stonks_agent.adapters.postgres.research_query import (
    PostgresResearchRequestStore,
)
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.application.research.request_run import request_research_run
from stonks_agent.composition.runtime import LocalRuntime
from stonks_agent.domain.auth import AccessTarget, LocalPrincipal, ResourceKind
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.gui_research import (
    GuiKronosAlphaView,
    GuiKronosForecastView,
    GuiResearchClaim,
    GuiResearchCommand,
    GuiResearchEvidenceField,
    GuiResearchEvidenceItem,
    GuiResearchEvidenceView,
    GuiResearchHistoryItem,
    GuiResearchHistoryView,
    GuiResearchIssueView,
    GuiResearchRunRef,
    GuiResearchRunView,
    GuiResearchUsageView,
    GuiResearchVersionView,
)
from stonks_agent.domain.job import JobLease, JobStatus
from stonks_agent.domain.market_region import market_for_symbol
from stonks_agent.domain.research_job import ResearchWorkerResult
from stonks_agent.domain.research_pipeline import PipelineStatus
from stonks_agent.domain.research_run import (
    CanonicalRunEvent,
    ResearchRunRequest,
)
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.domain.workflow import WorkflowStatus
from stonks_agent.entrypoints.worker import run_worker_once
from stonks_agent.ports.gui_research import GuiResearchFacade
from stonks_agent.ports.queue import QueuePort
from stonks_contracts.common import stable_payload_hash

type WorkerHandler = Callable[[JobLease], Result[object]]
_SNAPSHOT_WAIT_SECONDS = 120.0
_WORKER_ID = "local-gui-worker"
_EVIDENCE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "vwap",
)


class PostgresGuiResearchFacade(GuiResearchFacade):
    """Submit a live snapshot, then queue one snapshot-bound research run."""

    def __init__(
        self,
        *,
        runtime: LocalRuntime,
        queue: QueuePort,
        handlers: Mapping[str, WorkerHandler],
        worker_lock: Lock | None = None,
    ) -> None:
        self._runtime = runtime
        self._queue = queue
        self._handlers = handlers
        self._worker_lock = worker_lock or Lock()

    def submit(
        self,
        principal: LocalPrincipal,
        command: GuiResearchCommand,
    ) -> Result[GuiResearchRunRef]:
        snapshot = self._submit_snapshot(principal, command)
        if isinstance(snapshot, Failure):
            return snapshot
        snapshot_id = self._wait_for_snapshot(snapshot.value)
        if isinstance(snapshot_id, Failure):
            return snapshot_id
        scoped = principal.model_copy(
            update={
                "targets": frozenset(
                    {
                        *principal.targets,
                        AccessTarget(
                            kind=ResourceKind.SNAPSHOT,
                            identifier=str(snapshot_id.value),
                        ),
                    }
                )
            }
        )
        requested_at = datetime.now(UTC)
        requested = request_research_run(
            scoped,
            ResearchRunRequest(
                instrument_id=f"instrument:{command.symbol.lower()}",
                symbol=command.symbol,
                as_of=command.requested_at + timedelta(minutes=15),
                snapshot_id=snapshot_id.value,
                research_profile_id=command.profile,
                model_policy_id="research-models-v1",
                language="zh-TW",
                idempotency_key=_request_key(command, "research"),
                owner_subject=principal.subject,
                requested_at=requested_at,
            ),
            PostgresResearchRequestStore(self._runtime.engine),
        )
        if isinstance(requested, Failure):
            return requested
        return Success(GuiResearchRunRef(run_id=requested.value.run_id))

    def read(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Result[GuiResearchRunView]:
        loaded = self._run_state(principal, run_id)
        if isinstance(loaded, Failure):
            return loaded
        run, job = loaded.value
        status = _gui_status(run, job)
        result = self._research_result(job)
        if isinstance(result, Failure):
            if status in {"queued", "running"}:
                return Success(_pending_view(run, job, status))
            return result
        if result.value is None:
            return Success(_pending_view(run, job, status))
        value = result.value
        research = value.research_artifact
        pipeline_status = value.status
        snapshot_id = self._snapshot_id(run.run_id)
        if isinstance(snapshot_id, Failure):
            return snapshot_id
        report_content = self._report_content(value)
        kronos_forecast, kronos_alpha = _kronos_views(value)
        return Success(
            GuiResearchRunView(
                run_id=run.run_id,
                symbol=str(job.payload["symbol"]),
                status=(
                    "failed"
                    if pipeline_status is PipelineStatus.FAILED
                    else (
                        "degraded"
                        if pipeline_status is PipelineStatus.DEGRADED
                        else "succeeded"
                    )
                ),
                stage=(
                    "failed" if pipeline_status is PipelineStatus.FAILED else "report"
                ),
                as_of=research.as_of,
                snapshot_id=snapshot_id.value,
                evidence_count=len(research.allowed_evidence_ids),
                claims=tuple(
                    GuiResearchClaim(
                        text=claim.text,
                        evidence_ids=tuple(sorted(claim.evidence_ids, key=str)),
                    )
                    for claim in research.claims
                    if claim.evidence_ids
                ),
                counterarguments=research.counterarguments,
                risks=research.risks,
                confidence=research.confidence,
                kronos_forecast=kronos_forecast,
                kronos_alpha=kronos_alpha,
                usage=GuiResearchUsageView(
                    iterations=research.usage.iterations,
                    tool_calls=research.usage.tool_calls,
                    input_tokens=research.usage.input_tokens,
                    output_tokens=research.usage.output_tokens,
                    cost_usd=research.usage.cost_usd,
                    elapsed_ms=research.usage.elapsed_ms,
                ),
                issues=tuple(
                    GuiResearchIssueView(
                        stage=issue.stage.value,
                        code=issue.code,
                    )
                    for issue in value.pipeline.issues
                ),
                warnings=research.warnings,
                versions=_versions(value),
                paper_decision=_paper_decision(value),
                report_content=report_content,
                error_code=(
                    value.pipeline.issues[0].code
                    if pipeline_status is PipelineStatus.FAILED
                    and value.pipeline.issues
                    else None
                ),
                updated_at=run.updated_at,
            )
        )

    def recent(
        self,
        principal: LocalPrincipal,
        *,
        limit: int,
    ) -> Result[GuiResearchHistoryView]:
        if not 1 <= limit <= 20:
            return _failure(
                ErrorCode.INVALID_INPUT, "Research history limit is invalid"
            )
        try:
            with Session(self._runtime.engine) as session:
                pairs = tuple(
                    session.execute(
                        select(WorkflowRunRow, JobRow)
                        .join(JobRow, JobRow.run_id == WorkflowRunRow.run_id)
                        .where(
                            WorkflowRunRow.owner_subject == principal.subject,
                            WorkflowRunRow.run_type == "research_report",
                            JobRow.job_type == "research_pipeline",
                        )
                        .order_by(
                            WorkflowRunRow.updated_at.desc(),
                            WorkflowRunRow.run_id.desc(),
                        )
                        .limit(limit)
                    )
                )
                for run, job in pairs:
                    session.expunge(run)
                    session.expunge(job)
        except Exception:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Research history is unavailable",
            )
        items = tuple(self._history_item(run, job) for run, job in pairs)
        return Success(GuiResearchHistoryView(items=items))

    def evidence(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Result[GuiResearchEvidenceView]:
        loaded = self._run_state(principal, run_id)
        if isinstance(loaded, Failure):
            return loaded
        result = self._research_result(loaded.value[1])
        if isinstance(result, Failure):
            return result
        if result.value is None:
            return Success(GuiResearchEvidenceView(run_id=run_id, items=()))
        cited = frozenset(
            evidence_id
            for claim in result.value.research_artifact.claims
            for evidence_id in claim.evidence_ids
        )
        if not cited:
            return Success(GuiResearchEvidenceView(run_id=run_id, items=()))
        try:
            with Session(self._runtime.engine) as session:
                link = session.get(RunDatasetSnapshotRow, run_id)
                if link is None:
                    return _failure(
                        ErrorCode.CONFLICT,
                        "Research snapshot binding is unavailable",
                    )
                rows = tuple(
                    session.scalars(
                        select(EvidenceItemRow)
                        .join(
                            DatasetSnapshotEvidenceRow,
                            DatasetSnapshotEvidenceRow.evidence_id
                            == EvidenceItemRow.evidence_id,
                        )
                        .where(
                            DatasetSnapshotEvidenceRow.snapshot_id == link.snapshot_id,
                            EvidenceItemRow.evidence_id.in_(cited),
                        )
                        .order_by(EvidenceItemRow.evidence_id)
                    )
                )
                for row in rows:
                    session.expunge(row)
        except Exception:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Research evidence is unavailable",
            )
        if frozenset(row.evidence_id for row in rows) != cited:
            return _failure(
                ErrorCode.CONFLICT,
                "Cited evidence is outside the bound snapshot",
            )
        return Success(
            GuiResearchEvidenceView(
                run_id=run_id,
                items=tuple(_evidence_view(row) for row in rows),
            )
        )

    def events(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> Result[tuple[CanonicalRunEvent, ...]]:
        loaded = self._run_state(principal, run_id)
        if isinstance(loaded, Failure):
            return loaded
        run, job = loaded.value
        result = self._research_result(job)
        if isinstance(result, Failure):
            return result
        if run.status == WorkflowStatus.SUCCEEDED.value and result.value is None:
            return _failure(
                ErrorCode.CONFLICT,
                "Research terminal artifact is unavailable",
            )
        projected = _project_events(run, job, result.value)
        return Success(
            tuple(item for item in projected if item.sequence > after_sequence)[:limit]
        )

    def worker_once(self) -> Result[bool]:
        with self._worker_lock:
            return run_worker_once(
                self._queue,
                handlers=self._handlers,
                worker_id=_WORKER_ID,
                now=datetime.now(UTC),
                lease_for=timedelta(minutes=10),
            )

    def _submit_snapshot(
        self,
        principal: LocalPrincipal,
        command: GuiResearchCommand,
    ) -> Result[UUID]:
        end_date = command.requested_at.date()
        market = market_for_symbol(command.symbol)
        request = CreateSnapshotRequest(
            market=market,
            capability="prices",
            as_of=command.requested_at + timedelta(minutes=15),
            query={
                "symbol": command.symbol,
                "start_date": (end_date - timedelta(days=180)).isoformat(),
                "end_date": end_date.isoformat(),
                "interval": "1d",
            },
            provider_policy_id=f"{market.lower()}-prices/1",
            idempotency_key=_request_key(command, "snapshot"),
            owner_subject=principal.subject,
            requested_at=command.requested_at,
        )
        submitted = request_snapshot(
            principal,
            request,
            PostgresSnapshotRequestStore(self._runtime.engine),
        )
        if isinstance(submitted, Failure):
            return submitted
        return Success(submitted.value.run_id)

    def _wait_for_snapshot(self, run_id: UUID) -> Result[UUID]:
        deadline = time.monotonic() + _SNAPSHOT_WAIT_SECONDS
        while time.monotonic() < deadline:
            status = _snapshot_status(self._runtime.engine, run_id)
            if isinstance(status, Failure):
                return status
            if status.value is not None:
                return Success(status.value)
            worked = self.worker_once()
            if isinstance(worked, Failure):
                return worked
            if not worked.value:
                time.sleep(0.05)
        return _failure(
            ErrorCode.DATA_UNAVAILABLE,
            "Live snapshot did not complete before the GUI deadline",
        )

    def _run_state(
        self,
        principal: LocalPrincipal,
        run_id: UUID,
    ) -> Result[tuple[WorkflowRunRow, JobRow]]:
        try:
            with Session(self._runtime.engine) as session:
                run = session.get(WorkflowRunRow, run_id)
                jobs = tuple(
                    session.scalars(select(JobRow).where(JobRow.run_id == run_id))
                )
                if run is None or len(jobs) != 1:
                    return _failure(
                        ErrorCode.NOT_FOUND,
                        "Research run was not found",
                    )
                job = jobs[0]
                if (
                    run.owner_subject != principal.subject
                    or run.run_type != "research_report"
                    or job.job_type != "research_pipeline"
                ):
                    return _failure(
                        ErrorCode.FORBIDDEN,
                        "Research run is outside the local principal scope",
                    )
                session.expunge(run)
                session.expunge(job)
                return Success((run, job))
        except Exception:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Research projection is unavailable",
            )

    def _research_result(
        self,
        job: JobRow,
    ) -> Result[ResearchWorkerResult | None]:
        if job.result_artifact_hash is None:
            return Success(None)
        loaded = self._runtime.artifacts.read(job.result_artifact_hash)
        if isinstance(loaded, Failure):
            return loaded
        try:
            return Success(ResearchWorkerResult.model_validate_json(loaded.value))
        except ValueError:
            return _failure(
                ErrorCode.CONFLICT,
                "Research result artifact is invalid",
            )

    def _snapshot_id(self, run_id: UUID) -> Result[UUID]:
        try:
            with Session(self._runtime.engine) as session:
                link = session.get(RunDatasetSnapshotRow, run_id)
                if link is None:
                    return _failure(
                        ErrorCode.CONFLICT,
                        "Research snapshot binding is unavailable",
                    )
                return Success(link.snapshot_id)
        except Exception:
            return _failure(
                ErrorCode.INTERNAL_ERROR,
                "Research snapshot binding is unavailable",
            )

    def _history_item(
        self,
        run: WorkflowRunRow,
        job: JobRow,
    ) -> GuiResearchHistoryItem:
        status = _gui_status(run, job)
        stage = "queued" if status == "queued" else "research"
        confidence = None
        issue_count = 0
        error_code = _job_error_code(job)
        result = self._research_result(job)
        if isinstance(result, Failure):
            if status not in {"queued", "running"}:
                status = "failed"
                stage = "failed"
                error_code = "result_unavailable"
        elif result.value is not None:
            value = result.value
            status = value.status.value
            stage = "failed" if value.status is PipelineStatus.FAILED else "report"
            confidence = value.research_artifact.confidence
            issue_count = len(value.pipeline.issues)
            if value.status is PipelineStatus.FAILED and value.pipeline.issues:
                error_code = value.pipeline.issues[0].code
        return GuiResearchHistoryItem(
            run_id=run.run_id,
            symbol=str(job.payload["symbol"]),
            profile=run.policy_id,
            status=status,  # type: ignore[arg-type]
            stage=stage,
            as_of=run.as_of,
            confidence=confidence,
            issue_count=issue_count,
            error_code=error_code,
            updated_at=run.updated_at,
        )

    def _report_content(self, value: ResearchWorkerResult) -> str | None:
        report = value.pipeline.report
        if report is None or not report.renderings:
            return None
        reference = report.renderings[0].content_ref
        if not reference.startswith("sha256:"):
            return None
        loaded = self._runtime.artifacts.read(reference.removeprefix("sha256:"))
        if isinstance(loaded, Failure):
            return None
        try:
            return loaded.value.decode("utf-8")
        except UnicodeDecodeError:
            return None


def _kronos_views(
    value: ResearchWorkerResult,
) -> tuple[GuiKronosForecastView | None, GuiKronosAlphaView | None]:
    outcome = value.kronos
    if outcome is None:
        return None, None
    alpha = outcome.alpha_signal
    alpha_view = GuiKronosAlphaView(
        state="mapped" if alpha is not None else "blocked",
        deployment_state=outcome.deployment_state.value,  # type: ignore[arg-type]
        eligible=outcome.eligibility.eligible,
        weight=outcome.eligibility.weight,
        reason_codes=outcome.eligibility.reason_codes,
        value=alpha.value if alpha is not None else None,
        direction=alpha.direction.value if alpha is not None else None,
        confidence=alpha.confidence if alpha is not None else None,
    )
    output = outcome.forecast_output
    if output is None:
        assert outcome.error_code is not None
        return (
            GuiKronosForecastView(
                state="failed",
                actual_model_inference=False,
                error_code=outcome.error_code.value,
            ),
            alpha_view,
        )
    forecast = output.forecast
    return (
        GuiKronosForecastView(
            state="succeeded",
            actual_model_inference=outcome.actual_model_inference,
            forecast_id=forecast.forecast_id,
            model_id=forecast.model_id,
            model_revision=forecast.model_revision,
            generated_at=forecast.generated_at,
            horizon_bars=forecast.horizon_bars,
            path_count=forecast.path_count,
            expected_return=forecast.expected_return,
            median_return=forecast.median_return,
            direction_probability=forecast.direction_probability,
            expected_volatility=forecast.expected_volatility,
            downside_quantile=forecast.downside_quantile,
            max_drawdown_quantile=forecast.max_drawdown_quantile,
            quality_status=forecast.input_quality.status.value,
            warnings=(
                *forecast.input_quality.warnings,
                *forecast.validity_warnings,
            ),
        ),
        alpha_view,
    )


def _versions(value: ResearchWorkerResult) -> tuple[GuiResearchVersionView, ...]:
    research = value.research_artifact
    values = [
        GuiResearchVersionView(
            component="research_producer",
            version=f"{research.producer}@{research.producer_version}",
        )
    ]
    values.extend(
        GuiResearchVersionView(component=f"model:{index}", version=version)
        for index, version in enumerate(research.model_versions, start=1)
    )
    values.extend(
        GuiResearchVersionView(component=f"tool:{index}", version=version)
        for index, version in enumerate(research.tool_versions, start=1)
    )
    return tuple(values)


def _evidence_view(row: EvidenceItemRow) -> GuiResearchEvidenceItem:
    quality = row.quality if isinstance(row.quality, dict) else {}
    raw_completeness = quality.get("completeness", "0")
    try:
        completeness = Decimal(str(raw_completeness))
    except (InvalidOperation, ValueError):
        completeness = Decimal(0)
    completeness = min(Decimal(1), max(Decimal(0), completeness))
    raw_warnings = quality.get("warnings", ())
    warnings = (
        tuple(
            item[:4_096] for item in raw_warnings[:16] if isinstance(item, str) and item
        )
        if isinstance(raw_warnings, list | tuple)
        else ()
    )
    return GuiResearchEvidenceItem(
        evidence_id=row.evidence_id,
        kind=row.kind,
        source=row.source,
        provider=row.provider,
        event_time=row.event_time,
        available_at=row.available_at,
        quality_status=row.quality_state,
        completeness=completeness,
        warnings=warnings,
        content_hash=row.content_hash,
        fields=_evidence_fields(row),
    )


def _evidence_fields(
    row: EvidenceItemRow,
) -> tuple[GuiResearchEvidenceField, ...]:
    if row.kind != "market_data" or row.sensitivity == "restricted":
        return ()
    values: list[GuiResearchEvidenceField] = []
    for name in _EVIDENCE_FIELDS:
        value = row.payload.get(name)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            continue
        rendered = str(value)
        if not rendered or len(rendered) > 512:
            continue
        values.append(GuiResearchEvidenceField(name=name, value=rendered))
    return tuple(values)


def _job_error_code(job: JobRow) -> str | None:
    value = job.last_error.get("code") if isinstance(job.last_error, dict) else None
    return value if isinstance(value, str) else None


def _paper_decision(value: ResearchWorkerResult) -> str | None:
    outcome = value.kronos
    if outcome is None:
        return None
    reasons = ", ".join(outcome.eligibility.reason_codes)
    if not outcome.eligibility.eligible:
        return f"no-order: {reasons}"
    return "no-order: signal eligible, but this research view has no order authority"


def _kronos_event_payload(
    result: ResearchWorkerResult | None,
) -> dict[str, object]:
    outcome = result.kronos if result is not None else None
    if outcome is None:
        return {}
    return {
        "kronos_status": outcome.status,
        "alpha_status": outcome.alpha_status,
    }


def _snapshot_status(engine: Engine, run_id: UUID) -> Result[UUID | None]:
    try:
        with Session(engine) as session:
            job = session.scalar(select(JobRow).where(JobRow.run_id == run_id))
            link = session.get(RunDatasetSnapshotRow, run_id)
            if job is None or job.job_type != "create_snapshot":
                return _failure(ErrorCode.CONFLICT, "Snapshot job is invalid")
            if link is not None:
                return Success(link.snapshot_id)
            if job.status == JobStatus.DEAD_LETTER.value:
                return _failure(
                    ErrorCode.DATA_UNAVAILABLE,
                    "Live snapshot failed closed",
                )
            return Success(None)
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "Snapshot status is unavailable")


def _pending_view(
    run: WorkflowRunRow,
    job: JobRow,
    status: str,
) -> GuiResearchRunView:
    raw_error_code: object = (
        job.last_error.get("code") if isinstance(job.last_error, dict) else None
    )
    error_code = raw_error_code if isinstance(raw_error_code, str) else None
    return GuiResearchRunView(
        run_id=run.run_id,
        symbol=str(job.payload["symbol"]),
        status=status,  # type: ignore[arg-type]
        stage="queued" if status == "queued" else "research",
        error_code=error_code,
        updated_at=run.updated_at,
    )


def _gui_status(run: WorkflowRunRow, job: JobRow) -> str:
    if run.status == WorkflowStatus.FAILED.value:
        return "failed"
    if run.status == WorkflowStatus.CANCELLED.value:
        return "cancelled"
    if run.status == WorkflowStatus.SUCCEEDED.value:
        return "succeeded"
    if job.status == JobStatus.LEASED.value:
        return "running"
    return "queued"


def _project_events(
    run: WorkflowRunRow,
    job: JobRow,
    result: ResearchWorkerResult | None,
) -> tuple[CanonicalRunEvent, ...]:
    values: list[tuple[str, str, datetime, dict[str, object]]] = [
        ("research.queued", "queued", run.created_at, {})
    ]
    if job.attempts > 0 or job.status != JobStatus.QUEUED.value:
        values.append(("research.running", "research", job.updated_at, {}))
    if run.status == WorkflowStatus.SUCCEEDED.value:
        status = result.status if result is not None else PipelineStatus.FAILED
        terminal = {
            PipelineStatus.SUCCEEDED: "research.succeeded",
            PipelineStatus.DEGRADED: "research.degraded",
            PipelineStatus.FAILED: "research.failed",
        }[status]
        values.append(
            (
                terminal,
                "failed" if status is PipelineStatus.FAILED else "report",
                run.updated_at,
                _kronos_event_payload(result),
            )
        )
    elif run.status == WorkflowStatus.FAILED.value:
        values.append(("research.failed", "failed", run.updated_at, {}))
    elif run.status == WorkflowStatus.CANCELLED.value:
        values.append(("research.cancelled", "cancelled", run.updated_at, {}))
    return tuple(
        _event(run.run_id, sequence, event_type, stage, occurred_at, extra)
        for sequence, (event_type, stage, occurred_at, extra) in enumerate(
            values,
            start=1,
        )
    )


def _event(
    run_id: UUID,
    sequence: int,
    event_type: str,
    stage: str,
    occurred_at: datetime,
    extra: Mapping[str, object],
) -> CanonicalRunEvent:
    payload: dict[str, object] = {"stage": stage, **extra}
    event_id = uuid5(
        NAMESPACE_URL,
        f"stonks:gui-research:{run_id}:{sequence}:{event_type}",
    )
    return CanonicalRunEvent(
        event_id=event_id,
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        event_hash=stable_payload_hash(
            {
                "event_id": str(event_id),
                "run_id": str(run_id),
                "sequence": sequence,
                "event_type": event_type,
                "payload": payload,
                "occurred_at": occurred_at.isoformat(),
            }
        ),
    )


def _request_key(command: GuiResearchCommand, kind: str) -> str:
    digest = sha256(
        f"{command.symbol}:{command.interval.value}:{command.profile}:"
        f"{command.requested_at.isoformat()}:{kind}".encode()
    ).hexdigest()[:24]
    return f"gui-{kind}-{digest}"


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
