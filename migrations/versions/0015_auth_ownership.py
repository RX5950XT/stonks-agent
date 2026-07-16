"""Persist immutable workflow ownership for object authorization.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-16 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("owner_subject", sa.String(255), nullable=True),
    )
    op.execute("update run set owner_subject = 'system:legacy'")
    op.alter_column("run", "owner_subject", nullable=False)
    op.create_index("ix_run_owner_subject", "run", ["owner_subject"])


def downgrade() -> None:
    op.drop_index("ix_run_owner_subject", table_name="run")
    op.drop_column("run", "owner_subject")
