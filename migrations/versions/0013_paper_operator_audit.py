"""Add immutable paper operator audit chain.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-13 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_audit_head()
    _create_operator_actions()
    _protect_audit_chain()
    _grant_permissions()


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_paper_operator_head_requires_action "
        "on paper_operator_audit_head"
    )
    op.execute("drop function if exists require_paper_operator_head_action()")
    op.execute(
        "drop trigger if exists trg_paper_operator_action_chain "
        "on paper_operator_action"
    )
    op.execute("drop function if exists validate_paper_operator_action_chain()")
    op.execute(
        "drop trigger if exists trg_paper_operator_action_append_only "
        "on paper_operator_action"
    )
    op.execute(
        "drop trigger if exists trg_paper_operator_head_no_delete "
        "on paper_operator_audit_head"
    )
    op.execute(
        "drop trigger if exists trg_paper_operator_head_mutation "
        "on paper_operator_audit_head"
    )
    op.execute("drop function if exists validate_paper_operator_audit_head()")
    op.drop_table("paper_operator_action")
    op.drop_table("paper_operator_audit_head")


def _create_audit_head() -> None:
    op.create_table(
        "paper_operator_audit_head",
        sa.Column("head_id", sa.SmallInteger(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_id = 1", name="paper_operator_head_singleton"),
        sa.CheckConstraint("sequence >= 0", name="paper_operator_head_sequence"),
        sa.CheckConstraint(
            "(sequence = 0 and action_hash is null) or "
            "(sequence > 0 and action_hash is not null)",
            name="paper_operator_head_hash_shape",
        ),
        sa.PrimaryKeyConstraint("head_id"),
    )
    op.execute(
        "insert into paper_operator_audit_head "
        "(head_id, sequence, action_hash, created_at, updated_at) "
        "values (1, 0, null, clock_timestamp(), clock_timestamp())"
    )


def _create_operator_actions() -> None:
    op.create_table(
        "paper_operator_action",
        sa.Column("action_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("switch_version", sa.Integer(), nullable=False),
        sa.Column("previous_action_hash", sa.String(64), nullable=True),
        sa.Column("action_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="paper_operator_action_sequence"),
        sa.CheckConstraint("switch_version > 0", name="paper_operator_switch_version"),
        sa.CheckConstraint(
            "(scope = 'global' and account_id is null) or "
            "(scope = 'account' and account_id is not null)",
            name="paper_operator_action_scope_shape",
        ),
        sa.CheckConstraint(
            "(sequence = 1 and previous_action_hash is null) or "
            "(sequence > 1 and previous_action_hash is not null)",
            name="paper_operator_action_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint("sequence", name="uq_paper_operator_action_sequence"),
        sa.UniqueConstraint("action_hash", name="uq_paper_operator_action_hash"),
    )


def _protect_audit_chain() -> None:
    op.execute(
        """
        create function validate_paper_operator_audit_head()
        returns trigger language plpgsql as $$ begin
            if new.head_id <> old.head_id or new.created_at <> old.created_at
               or new.sequence <> old.sequence + 1 or new.action_hash is null then
                raise exception 'paper operator audit head CAS failed' using errcode='40001';
            end if;
            new.updated_at := clock_timestamp(); return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_operator_head_mutation before update "
        "on paper_operator_audit_head for each row execute function "
        "validate_paper_operator_audit_head()"
    )
    op.execute(
        "create trigger trg_paper_operator_head_no_delete before delete "
        "on paper_operator_audit_head for each row execute function "
        "reject_append_only_mutation()"
    )
    op.execute(
        "create trigger trg_paper_operator_action_append_only before update or delete "
        "on paper_operator_action for each row execute function "
        "reject_append_only_mutation()"
    )
    op.execute(
        """
        create function validate_paper_operator_action_chain()
        returns trigger language plpgsql as $$ declare prior text; begin
            if new.sequence = 1 then
                if new.previous_action_hash is not null then
                    raise exception 'paper operator genesis hash is invalid' using errcode='23514';
                end if;
            else
                select action_hash into prior from paper_operator_action
                 where sequence = new.sequence - 1;
                if prior is null or new.previous_action_hash <> prior then
                    raise exception 'paper operator action chain is invalid' using errcode='40001';
                end if;
            end if;
            return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_operator_action_chain before insert "
        "on paper_operator_action for each row execute function "
        "validate_paper_operator_action_chain()"
    )
    op.execute(
        """
        create function require_paper_operator_head_action()
        returns trigger language plpgsql as $$ begin
            if not exists (
                select 1 from paper_operator_action
                 where sequence = new.sequence and action_hash = new.action_hash
            ) then
                raise exception 'paper operator audit head has no action' using errcode='23514';
            end if;
            return null;
        end $$
        """
    )
    op.execute(
        "create constraint trigger trg_paper_operator_head_requires_action "
        "after update on paper_operator_audit_head deferrable initially deferred "
        "for each row execute function require_paper_operator_head_action()"
    )


def _grant_permissions() -> None:
    tables = "paper_operator_audit_head, paper_operator_action"
    op.execute(
        f"revoke all on {tables} from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute(f"grant select on {tables} to stonks_reader")
    op.execute(f"grant select, insert on {tables} to stonks_app")
    op.execute(
        "grant update (sequence, action_hash, updated_at) "
        "on paper_operator_audit_head to stonks_app"
    )
