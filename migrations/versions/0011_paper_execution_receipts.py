"""Add durable idempotent reference paper execution receipts.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-13 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_execution_receipt",
        sa.Column("receipt_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("order_intent_id", sa.UUID(), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("outcome_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_intent_id"], ["order_intent.intent_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_execution_idempotency"
        ),
        sa.UniqueConstraint("command_id", name="uq_paper_execution_command"),
        sa.UniqueConstraint("receipt_hash", name="uq_paper_execution_receipt_hash"),
        sa.UniqueConstraint("outcome_hash", name="uq_paper_execution_outcome_hash"),
    )
    op.create_index(
        "ix_paper_execution_account_time",
        "paper_execution_receipt",
        ["account_id", "created_at"],
    )
    op.execute(
        "create trigger trg_paper_execution_receipt_append_only "
        "before update or delete on paper_execution_receipt for each row "
        "execute function reject_append_only_mutation()"
    )
    op.execute(
        "revoke all on paper_execution_receipt "
        "from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute("grant select on paper_execution_receipt to stonks_reader")
    op.execute("grant select, insert on paper_execution_receipt to stonks_app")


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_paper_execution_receipt_append_only "
        "on paper_execution_receipt"
    )
    op.drop_index(
        "ix_paper_execution_account_time", table_name="paper_execution_receipt"
    )
    op.drop_table("paper_execution_receipt")
