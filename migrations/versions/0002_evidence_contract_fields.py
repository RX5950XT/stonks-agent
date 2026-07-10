"""evidence_contract_fields

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11 03:49:46.687640
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence_item",
        sa.Column(
            "quality",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        """
        update evidence_item
        set quality = jsonb_build_object(
            'schema_version', '1.0.0',
            'status', quality_state,
            'completeness', '1',
            'warnings', jsonb_build_array(),
            'fallback_chain', jsonb_build_array()
        )
        where quality is null
        """
    )
    op.alter_column("evidence_item", "quality", nullable=False)
    op.add_column(
        "evidence_item",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evidence_item",
        sa.Column("transformation_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "evidence_item",
        sa.Column(
            "untrusted_content",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("evidence_item", "untrusted_content")
    op.drop_column("evidence_item", "transformation_version")
    op.drop_column("evidence_item", "expires_at")
    op.drop_column("evidence_item", "quality")
