"""Add append-only stale worker result quarantine audit.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_late_result_audit",
        sa.Column("audit_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column("result_artifact_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_generation > 0",
            name="worker_late_result_generation_positive",
        ),
        sa.CheckConstraint(
            "result_artifact_hash ~ '^[0-9a-f]{64}$' "
            "and record_hash ~ '^[0-9a-f]{64}$'",
            name="worker_late_result_hash_format",
        ),
        sa.CheckConstraint(
            "reason ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="worker_late_result_reason_format",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.UniqueConstraint("record_hash", name="uq_worker_late_result_record_hash"),
        sa.UniqueConstraint(
            "job_id",
            "attempt_generation",
            "result_artifact_hash",
            "reason",
            name="uq_worker_late_result_identity",
        ),
    )
    op.create_index(
        "ix_worker_late_result_run_observed",
        "worker_late_result_audit",
        ["run_id", "observed_at"],
    )
    op.execute(
        "create trigger trg_worker_late_result_append_only "
        "before update or delete on worker_late_result_audit for each row "
        "execute function reject_append_only_mutation()"
    )
    op.execute(
        "revoke all on worker_late_result_audit "
        "from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute("grant select on worker_late_result_audit to stonks_reader")
    op.execute("grant select, insert on worker_late_result_audit to stonks_app")


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_worker_late_result_append_only "
        "on worker_late_result_audit"
    )
    op.drop_index(
        "ix_worker_late_result_run_observed",
        table_name="worker_late_result_audit",
    )
    op.drop_table("worker_late_result_audit")
