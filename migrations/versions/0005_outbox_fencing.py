"""outbox_fencing

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "lease_generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox",
        sa.Column("lease_nonce", sa.UUID(), nullable=True),
    )
    op.create_check_constraint(
        "outbox_lease_generation_nonnegative",
        "outbox",
        "lease_generation >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "outbox_lease_generation_nonnegative",
        "outbox",
        type_="check",
    )
    op.drop_column("outbox", "lease_nonce")
    op.drop_column("outbox", "lease_generation")
