"""Add bounded non-canonical trace context to durable transport rows.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-17 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("job", "outbox", "inbox")
_TRACE_COLUMNS = ("traceparent", "tracestate", "correlation_id")
_QUEUE_MUTATORS = ("stonks_app", "stonks_worker")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("traceparent", sa.String(55), nullable=True))
        op.add_column(table, sa.Column("tracestate", sa.String(512), nullable=True))
        op.add_column(table, sa.Column("correlation_id", sa.String(128), nullable=True))
        _create_constraints(table)
    _protect_trace_columns()


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"{table}_correlation_id_valid", table, type_="check")
        op.drop_constraint(f"{table}_tracestate_valid", table, type_="check")
        op.drop_constraint(f"{table}_traceparent_valid", table, type_="check")
        for column in reversed(_TRACE_COLUMNS):
            op.drop_column(table, column)


def _create_constraints(table: str) -> None:
    op.create_check_constraint(
        f"{table}_traceparent_valid",
        table,
        "traceparent is null or ("
        "traceparent ~ '^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$' "
        "and substring(traceparent from 4 for 32) <> repeat('0', 32) "
        "and substring(traceparent from 37 for 16) <> repeat('0', 16))",
    )
    op.create_check_constraint(
        f"{table}_tracestate_valid",
        table,
        "tracestate is null or (traceparent is not null "
        "and octet_length(tracestate) between 1 and 512 "
        "and tracestate !~ '[^ -~]')",
    )
    op.create_check_constraint(
        f"{table}_correlation_id_valid",
        table,
        "correlation_id is null or "
        "correlation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'",
    )


def _protect_trace_columns() -> None:
    columns = ", ".join(_TRACE_COLUMNS)
    roles = ", ".join(_QUEUE_MUTATORS)
    for table in _TABLES:
        op.execute(f"revoke update ({columns}) on {table} from {roles}")
