"""Canonical SQLAlchemy table mappings for P1 durable state."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stonks_agent.adapters.postgres.secret_free_json import SecretFreeJSONB


class Base(DeclarativeBase):
    pass


class InstrumentRow(Base):
    __tablename__ = "instrument"
    __table_args__ = (
        CheckConstraint("version > 0", name="instrument_version_positive"),
        UniqueConstraint(
            "exchange_mic",
            "primary_symbol",
            "valid_from",
            name="uq_instrument_primary_identity",
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(32))
    primary_symbol: Mapped[str] = mapped_column(String(64))
    exchange_mic: Mapped[str] = mapped_column(String(4), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InstrumentAliasRow(Base):
    __tablename__ = "instrument_alias"
    __table_args__ = (
        CheckConstraint(
            "valid_to is null or valid_to > valid_from",
            name="instrument_alias_valid_window",
        ),
        UniqueConstraint(
            "provider",
            "symbol",
            "valid_from",
            name="uq_instrument_alias_provider_symbol_from",
        ),
    )

    alias_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("instrument.instrument_id", ondelete="RESTRICT"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(128))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradingCalendarVersionRow(Base):
    __tablename__ = "trading_calendar_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="calendar_effective_window",
        ),
        UniqueConstraint("mic", "version", name="uq_calendar_mic_version"),
    )

    calendar_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True
    )
    mic: Mapped[str] = mapped_column(String(4), index=True)
    version: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    definition: Mapped[dict[str, object]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArtifactManifestRow(Base):
    __tablename__ = "artifact_manifest"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="artifact_size_nonnegative"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="artifact_content_hash_format",
        ),
    )

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(127))
    license_tag: Mapped[str] = mapped_column(String(128))
    sensitivity: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(128))
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    storage_uri: Mapped[str] = mapped_column(Text)
    metadata_payload: Mapped[dict[str, object]] = mapped_column("metadata", JSONB)


class EvidenceItemRow(Base):
    __tablename__ = "evidence_item"
    __table_args__ = (
        CheckConstraint(
            "available_at <= observed_at",
            name="evidence_available_by_observed",
        ),
        CheckConstraint(
            "not strict_point_in_time or available_at <= as_of",
            name="evidence_available_by_as_of",
        ),
        CheckConstraint(
            "not strict_point_in_time or availability_certainty = 'proven'",
            name="evidence_strict_availability_proven",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="evidence_content_hash_format",
        ),
    )

    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    availability_certainty: Mapped[str] = mapped_column(
        String(16), server_default=text("'proven'")
    )
    strict_point_in_time: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )
    source: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    raw_artifact_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("artifact_manifest.content_hash", ondelete="RESTRICT"),
    )
    quality_state: Mapped[str] = mapped_column(String(32))
    quality: Mapped[dict[str, object]] = mapped_column(JSONB)
    sensitivity: Mapped[str] = mapped_column(String(32))
    license_tag: Mapped[str] = mapped_column(String(128))
    redistribution_tag: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transformation_version: Mapped[str | None] = mapped_column(String(128))
    untrusted_content: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EvidenceEdgeRow(Base):
    __tablename__ = "evidence_edge"
    __table_args__ = (
        PrimaryKeyConstraint("parent_evidence_id", "child_evidence_id", "relation"),
        CheckConstraint(
            "parent_evidence_id <> child_evidence_id",
            name="evidence_edge_not_self",
        ),
    )

    parent_evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_item.evidence_id", ondelete="RESTRICT"),
    )
    child_evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_item.evidence_id", ondelete="RESTRICT"),
    )
    relation: Mapped[str] = mapped_column(String(64))
    transformation_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DatasetSnapshotRow(Base):
    __tablename__ = "dataset_snapshot"
    __table_args__ = (
        CheckConstraint(
            "cutoff_at <= as_of",
            name="dataset_snapshot_cutoff_by_as_of",
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_policy_id: Mapped[str] = mapped_column(String(128))
    manifest_artifact_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("artifact_manifest.content_hash", ondelete="RESTRICT"),
    )
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DatasetSnapshotEvidenceRow(Base):
    __tablename__ = "dataset_snapshot_evidence"
    __table_args__ = (PrimaryKeyConstraint("snapshot_id", "evidence_id"),)

    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("dataset_snapshot.snapshot_id", ondelete="RESTRICT"),
    )
    evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_item.evidence_id", ondelete="RESTRICT"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkflowRunRow(Base):
    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint("version > 0", name="run_version_positive"),
        UniqueConstraint("idempotency_key", name="uq_run_idempotency_key"),
    )

    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    input_hash: Mapped[str] = mapped_column(String(64))
    owner_subject: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunDatasetSnapshotRow(Base):
    __tablename__ = "run_dataset_snapshot"

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("run.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("dataset_snapshot.snapshot_id", ondelete="RESTRICT"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunEventRow(Base):
    __tablename__ = "run_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        UniqueConstraint("event_hash", name="uq_run_event_hash"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("run.run_id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(SecretFreeJSONB())
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class JobRow(Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="job_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="job_max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="job_attempts_within_max"),
        CheckConstraint("attempt_generation >= 0", name="job_generation_nonnegative"),
        CheckConstraint(
            "deadline_at > not_before",
            name="job_deadline_after_not_before",
        ),
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        Index("ix_job_claim", "status", "not_before", "lease_until"),
    )

    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("run.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(SecretFreeJSONB())
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    attempt_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempt_nonce: Mapped[str | None] = mapped_column(String(128))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_artifact_hash: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("artifact_manifest.content_hash", ondelete="RESTRICT")
    )
    last_error: Mapped[dict[str, object] | None] = mapped_column(SecretFreeJSONB())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "sequence",
            name="uq_outbox_aggregate_sequence",
        ),
        Index("ix_outbox_delivery", "published_at", "not_before"),
        CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="outbox_max_attempts_positive"),
        CheckConstraint(
            "lease_generation >= 0",
            name="outbox_lease_generation_nonnegative",
        ),
    )

    outbox_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(128))
    sequence: Mapped[int] = mapped_column(Integer)
    topic: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(SecretFreeJSONB())
    idempotency_key: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("10"))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_nonce: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_error: Mapped[dict[str, object] | None] = mapped_column(SecretFreeJSONB())


class InboxRow(Base):
    __tablename__ = "inbox"
    __table_args__ = (PrimaryKeyConstraint("consumer", "message_id"),)

    consumer: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(256))
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class ProviderHealthRow(Base):
    __tablename__ = "provider_health"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "capability", "market"),
        CheckConstraint("failure_count >= 0", name="provider_failure_nonnegative"),
    )

    provider: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    failure_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    quota_remaining: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, object]] = mapped_column(JSONB)


class UsageBudgetRow(Base):
    __tablename__ = "usage_budget"
    __table_args__ = (
        CheckConstraint("period_end > period_start", name="budget_period_valid"),
        CheckConstraint("hard_limit >= 0", name="budget_limit_nonnegative"),
        CheckConstraint("used >= 0", name="budget_used_nonnegative"),
        CheckConstraint("version > 0", name="budget_version_positive"),
        UniqueConstraint(
            "scope",
            "period_start",
            "period_end",
            name="uq_budget_scope_period",
        ),
    )

    budget_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hard_limit: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    used: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    currency: Mapped[str | None] = mapped_column(String(3))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyRegistryRow(Base):
    __tablename__ = "strategy_registry"
    __table_args__ = (
        PrimaryKeyConstraint("strategy_id", "strategy_version"),
        UniqueConstraint("manifest_id", name="uq_strategy_registry_manifest_id"),
        UniqueConstraint("manifest_hash", name="uq_strategy_registry_manifest_hash"),
        CheckConstraint("version > 0", name="strategy_registry_version_positive"),
        CheckConstraint(
            "(evaluation_report_id is null) = (evaluation_hash is null)",
            name="strategy_registry_evaluation_binding",
        ),
        Index("ix_strategy_registry_state", "state"),
    )

    strategy_id: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str] = mapped_column(String(64))
    manifest_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    source_artifact_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("artifact_manifest.content_hash", ondelete="RESTRICT")
    )
    runtime_hash: Mapped[str] = mapped_column(String(64))
    feature_spec_hash: Mapped[str] = mapped_column(String(64))
    label_spec_hash: Mapped[str] = mapped_column(String(64))
    universe_spec_hash: Mapped[str] = mapped_column(String(64))
    cost_model_hash: Mapped[str] = mapped_column(String(64))
    split_policy_hash: Mapped[str] = mapped_column(String(64))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(128))
    deterministic: Mapped[bool] = mapped_column(Boolean)
    manifest_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))
    evaluation_report_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evaluation_hash: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyEvaluationReportRow(Base):
    __tablename__ = "strategy_evaluation_report"
    __table_args__ = (
        UniqueConstraint("evaluation_hash", name="uq_strategy_evaluation_hash"),
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.strategy_version"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_strategy_evaluation_identity",
            "strategy_id",
            "strategy_version",
            "created_at",
        ),
    )

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str] = mapped_column(String(64))
    strategy_manifest_hash: Mapped[str] = mapped_column(String(64))
    dataset_snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("dataset_snapshot.snapshot_id", ondelete="RESTRICT"),
    )
    data_hash: Mapped[str] = mapped_column(String(64))
    runtime_hash: Mapped[str] = mapped_column(String(64))
    evaluation_policy_hash: Mapped[str] = mapped_column(String(64))
    evaluation_hash: Mapped[str] = mapped_column(String(64))
    report_artifact_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("artifact_manifest.content_hash", ondelete="RESTRICT")
    )
    passed: Mapped[bool] = mapped_column(Boolean)
    calibration: Mapped[str] = mapped_column(String(32))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyAuditEventRow(Base):
    __tablename__ = "strategy_audit_event"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "sequence",
            name="uq_strategy_audit_sequence",
        ),
        UniqueConstraint("event_hash", name="uq_strategy_audit_hash"),
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.strategy_version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence > 0", name="strategy_audit_sequence_positive"),
        CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="strategy_audit_chain_shape",
        ),
        CheckConstraint(
            "(evaluation_report_id is null) = (evaluation_hash is null)",
            name="strategy_audit_evaluation_binding",
        ),
        Index(
            "ix_strategy_audit_identity",
            "strategy_id",
            "strategy_version",
            "sequence",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(72))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(128))
    evaluation_report_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    evaluation_hash: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
