"""Restrict queue updates to lease and transition columns.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12 03:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUEUE_MUTATORS = ("stonks_app", "stonks_worker")
QUEUE_UPDATE_COLUMNS = {
    "job": (
        "status",
        "not_before",
        "attempts",
        "attempt_generation",
        "attempt_nonce",
        "lease_owner",
        "lease_until",
        "result_artifact_hash",
        "last_error",
        "updated_at",
    ),
    "outbox": (
        "not_before",
        "published_at",
        "attempts",
        "lease_owner",
        "lease_until",
        "lease_generation",
        "lease_nonce",
        "last_error",
    ),
}


def upgrade() -> None:
    roles = ", ".join(QUEUE_MUTATORS)
    for table, columns in QUEUE_UPDATE_COLUMNS.items():
        op.execute(f"revoke update on {table} from {roles}")
        op.execute(f"grant update ({', '.join(columns)}) on {table} to {roles}")


def downgrade() -> None:
    roles = ", ".join(QUEUE_MUTATORS)
    for table, columns in QUEUE_UPDATE_COLUMNS.items():
        op.execute(f"revoke update ({', '.join(columns)}) on {table} from {roles}")
        op.execute(f"grant update on {table} to {roles}")
