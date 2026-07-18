"""Add durable artifact maintenance audit chain.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_head()
    _create_events()
    _protect_chain()
    _grant_permissions()


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_artifact_maintenance_head_requires_event "
        "on artifact_maintenance_head"
    )
    op.execute("drop function if exists require_artifact_maintenance_head_event()")
    op.execute(
        "drop trigger if exists trg_artifact_maintenance_event_chain "
        "on artifact_maintenance_event"
    )
    op.execute("drop function if exists validate_artifact_maintenance_event_chain()")
    op.execute(
        "drop trigger if exists trg_artifact_maintenance_event_append_only "
        "on artifact_maintenance_event"
    )
    op.execute(
        "drop trigger if exists trg_artifact_maintenance_head_no_delete "
        "on artifact_maintenance_head"
    )
    op.execute(
        "drop trigger if exists trg_artifact_maintenance_head_mutation "
        "on artifact_maintenance_head"
    )
    op.execute("drop function if exists validate_artifact_maintenance_head()")
    op.drop_table("artifact_maintenance_event")
    op.drop_table("artifact_maintenance_head")


def _create_head() -> None:
    op.create_table(
        "artifact_maintenance_head",
        sa.Column("head_id", sa.SmallInteger(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_id = 1", name="artifact_maintenance_head_singleton"),
        sa.CheckConstraint("sequence >= 0", name="artifact_maintenance_head_sequence"),
        sa.CheckConstraint(
            "(sequence = 0 and event_hash is null) or "
            "(sequence > 0 and event_hash is not null)",
            name="artifact_maintenance_head_hash_shape",
        ),
        sa.PrimaryKeyConstraint("head_id"),
    )
    op.execute(
        "insert into artifact_maintenance_head "
        "(head_id, sequence, event_hash, created_at, updated_at) "
        "values (1, 0, null, clock_timestamp(), clock_timestamp())"
    )


def _create_events() -> None:
    op.create_table(
        "artifact_maintenance_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(128), nullable=True),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="artifact_maintenance_event_sequence"),
        sa.CheckConstraint(
            "action in ('extend_retention', 'enable_legal_hold', "
            "'collect_orphans', 'restore')",
            name="artifact_maintenance_event_action",
        ),
        sa.CheckConstraint(
            "phase in ('requested', 'completed', 'failed')",
            name="artifact_maintenance_event_phase",
        ),
        sa.CheckConstraint(
            "(phase = 'requested' and outcome is null) or "
            "(phase in ('completed', 'failed') and outcome is not null)",
            name="artifact_maintenance_event_outcome_shape",
        ),
        sa.CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$' and "
            "((phase = 'requested' and result_hash is null) or "
            "(phase in ('completed', 'failed') and "
            "result_hash ~ '^[0-9a-f]{64}$'))",
            name="artifact_maintenance_event_binding_shape",
        ),
        sa.CheckConstraint(
            "(action = 'collect_orphans' and content_hash is null) or "
            "(action <> 'collect_orphans' and "
            "content_hash ~ '^[0-9a-f]{64}$')",
            name="artifact_maintenance_event_target_shape",
        ),
        sa.CheckConstraint(
            "(sequence = 1 and previous_event_hash is null) or "
            "(sequence > 1 and previous_event_hash is not null)",
            name="artifact_maintenance_event_chain_shape",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("sequence", name="uq_artifact_maintenance_event_sequence"),
        sa.UniqueConstraint("event_hash", name="uq_artifact_maintenance_event_hash"),
    )
    op.create_index(
        "uq_artifact_maintenance_requested_operation",
        "artifact_maintenance_event",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("phase = 'requested'"),
    )
    op.create_index(
        "uq_artifact_maintenance_terminal_operation",
        "artifact_maintenance_event",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("phase in ('completed', 'failed')"),
    )


def _protect_chain() -> None:
    op.execute(
        """
        create function validate_artifact_maintenance_head()
        returns trigger language plpgsql as $$ begin
            if new.head_id <> old.head_id or new.created_at <> old.created_at
               or new.sequence <> old.sequence + 1 or new.event_hash is null then
                raise exception 'artifact maintenance head CAS failed'
                    using errcode='40001';
            end if;
            new.updated_at := clock_timestamp();
            return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_artifact_maintenance_head_mutation before update "
        "on artifact_maintenance_head for each row execute function "
        "validate_artifact_maintenance_head()"
    )
    op.execute(
        "create trigger trg_artifact_maintenance_head_no_delete before delete "
        "on artifact_maintenance_head for each row execute function "
        "reject_append_only_mutation()"
    )
    op.execute(
        "create trigger trg_artifact_maintenance_event_append_only "
        "before update or delete on artifact_maintenance_event for each row "
        "execute function reject_append_only_mutation()"
    )
    op.execute(
        """
        create function validate_artifact_maintenance_event_chain()
        returns trigger language plpgsql as $$ declare prior text; begin
            if new.sequence = 1 then
                if new.previous_event_hash is not null then
                    raise exception 'artifact maintenance genesis hash is invalid'
                        using errcode='23514';
                end if;
            else
                select event_hash into prior from artifact_maintenance_event
                 where sequence = new.sequence - 1;
                if prior is null or new.previous_event_hash <> prior then
                    raise exception 'artifact maintenance event chain is invalid'
                        using errcode='40001';
                end if;
            end if;
            return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_artifact_maintenance_event_chain before insert "
        "on artifact_maintenance_event for each row execute function "
        "validate_artifact_maintenance_event_chain()"
    )
    op.execute(
        """
        create function require_artifact_maintenance_head_event()
        returns trigger language plpgsql as $$ begin
            if not exists (
                select 1 from artifact_maintenance_event
                 where sequence = new.sequence and event_hash = new.event_hash
            ) then
                raise exception 'artifact maintenance head has no event'
                    using errcode='23514';
            end if;
            return null;
        end $$
        """
    )
    op.execute(
        "create constraint trigger trg_artifact_maintenance_head_requires_event "
        "after update on artifact_maintenance_head deferrable initially deferred "
        "for each row execute function require_artifact_maintenance_head_event()"
    )


def _grant_permissions() -> None:
    tables = "artifact_maintenance_head, artifact_maintenance_event"
    op.execute(
        f"revoke all on {tables} from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute(f"grant select on {tables} to stonks_reader")
    op.execute(f"grant select, insert on {tables} to stonks_app")
    op.execute(
        "grant update (sequence, event_hash, updated_at) "
        "on artifact_maintenance_head to stonks_app"
    )
