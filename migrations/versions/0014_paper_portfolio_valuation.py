"""Add immutable ledger-bound paper portfolio valuations.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-14 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_portfolio_valuation",
        sa.Column("valuation_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column("ledger_hash", sa.String(64), nullable=True),
        sa.Column("ledger_projection_hash", sa.String(64), nullable=False),
        sa.Column("valuation_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "ledger_sequence >= 0",
            name="paper_portfolio_valuation_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "(ledger_sequence = 0 and ledger_hash is null) or "
            "(ledger_sequence > 0 and ledger_hash is not null)",
            name="paper_portfolio_valuation_ledger_shape",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("valuation_id"),
        sa.UniqueConstraint("valuation_hash", name="uq_paper_portfolio_valuation_hash"),
    )
    op.create_index(
        "ix_paper_portfolio_valuation_account_as_of",
        "paper_portfolio_valuation",
        ["account_id", "as_of"],
    )
    op.execute(
        "create trigger trg_paper_portfolio_valuation_append_only "
        "before update or delete on paper_portfolio_valuation for each row "
        "execute function reject_append_only_mutation()"
    )
    tables = "paper_portfolio_valuation"
    op.execute(
        f"revoke all on {tables} from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute(f"grant select on {tables} to stonks_reader")
    op.execute(f"grant select, insert on {tables} to stonks_app")


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_paper_portfolio_valuation_append_only "
        "on paper_portfolio_valuation"
    )
    op.drop_index(
        "ix_paper_portfolio_valuation_account_as_of",
        table_name="paper_portfolio_valuation",
    )
    op.drop_table("paper_portfolio_valuation")
