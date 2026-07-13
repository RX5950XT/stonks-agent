"""Add replay-derived paper ledger projections and fill accounting guards.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-13 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_opening_snapshot()
    _create_ledger_projection()
    _protect_fill_accounting()
    _protect_global_kill_switch()
    _grant_permissions()


def downgrade() -> None:
    op.execute(
        "drop trigger if exists trg_journal_requires_fill_accounting "
        "on journal_transaction"
    )
    op.execute(
        "drop trigger if exists trg_fill_requires_journal_accounting on paper_fill"
    )
    op.execute("drop function if exists require_fill_accounting()")
    op.drop_index("uq_paper_kill_switch_global", table_name="paper_kill_switch")
    op.execute(
        "drop trigger if exists trg_paper_ledger_projection_mutation "
        "on paper_ledger_account_projection"
    )
    op.execute("drop function if exists validate_paper_ledger_projection()")
    op.execute(
        "drop trigger if exists trg_paper_ledger_projection_no_delete "
        "on paper_ledger_account_projection"
    )
    op.execute(
        "drop trigger if exists trg_paper_opening_snapshot_append_only "
        "on paper_account_opening_snapshot"
    )
    op.drop_table("paper_ledger_account_projection")
    op.drop_table("paper_account_opening_snapshot")


def _create_opening_snapshot() -> None:
    op.create_table(
        "paper_account_opening_snapshot",
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("snapshot_id", name="uq_paper_account_opening_snapshot_id"),
        sa.UniqueConstraint(
            "snapshot_hash", name="uq_paper_account_opening_snapshot_hash"
        ),
    )
    op.execute(
        "create trigger trg_paper_opening_snapshot_append_only before update or delete "
        "on paper_account_opening_snapshot for each row execute function "
        "reject_append_only_mutation()"
    )


def _create_ledger_projection() -> None:
    op.create_table(
        "paper_ledger_account_projection",
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("ledger_account", sa.String(256), nullable=False),
        sa.Column("commodity", sa.String(128), nullable=False),
        sa.Column("quantum", sa.Numeric(), nullable=False),
        sa.Column("debit_total", sa.Numeric(), nullable=False),
        sa.Column("credit_total", sa.Numeric(), nullable=False),
        sa.Column("updated_ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quantum > 0 and debit_total >= 0 and credit_total >= 0",
            name="paper_ledger_projection_amounts_valid",
        ),
        sa.CheckConstraint(
            "mod(debit_total, quantum) = 0 and mod(credit_total, quantum) = 0",
            name="paper_ledger_projection_quantized",
        ),
        sa.CheckConstraint(
            "updated_ledger_sequence >= 0",
            name="paper_ledger_projection_sequence_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("account_id", "ledger_account", "commodity"),
    )
    op.execute(
        """
        create function validate_paper_ledger_projection()
        returns trigger language plpgsql as $$
        declare current_ledger_sequence bigint;
        begin
            select ledger_sequence into current_ledger_sequence from paper_account
            where account_id=new.account_id;
            if not found or new.updated_ledger_sequence>current_ledger_sequence then
                raise exception 'paper ledger projection sequence is invalid'
                    using errcode='40001';
            end if;
            if tg_op='UPDATE' then
                if new.account_id<>old.account_id
                   or new.ledger_account<>old.ledger_account
                   or new.commodity<>old.commodity or new.quantum<>old.quantum
                   or new.debit_total<old.debit_total
                   or new.credit_total<old.credit_total
                   or new.updated_ledger_sequence<=old.updated_ledger_sequence then
                    raise exception 'paper ledger projection mutation is invalid'
                        using errcode='40001';
                end if;
            end if;
            new.updated_at:=clock_timestamp(); return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_ledger_projection_mutation before insert or update "
        "on paper_ledger_account_projection for each row execute function "
        "validate_paper_ledger_projection()"
    )
    op.execute(
        "create trigger trg_paper_ledger_projection_no_delete before delete "
        "on paper_ledger_account_projection for each row execute function "
        "reject_append_only_mutation()"
    )


def _protect_fill_accounting() -> None:
    op.execute(
        """
        create function require_fill_accounting()
        returns trigger language plpgsql as $$
        declare target uuid; source_order uuid; account text; filled numeric;
                latest order_event%rowtype;
        begin
            target:=coalesce(
                (to_jsonb(new)->>'fill_id')::uuid,
                (to_jsonb(new)->>'source_fill_id')::uuid
            );
            select order_intent_id, account_id into source_order, account
            from paper_fill where fill_id=target;
            if not found or (select count(*) from journal_transaction
                             where source_fill_id=target)<>1
               or not exists(
                    select 1 from journal_transaction j
                    where j.source_fill_id=target
                      and j.source_order_intent_id=source_order
                      and j.account_id=account
               ) then
                raise exception 'paper fill requires exact journal accounting'
                    using errcode='23514';
            end if;
            select * into latest from order_event where order_intent_id=source_order
            order by sequence desc limit 1;
            select coalesce(sum(quantity), 0) into filled from paper_fill
            where order_intent_id=source_order;
            if latest.event_id is null
               or latest.to_status not in ('partially_filled','filled')
               or latest.cumulative_filled_quantity<>filled then
                raise exception 'paper fill order state is unknown'
                    using errcode='23514';
            end if;
            return null;
        end $$
        """
    )
    for trigger, table in (
        ("trg_fill_requires_journal_accounting", "paper_fill"),
        ("trg_journal_requires_fill_accounting", "journal_transaction"),
    ):
        op.execute(
            f"create constraint trigger {trigger} after insert on {table} "
            "deferrable initially deferred for each row execute function "
            "require_fill_accounting()"
        )


def _protect_global_kill_switch() -> None:
    op.create_index(
        "uq_paper_kill_switch_global",
        "paper_kill_switch",
        ["scope"],
        unique=True,
        postgresql_where=sa.text("scope='global'"),
    )
    op.execute(
        """
        insert into paper_kill_switch
            (switch_id, scope, account_id, active, reason_code, actor,
             version, created_at, updated_at)
        values
            ('46000000-0000-4000-8000-000000000000', 'global', null, false,
             'initialized', 'system:migration', 1, clock_timestamp(), clock_timestamp())
        on conflict do nothing
        """
    )


def _grant_permissions() -> None:
    tables = "paper_account_opening_snapshot, paper_ledger_account_projection"
    op.execute(
        f"revoke all on {tables} from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute(f"grant select on {tables} to stonks_reader")
    op.execute(f"grant select, insert on {tables} to stonks_app")
    op.execute(
        "grant update (debit_total, credit_total, updated_ledger_sequence, updated_at) "
        "on paper_ledger_account_projection to stonks_app"
    )
