"""SQLAlchemy repositories for canonical evidence and workflow state."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    EvidenceEdgeRow,
    EvidenceItemRow,
    WorkflowRunRow,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.workflow import (
    CreateWorkflowRun,
    WorkflowRunRecord,
    WorkflowStatus,
    can_transition,
)
from stonks_contracts.evidence import EvidenceItem, EvidenceKind, Sensitivity
from stonks_contracts.market_data import DataQuality


class PostgresEvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, item: EvidenceItem) -> Result[EvidenceItem]:
        duplicate = self._session.scalar(
            select(EvidenceItemRow.evidence_id).where(
                or_(
                    EvidenceItemRow.evidence_id == item.evidence_id,
                    EvidenceItemRow.content_hash == item.content_hash,
                )
            )
        )
        if duplicate is not None:
            return _failure(ErrorCode.CONFLICT, "Evidence already exists")
        self._session.add(_evidence_row(item))
        for parent_id in item.derived_from:
            self._session.add(
                EvidenceEdgeRow(
                    parent_evidence_id=parent_id,
                    child_evidence_id=item.evidence_id,
                    relation="derived_from",
                    transformation_version=item.transformation_version or "",
                    created_at=item.observed_at,
                )
            )
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Evidence append violated a constraint")
        return Success(item)

    def get(self, evidence_id: UUID) -> Result[EvidenceItem]:
        row = self._session.get(EvidenceItemRow, evidence_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Evidence was not found")
        return Success(self._to_contract(row))

    def query_available(
        self,
        *,
        subject: str,
        as_of: datetime,
    ) -> Result[tuple[EvidenceItem, ...]]:
        rows = self._session.scalars(
            select(EvidenceItemRow)
            .where(
                EvidenceItemRow.subject == subject,
                EvidenceItemRow.available_at <= as_of,
                or_(EvidenceItemRow.expires_at.is_(None), EvidenceItemRow.expires_at > as_of),
            )
            .order_by(EvidenceItemRow.available_at, EvidenceItemRow.evidence_id)
        ).all()
        return Success(tuple(self._to_contract(row) for row in rows))

    def _to_contract(self, row: EvidenceItemRow) -> EvidenceItem:
        parents = self._session.scalars(
            select(EvidenceEdgeRow.parent_evidence_id)
            .where(EvidenceEdgeRow.child_evidence_id == row.evidence_id)
            .order_by(EvidenceEdgeRow.parent_evidence_id)
        ).all()
        return EvidenceItem(
            evidence_id=row.evidence_id,
            subject=row.subject,
            kind=EvidenceKind(row.kind),
            payload=row.payload,
            event_time=row.event_time,
            published_at=row.published_at,
            available_at=row.available_at,
            observed_at=row.observed_at,
            as_of=row.as_of,
            source=row.source,
            provider=row.provider,
            source_url=row.source_url,
            content_hash=row.content_hash,
            raw_artifact_ref=f"sha256:{row.raw_artifact_hash}",
            quality=DataQuality.model_validate(row.quality),
            sensitivity=Sensitivity(row.sensitivity),
            license_tag=row.license_tag,
            redistribution_tag=row.redistribution_tag,
            expires_at=row.expires_at,
            derived_from=tuple(parents),
            transformation_version=row.transformation_version,
            untrusted_content=row.untrusted_content,
        )


class PostgresWorkflowStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, request: CreateWorkflowRun) -> Result[WorkflowRunRecord]:
        existing = self._session.scalar(
            select(WorkflowRunRow).where(
                WorkflowRunRow.idempotency_key == request.idempotency_key
            )
        )
        if existing is not None:
            if existing.input_hash != request.input_hash:
                return _failure(ErrorCode.CONFLICT, "Run idempotency payload mismatch")
            return Success(_workflow_record(existing))
        row = WorkflowRunRow(
            run_id=request.run_id,
            run_type=request.run_type,
            status=WorkflowStatus.PENDING.value,
            as_of=request.as_of,
            policy_id=request.policy_id,
            idempotency_key=request.idempotency_key,
            input_hash=request.input_hash,
            version=1,
            created_at=request.created_at,
            updated_at=request.created_at,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Run already exists")
        return Success(_workflow_record(row))

    def get(self, run_id: UUID) -> Result[WorkflowRunRecord]:
        row = self._session.get(WorkflowRunRow, run_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Run was not found")
        return Success(_workflow_record(row))

    def transition(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        new_status: WorkflowStatus,
        updated_at: datetime,
    ) -> Result[WorkflowRunRecord]:
        current = self._session.get(WorkflowRunRow, run_id)
        if current is None:
            return _failure(ErrorCode.NOT_FOUND, "Run was not found")
        current_status = WorkflowStatus(current.status)
        if not can_transition(current_status, new_status):
            return _failure(ErrorCode.CONFLICT, "Run transition is not allowed")
        row = self._session.scalar(
            update(WorkflowRunRow)
            .where(
                WorkflowRunRow.run_id == run_id,
                WorkflowRunRow.version == expected_version,
                WorkflowRunRow.status == current_status.value,
            )
            .values(
                status=new_status.value,
                version=expected_version + 1,
                updated_at=updated_at,
            )
            .returning(WorkflowRunRow)
        )
        if row is None:
            return _failure(ErrorCode.CONFLICT, "Run version changed concurrently")
        self._session.flush()
        return Success(_workflow_record(row))


def _evidence_row(item: EvidenceItem) -> EvidenceItemRow:
    return EvidenceItemRow(
        evidence_id=item.evidence_id,
        subject=item.subject,
        kind=item.kind.value,
        payload=item.payload,
        event_time=item.event_time,
        published_at=item.published_at,
        available_at=item.available_at,
        observed_at=item.observed_at,
        as_of=item.as_of,
        availability_certainty="proven",
        strict_point_in_time=True,
        source=item.source,
        provider=item.provider,
        source_url=item.source_url,
        content_hash=item.content_hash,
        raw_artifact_hash=item.raw_artifact_ref.removeprefix("sha256:"),
        quality_state=item.quality.status.value,
        quality=item.quality.model_dump(mode="json"),
        sensitivity=item.sensitivity.value,
        license_tag=item.license_tag,
        redistribution_tag=item.redistribution_tag,
        expires_at=item.expires_at,
        transformation_version=item.transformation_version,
        untrusted_content=item.untrusted_content,
        created_at=item.observed_at,
    )


def _workflow_record(row: WorkflowRunRow) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=row.run_id,
        run_type=row.run_type,
        as_of=row.as_of,
        policy_id=row.policy_id,
        idempotency_key=row.idempotency_key,
        input_hash=row.input_hash,
        created_at=row.created_at,
        status=WorkflowStatus(row.status),
        version=row.version,
        updated_at=row.updated_at,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
