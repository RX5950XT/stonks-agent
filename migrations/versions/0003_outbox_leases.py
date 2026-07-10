"""outbox_leases

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11 03:57:40.179340
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("10"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "outbox",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "outbox_attempts_nonnegative",
        "outbox",
        "attempts >= 0",
    )
    op.create_check_constraint(
        "outbox_max_attempts_positive",
        "outbox",
        "max_attempts > 0",
    )


def downgrade() -> None:
    op.drop_constraint("outbox_max_attempts_positive", "outbox", type_="check")
    op.drop_constraint("outbox_attempts_nonnegative", "outbox", type_="check")
    op.drop_column("outbox", "lease_until")
    op.drop_column("outbox", "lease_owner")
    op.drop_column("outbox", "max_attempts")
