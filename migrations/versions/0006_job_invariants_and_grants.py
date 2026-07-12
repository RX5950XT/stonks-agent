"""Require canonical job ownership/deadlines and reset role grants.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-11 15:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANONICAL_TABLES = (
    "instrument",
    "instrument_alias",
    "trading_calendar_version",
    "artifact_manifest",
    "evidence_item",
    "evidence_edge",
    "dataset_snapshot",
    "dataset_snapshot_evidence",
    "run",
    "run_dataset_snapshot",
    "run_event",
    "job",
    "outbox",
    "inbox",
    "provider_health",
    "usage_budget",
)
APP_MUTABLE_TABLES = (
    "instrument",
    "instrument_alias",
    "trading_calendar_version",
    "run",
    "job",
    "outbox",
    "provider_health",
    "usage_budget",
)
WORKER_APPEND_TABLES = (
    "artifact_manifest",
    "evidence_item",
    "evidence_edge",
    "dataset_snapshot",
    "dataset_snapshot_evidence",
    "run_dataset_snapshot",
    "run_event",
    "inbox",
)
WORKER_MUTABLE_TABLES = ("job", "outbox")
DATABASE_ROLES = ("stonks_reader", "stonks_app", "stonks_worker")


def upgrade() -> None:
    _require_existing_job_values()
    op.drop_constraint("job_deadline_after_not_before", "job", type_="check")
    op.alter_column("job", "run_id", nullable=False)
    op.alter_column("job", "deadline_at", nullable=False)
    op.create_check_constraint(
        "job_deadline_after_not_before",
        "job",
        "deadline_at > not_before",
    )
    _drop_upgrade_checks()
    _reset_least_privilege_grants()


def downgrade() -> None:
    op.drop_constraint("job_deadline_after_not_before", "job", type_="check")
    op.alter_column("job", "deadline_at", nullable=True)
    op.alter_column("job", "run_id", nullable=True)
    op.create_check_constraint(
        "job_deadline_after_not_before",
        "job",
        "deadline_at is null or deadline_at > not_before",
    )
    op.execute("grant usage on schema public to public")


def _require_existing_job_values() -> None:
    for name, expression in (
        ("job_run_id_required_upgrade", "run_id is not null"),
        ("job_deadline_at_required_upgrade", "deadline_at is not null"),
    ):
        op.execute(
            f"alter table job add constraint {name} check ({expression}) not valid"
        )
        op.execute(f"alter table job validate constraint {name}")


def _drop_upgrade_checks() -> None:
    op.drop_constraint("job_deadline_at_required_upgrade", "job", type_="check")
    op.drop_constraint("job_run_id_required_upgrade", "job", type_="check")


def _reset_least_privilege_grants() -> None:
    roles = ", ".join(DATABASE_ROLES)
    canonical = ", ".join(CANONICAL_TABLES)
    op.execute("revoke all on schema public from public")
    op.execute(f"revoke all on schema public from {roles}")
    op.execute("revoke all on all tables in schema public from public")
    op.execute(f"revoke all on all tables in schema public from {roles}")
    op.execute(f"grant usage on schema public to {roles}")
    op.execute(f"grant select on {canonical} to stonks_reader")
    op.execute(f"grant select, insert on {canonical} to stonks_app")
    op.execute(f"grant update on {', '.join(APP_MUTABLE_TABLES)} to stonks_app")
    op.execute(
        f"grant select, insert on {', '.join(WORKER_APPEND_TABLES)} to stonks_worker"
    )
    op.execute(
        "grant select, insert, update on "
        f"{', '.join(WORKER_MUTABLE_TABLES)} to stonks_worker"
    )
    op.execute("grant select, update on run to stonks_worker")
