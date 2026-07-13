"""PostgreSQL strategy registry with CAS promotion and immutable audit events."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    DatasetSnapshotRow,
    StrategyAuditEventRow,
    StrategyEvaluationReportRow,
    StrategyRegistryRow,
)
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.evaluation import EvaluationReport
from stonks_agent.domain.strategy import (
    PromotionState,
    StrategyAuditEvent,
    StrategyKind,
    StrategyManifest,
    StrategyMutationResult,
    StrategyRegistryEntry,
    StrategyTransitionRequest,
    can_transition,
)
from stonks_contracts.common import stable_payload_hash


class PostgresStrategyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, manifest: StrategyManifest) -> Result[StrategyMutationResult]:
        existing = self._get_row(manifest.strategy_id, manifest.strategy_version)
        if existing is not None:
            if _manifest_from_row(existing) != manifest:
                return _failure(ErrorCode.CONFLICT, "Strategy identity already exists")
            event_result = self.list_events(
                manifest.strategy_id, manifest.strategy_version
            )
            if isinstance(event_result, Failure):
                return event_result
            return Success(
                StrategyMutationResult(
                    entry=_entry_from_row(existing),
                    event=event_result.value[0],
                )
            )
        occurred_at = self._database_now()
        row = _registry_row(manifest, occurred_at)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Strategy registration conflicted")
        self._session.refresh(row)
        event = _new_event(
            strategy_id=manifest.strategy_id,
            strategy_version=manifest.strategy_version,
            sequence=1,
            event_type="strategy.registered",
            from_state=None,
            to_state=PromotionState.DRAFT,
            reason_code="strategy_registered",
            actor="system:registry",
            evaluation_report_id=None,
            evaluation_hash=None,
            occurred_at=row.updated_at,
            previous_hash=None,
        )
        self._session.add(_event_row(event))
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Strategy registration conflicted")
        return Success(StrategyMutationResult(entry=_entry_from_row(row), event=event))

    def get(
        self,
        strategy_id: str,
        strategy_version: str,
    ) -> Result[StrategyRegistryEntry]:
        row = self._get_row(strategy_id, strategy_version)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Strategy was not found")
        audit = self._validate_current_audit(row)
        if isinstance(audit, Failure):
            return audit
        return Success(_entry_from_row(row))

    def register_evaluation(
        self,
        report: EvaluationReport,
    ) -> Result[EvaluationReport]:
        registry = self._get_row(report.strategy_id, report.strategy_version)
        if registry is None:
            return _failure(ErrorCode.NOT_FOUND, "Strategy was not found")
        audit = self._validate_current_audit(registry)
        if isinstance(audit, Failure):
            return audit
        if not _evaluation_matches_registry(report, registry):
            return _failure(ErrorCode.CONFLICT, "Evaluation strategy binding mismatch")
        snapshot = self._session.get(DatasetSnapshotRow, report.dataset_snapshot_id)
        if snapshot is None:
            return _failure(ErrorCode.NOT_FOUND, "Evaluation snapshot was not found")
        if (
            snapshot.content_hash != report.data_hash
            or snapshot.as_of > report.as_of
            or snapshot.cutoff_at > report.as_of
        ):
            return _failure(ErrorCode.CONFLICT, "Evaluation snapshot binding mismatch")
        existing = self._session.get(StrategyEvaluationReportRow, report.report_id)
        if existing is not None:
            persisted = _evaluation_from_row(existing)
            if (
                persisted == report
                and existing.evaluation_hash == report.evaluation_hash
            ):
                return Success(persisted)
            return _failure(ErrorCode.CONFLICT, "Evaluation report id already exists")
        duplicate_hash = self._session.scalar(
            select(StrategyEvaluationReportRow.report_id).where(
                StrategyEvaluationReportRow.evaluation_hash == report.evaluation_hash
            )
        )
        if duplicate_hash is not None:
            return _failure(ErrorCode.CONFLICT, "Evaluation hash already exists")
        self._session.add(_evaluation_row(report))
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Evaluation registration conflicted")
        return Success(report)

    def get_evaluation(self, report_id: UUID) -> Result[EvaluationReport]:
        row = self._session.get(StrategyEvaluationReportRow, report_id)
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Evaluation report was not found")
        report = _evaluation_from_row(row)
        registry = self._get_row(report.strategy_id, report.strategy_version)
        if registry is None:
            return _failure(ErrorCode.CONFLICT, "Evaluation strategy is unavailable")
        audit = self._validate_current_audit(registry)
        if isinstance(audit, Failure):
            return audit
        if not _evaluation_matches_row(report, row) or not _evaluation_matches_registry(
            report, registry
        ):
            return _failure(ErrorCode.CONFLICT, "Evaluation report integrity failed")
        return Success(report)

    def transition(
        self,
        request: StrategyTransitionRequest,
    ) -> Result[StrategyMutationResult]:
        current = self._get_row(request.strategy_id, request.strategy_version)
        if current is None:
            return _failure(ErrorCode.NOT_FOUND, "Strategy was not found")
        audit = self._validate_current_audit(current)
        if isinstance(audit, Failure):
            return audit
        if (
            current.version != request.expected_version
            or current.state != request.current_state.value
            or not can_transition(request.current_state, request.target_state)
        ):
            return _failure(ErrorCode.CONFLICT, "Strategy CAS precondition failed")
        report_result = self._resolve_transition_evaluation(request, current)
        if isinstance(report_result, Failure):
            return report_result
        report = report_result.value
        validation_at = self._database_now()
        if report is not None and not _evaluation_allows_target(
            report, request.target_state, validation_at
        ):
            return _failure(ErrorCode.CONFLICT, "Evaluation does not allow promotion")
        report_id = (
            report.report_id if report is not None else current.evaluation_report_id
        )
        evaluation_hash = (
            report.evaluation_hash if report is not None else current.evaluation_hash
        )
        updated_row = self._apply_transition_update(
            request,
            report_id=report_id,
            evaluation_hash=evaluation_hash,
        )
        if updated_row is None:
            return _failure(ErrorCode.CONFLICT, "Strategy version changed concurrently")
        return self._append_transition_event(
            request,
            updated_row=updated_row,
            report_id=report_id,
            evaluation_hash=evaluation_hash,
        )

    def _apply_transition_update(
        self,
        request: StrategyTransitionRequest,
        *,
        report_id: UUID | None,
        evaluation_hash: str | None,
    ) -> StrategyRegistryRow | None:
        return self._session.scalar(
            update(StrategyRegistryRow)
            .where(
                StrategyRegistryRow.strategy_id == request.strategy_id,
                StrategyRegistryRow.strategy_version == request.strategy_version,
                StrategyRegistryRow.version == request.expected_version,
                StrategyRegistryRow.state == request.current_state.value,
            )
            .values(
                state=request.target_state.value,
                evaluation_report_id=report_id,
                evaluation_hash=evaluation_hash,
                version=request.expected_version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(StrategyRegistryRow)
        )

    def _append_transition_event(
        self,
        request: StrategyTransitionRequest,
        *,
        updated_row: StrategyRegistryRow,
        report_id: UUID | None,
        evaluation_hash: str | None,
    ) -> Result[StrategyMutationResult]:
        previous = self._latest_event(request.strategy_id, request.strategy_version)
        if previous is None or previous.sequence != request.expected_version:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Strategy audit chain is inconsistent")
        event = _new_event(
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            sequence=request.expected_version + 1,
            event_type=f"strategy.{request.target_state.value}",
            from_state=request.current_state,
            to_state=request.target_state,
            reason_code=request.reason_code,
            actor=request.actor,
            evaluation_report_id=report_id,
            evaluation_hash=evaluation_hash,
            occurred_at=updated_row.updated_at,
            previous_hash=previous.event_hash,
        )
        self._session.add(_event_row(event))
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            return _failure(ErrorCode.CONFLICT, "Strategy audit append conflicted")
        return Success(
            StrategyMutationResult(entry=_entry_from_row(updated_row), event=event)
        )

    def list_events(
        self,
        strategy_id: str,
        strategy_version: str,
    ) -> Result[tuple[StrategyAuditEvent, ...]]:
        rows = self._session.scalars(
            select(StrategyAuditEventRow)
            .where(
                StrategyAuditEventRow.strategy_id == strategy_id,
                StrategyAuditEventRow.strategy_version == strategy_version,
            )
            .order_by(StrategyAuditEventRow.sequence)
        ).all()
        if not rows:
            return _failure(ErrorCode.NOT_FOUND, "Strategy audit events were not found")
        events = tuple(_event_from_row(row) for row in rows)
        previous_hash: str | None = None
        for sequence, event in enumerate(events, start=1):
            if (
                event.sequence != sequence
                or event.previous_hash != previous_hash
                or event.event_hash != _calculate_event_hash(event)
            ):
                return _failure(ErrorCode.CONFLICT, "Strategy audit chain is invalid")
            previous_hash = event.event_hash
        return Success(events)

    def _resolve_transition_evaluation(
        self,
        request: StrategyTransitionRequest,
        registry: StrategyRegistryRow,
    ) -> Result[EvaluationReport | None]:
        if request.evaluation_report_id is None:
            return Success(None)
        row = self._session.get(
            StrategyEvaluationReportRow, request.evaluation_report_id
        )
        if row is None:
            return _failure(ErrorCode.NOT_FOUND, "Evaluation report was not found")
        report = _evaluation_from_row(row)
        if (
            request.evaluation_hash != row.evaluation_hash
            or not _evaluation_matches_registry(report, registry)
        ):
            return _failure(ErrorCode.CONFLICT, "Evaluation report binding mismatch")
        return Success(report)

    def _validate_current_audit(
        self,
        registry: StrategyRegistryRow,
    ) -> Result[StrategyAuditEvent]:
        result = self.list_events(registry.strategy_id, registry.strategy_version)
        if isinstance(result, Failure):
            return _failure(ErrorCode.CONFLICT, "Strategy audit is unavailable")
        latest = result.value[-1]
        if (
            latest.sequence != registry.version
            or latest.to_state.value != registry.state
            or latest.evaluation_report_id != registry.evaluation_report_id
            or latest.evaluation_hash != registry.evaluation_hash
        ):
            return _failure(ErrorCode.CONFLICT, "Strategy audit projection mismatch")
        return Success(latest)

    def _get_row(
        self,
        strategy_id: str,
        strategy_version: str,
    ) -> StrategyRegistryRow | None:
        return self._session.get(StrategyRegistryRow, (strategy_id, strategy_version))

    def _latest_event(
        self,
        strategy_id: str,
        strategy_version: str,
    ) -> StrategyAuditEventRow | None:
        return self._session.scalar(
            select(StrategyAuditEventRow)
            .where(
                StrategyAuditEventRow.strategy_id == strategy_id,
                StrategyAuditEventRow.strategy_version == strategy_version,
            )
            .order_by(StrategyAuditEventRow.sequence.desc())
            .limit(1)
        )

    def _database_now(self) -> datetime:
        value = self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):  # pragma: no cover - DB contract invariant
            raise RuntimeError("PostgreSQL clock_timestamp did not return datetime")
        return value


def _registry_row(
    manifest: StrategyManifest, occurred_at: datetime
) -> StrategyRegistryRow:
    return StrategyRegistryRow(
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.strategy_version,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        kind=manifest.kind.value,
        source_artifact_hash=manifest.source_artifact_ref.removeprefix("sha256:"),
        runtime_hash=manifest.runtime_hash,
        feature_spec_hash=manifest.feature_spec_hash,
        label_spec_hash=manifest.label_spec_hash,
        universe_spec_hash=manifest.universe_spec_hash,
        cost_model_hash=manifest.cost_model_hash,
        split_policy_hash=manifest.split_policy_hash,
        parameters_hash=manifest.parameters_hash,
        owner=manifest.owner,
        deterministic=manifest.deterministic,
        manifest_created_at=manifest.created_at,
        state=PromotionState.DRAFT.value,
        version=1,
        created_at=occurred_at,
        updated_at=occurred_at,
    )


def _manifest_from_row(row: StrategyRegistryRow) -> StrategyManifest:
    return StrategyManifest(
        manifest_id=row.manifest_id,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        kind=StrategyKind(row.kind),
        source_artifact_ref=f"sha256:{row.source_artifact_hash}",
        runtime_hash=row.runtime_hash,
        feature_spec_hash=row.feature_spec_hash,
        label_spec_hash=row.label_spec_hash,
        universe_spec_hash=row.universe_spec_hash,
        cost_model_hash=row.cost_model_hash,
        split_policy_hash=row.split_policy_hash,
        parameters_hash=row.parameters_hash,
        owner=row.owner,
        deterministic=row.deterministic,
        created_at=row.manifest_created_at,
    )


def _entry_from_row(row: StrategyRegistryRow) -> StrategyRegistryEntry:
    return StrategyRegistryEntry(
        manifest=_manifest_from_row(row),
        state=PromotionState(row.state),
        evaluation_report_id=row.evaluation_report_id,
        evaluation_hash=row.evaluation_hash,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _evaluation_row(report: EvaluationReport) -> StrategyEvaluationReportRow:
    return StrategyEvaluationReportRow(
        report_id=report.report_id,
        strategy_id=report.strategy_id,
        strategy_version=report.strategy_version,
        strategy_manifest_hash=report.strategy_manifest_hash,
        dataset_snapshot_id=report.dataset_snapshot_id,
        data_hash=report.data_hash,
        runtime_hash=report.runtime_hash,
        evaluation_policy_hash=report.evaluation_policy_hash,
        evaluation_hash=report.evaluation_hash,
        report_artifact_hash=report.report_artifact_ref.removeprefix("sha256:"),
        passed=report.passed,
        calibration=report.calibration.value,
        valid_until=report.valid_until,
        payload=report.model_dump(mode="json"),
        created_at=report.created_at,
    )


def _evaluation_from_row(row: StrategyEvaluationReportRow) -> EvaluationReport:
    return EvaluationReport.model_validate(row.payload)


def _evaluation_matches_registry(
    report: EvaluationReport,
    registry: StrategyRegistryRow,
) -> bool:
    return (
        report.strategy_id == registry.strategy_id
        and report.strategy_version == registry.strategy_version
        and report.strategy_manifest_hash == registry.manifest_hash
        and report.runtime_hash == registry.runtime_hash
    )


def _evaluation_matches_row(
    report: EvaluationReport,
    row: StrategyEvaluationReportRow,
) -> bool:
    return (
        report.report_id == row.report_id
        and report.strategy_id == row.strategy_id
        and report.strategy_version == row.strategy_version
        and report.strategy_manifest_hash == row.strategy_manifest_hash
        and report.dataset_snapshot_id == row.dataset_snapshot_id
        and report.data_hash == row.data_hash
        and report.runtime_hash == row.runtime_hash
        and report.evaluation_policy_hash == row.evaluation_policy_hash
        and report.evaluation_hash == row.evaluation_hash
        and report.report_artifact_ref == f"sha256:{row.report_artifact_hash}"
        and report.passed == row.passed
        and report.calibration.value == row.calibration
        and report.valid_until == row.valid_until
        and report.created_at == row.created_at
    )


def _evaluation_allows_target(
    report: EvaluationReport,
    target: PromotionState,
    occurred_at: datetime,
) -> bool:
    if target in {PromotionState.SHADOW, PromotionState.PAPER_ELIGIBLE}:
        return report.passed and report.valid_until > occurred_at
    if target is PromotionState.REJECTED:
        return not report.passed
    return True


def _new_event(
    *,
    strategy_id: str,
    strategy_version: str,
    sequence: int,
    event_type: str,
    from_state: PromotionState | None,
    to_state: PromotionState,
    reason_code: str,
    actor: str,
    evaluation_report_id: UUID | None,
    evaluation_hash: str | None,
    occurred_at: datetime,
    previous_hash: str | None,
) -> StrategyAuditEvent:
    identity = _event_payload(
        event_id=None,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        sequence=sequence,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        reason_code=reason_code,
        actor=actor,
        evaluation_report_id=evaluation_report_id,
        evaluation_hash=evaluation_hash,
        occurred_at=occurred_at,
        previous_hash=previous_hash,
    )
    event_id = uuid5(NAMESPACE_URL, stable_payload_hash(identity))
    payload = identity | {"event_id": str(event_id)}
    return StrategyAuditEvent(
        event_id=event_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        sequence=sequence,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        reason_code=reason_code,
        actor=actor,
        evaluation_report_id=evaluation_report_id,
        evaluation_hash=evaluation_hash,
        occurred_at=occurred_at,
        previous_hash=previous_hash,
        event_hash=stable_payload_hash(payload),
    )


def _event_payload(
    *,
    event_id: UUID | None,
    strategy_id: str,
    strategy_version: str,
    sequence: int,
    event_type: str,
    from_state: PromotionState | None,
    to_state: PromotionState,
    reason_code: str,
    actor: str,
    evaluation_report_id: UUID | None,
    evaluation_hash: str | None,
    occurred_at: datetime,
    previous_hash: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "sequence": sequence,
        "event_type": event_type,
        "from_state": from_state.value if from_state is not None else None,
        "to_state": to_state.value,
        "reason_code": reason_code,
        "actor": actor,
        "evaluation_report_id": (
            str(evaluation_report_id) if evaluation_report_id is not None else None
        ),
        "evaluation_hash": evaluation_hash,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "previous_hash": previous_hash,
    }
    if event_id is not None:
        payload["event_id"] = str(event_id)
    return payload


def _calculate_event_hash(event: StrategyAuditEvent) -> str:
    payload = _event_payload(
        event_id=event.event_id,
        strategy_id=event.strategy_id,
        strategy_version=event.strategy_version,
        sequence=event.sequence,
        event_type=event.event_type,
        from_state=event.from_state,
        to_state=event.to_state,
        reason_code=event.reason_code,
        actor=event.actor,
        evaluation_report_id=event.evaluation_report_id,
        evaluation_hash=event.evaluation_hash,
        occurred_at=event.occurred_at,
        previous_hash=event.previous_hash,
    )
    return stable_payload_hash(payload)


def _event_row(event: StrategyAuditEvent) -> StrategyAuditEventRow:
    return StrategyAuditEventRow(
        event_id=event.event_id,
        strategy_id=event.strategy_id,
        strategy_version=event.strategy_version,
        sequence=event.sequence,
        event_type=event.event_type,
        from_state=event.from_state.value if event.from_state is not None else None,
        to_state=event.to_state.value,
        reason_code=event.reason_code,
        actor=event.actor,
        evaluation_report_id=event.evaluation_report_id,
        evaluation_hash=event.evaluation_hash,
        occurred_at=event.occurred_at,
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
    )


def _event_from_row(row: StrategyAuditEventRow) -> StrategyAuditEvent:
    return StrategyAuditEvent(
        event_id=row.event_id,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        sequence=row.sequence,
        event_type=row.event_type,
        from_state=PromotionState(row.from_state) if row.from_state else None,
        to_state=PromotionState(row.to_state),
        reason_code=row.reason_code,
        actor=row.actor,
        evaluation_report_id=row.evaluation_report_id,
        evaluation_hash=row.evaluation_hash,
        occurred_at=row.occurred_at,
        previous_hash=row.previous_hash,
        event_hash=row.event_hash,
    )


def _failure(code: ErrorCode, message: str) -> Failure:
    return Failure(StructuredError(code=code, message=message))
