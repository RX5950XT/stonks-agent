"""Add immutable strategy registry, evaluation reports, and audit chain.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-13 04:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRATEGY_TABLES = (
    "strategy_registry",
    "strategy_evaluation_report",
    "strategy_audit_event",
)


def upgrade() -> None:
    _create_registry()
    _create_evaluations()
    _create_audit_events()
    _protect_strategy_state()
    _grant_strategy_privileges()


def downgrade() -> None:
    op.execute("drop trigger trg_strategy_mutation_has_audit on strategy_registry")
    op.execute("drop trigger trg_strategy_registry_immutable on strategy_registry")
    op.execute("drop trigger trg_strategy_audit_chain on strategy_audit_event")
    op.execute("drop function require_strategy_audit_event()")
    op.execute("drop function validate_strategy_audit_chain()")
    op.execute("drop function validate_strategy_registry_mutation()")
    op.drop_table("strategy_audit_event")
    op.drop_table("strategy_evaluation_report")
    op.drop_table("strategy_registry")


def _create_registry() -> None:
    op.create_table(
        "strategy_registry",
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("manifest_id", sa.UUID(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("label_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("universe_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("cost_model_hash", sa.String(length=64), nullable=False),
        sa.Column("split_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("deterministic", sa.Boolean(), nullable=False),
        sa.Column("manifest_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("evaluation_report_id", sa.UUID(), nullable=True),
        sa.Column("evaluation_hash", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="strategy_registry_version_positive"),
        sa.CheckConstraint(
            "(evaluation_report_id is null) = (evaluation_hash is null)",
            name="strategy_registry_evaluation_binding",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_hash"],
            ["artifact_manifest.content_hash"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("strategy_id", "strategy_version"),
        sa.UniqueConstraint("manifest_id", name="uq_strategy_registry_manifest_id"),
        sa.UniqueConstraint("manifest_hash", name="uq_strategy_registry_manifest_hash"),
    )
    op.create_index("ix_strategy_registry_state", "strategy_registry", ["state"])


def _create_evaluations() -> None:
    op.create_table(
        "strategy_evaluation_report",
        sa.Column("report_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("strategy_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("data_hash", sa.String(length=64), nullable=False),
        sa.Column("runtime_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluation_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluation_hash", sa.String(length=64), nullable=False),
        sa.Column("report_artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("calibration", sa.String(length=32), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.strategy_version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["dataset_snapshot.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_artifact_hash"],
            ["artifact_manifest.content_hash"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("report_id"),
        sa.UniqueConstraint("evaluation_hash", name="uq_strategy_evaluation_hash"),
    )
    op.create_index(
        "ix_strategy_evaluation_identity",
        "strategy_evaluation_report",
        ["strategy_id", "strategy_version", "created_at"],
    )


def _create_audit_events() -> None:
    op.create_table(
        "strategy_audit_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=72), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("evaluation_report_id", sa.UUID(), nullable=True),
        sa.Column("evaluation_hash", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("sequence > 0", name="strategy_audit_sequence_positive"),
        sa.CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="strategy_audit_chain_shape",
        ),
        sa.CheckConstraint(
            "(evaluation_report_id is null) = (evaluation_hash is null)",
            name="strategy_audit_evaluation_binding",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.strategy_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "sequence",
            name="uq_strategy_audit_sequence",
        ),
        sa.UniqueConstraint("event_hash", name="uq_strategy_audit_hash"),
    )
    op.create_index(
        "ix_strategy_audit_identity",
        "strategy_audit_event",
        ["strategy_id", "strategy_version", "sequence"],
    )


def _protect_strategy_state() -> None:
    op.execute(
        """
        create function validate_strategy_registry_mutation()
        returns trigger language plpgsql as $$
        declare
            evaluation strategy_evaluation_report%rowtype;
        begin
            if tg_op = 'INSERT' then
                if new.state <> 'draft' or new.version <> 1
                   or new.evaluation_report_id is not null
                   or new.evaluation_hash is not null then
                    raise exception 'strategy registration must start in draft'
                        using errcode = '23514';
                end if;
                new.created_at := clock_timestamp();
                new.updated_at := new.created_at;
                return new;
            end if;

            if new.strategy_id is distinct from old.strategy_id
               or new.strategy_version is distinct from old.strategy_version
               or new.manifest_id is distinct from old.manifest_id
               or new.manifest_hash is distinct from old.manifest_hash
               or new.kind is distinct from old.kind
               or new.source_artifact_hash is distinct from old.source_artifact_hash
               or new.runtime_hash is distinct from old.runtime_hash
               or new.feature_spec_hash is distinct from old.feature_spec_hash
               or new.label_spec_hash is distinct from old.label_spec_hash
               or new.universe_spec_hash is distinct from old.universe_spec_hash
               or new.cost_model_hash is distinct from old.cost_model_hash
               or new.split_policy_hash is distinct from old.split_policy_hash
               or new.parameters_hash is distinct from old.parameters_hash
               or new.owner is distinct from old.owner
               or new.deterministic is distinct from old.deterministic
               or new.manifest_created_at is distinct from old.manifest_created_at
               or new.created_at is distinct from old.created_at then
                raise exception 'strategy identity and hashes are immutable'
                    using errcode = '55000';
            end if;
            if new.version <> old.version + 1 then
                raise exception 'strategy version must increment exactly once'
                    using errcode = '40001';
            end if;
            if not (
                (old.state = 'draft' and new.state = 'evaluating')
                or (old.state = 'evaluating' and new.state in ('rejected', 'shadow'))
                or (old.state = 'shadow' and new.state = 'paper_eligible')
                or (old.state = 'paper_eligible' and new.state in ('suspended', 'retired'))
                or (old.state = 'suspended' and new.state in ('evaluating', 'retired'))
            ) then
                raise exception 'strategy promotion transition is not allowed'
                    using errcode = '23514';
            end if;
            if (new.evaluation_report_id is null) <> (new.evaluation_hash is null) then
                raise exception 'strategy evaluation binding is incomplete'
                    using errcode = '23514';
            end if;
            if new.state in ('rejected', 'shadow', 'paper_eligible', 'suspended', 'retired')
               and new.evaluation_report_id is null then
                raise exception 'strategy state requires evaluation binding'
                    using errcode = '23514';
            end if;
            if new.evaluation_report_id is not null then
                select * into evaluation
                from strategy_evaluation_report
                where report_id = new.evaluation_report_id;
                if not found
                   or evaluation.evaluation_hash <> new.evaluation_hash
                   or evaluation.strategy_id <> new.strategy_id
                   or evaluation.strategy_version <> new.strategy_version
                   or evaluation.strategy_manifest_hash <> new.manifest_hash
                   or evaluation.runtime_hash <> new.runtime_hash then
                    raise exception 'strategy evaluation binding is invalid'
                        using errcode = '23514';
                end if;
                if new.state in ('shadow', 'paper_eligible')
                   and (evaluation.passed is not true
                        or evaluation.valid_until <= clock_timestamp()) then
                    raise exception 'strategy promotion requires valid passed evaluation'
                        using errcode = '23514';
                end if;
            end if;
            new.updated_at := clock_timestamp();
            return new;
        end
        $$
        """
    )
    op.execute(
        """
        create trigger trg_strategy_registry_immutable
        before insert or update on strategy_registry
        for each row execute function validate_strategy_registry_mutation()
        """
    )
    op.execute(
        """
        create trigger trg_strategy_registry_no_delete
        before delete on strategy_registry
        for each row execute function reject_append_only_mutation()
        """
    )
    for table in ("strategy_evaluation_report", "strategy_audit_event"):
        op.execute(
            f"""
            create trigger trg_{table}_append_only
            before update or delete on {table}
            for each row execute function reject_append_only_mutation()
            """
        )
    _protect_audit_chain()


def _protect_audit_chain() -> None:
    op.execute(
        """
        create function validate_strategy_audit_chain()
        returns trigger language plpgsql as $$
        declare
            prior strategy_audit_event%rowtype;
        begin
            select * into prior
            from strategy_audit_event
            where strategy_id = new.strategy_id
              and strategy_version = new.strategy_version
            order by sequence desc limit 1 for update;
            if new.sequence = 1 then
                if found or new.previous_hash is not null then
                    raise exception 'strategy genesis audit event is invalid'
                        using errcode = '23514';
                end if;
            elsif not found or new.sequence <> prior.sequence + 1
                  or new.previous_hash <> prior.event_hash then
                raise exception 'strategy audit hash chain is invalid'
                    using errcode = '23514';
            end if;
            return new;
        end
        $$
        """
    )
    op.execute(
        """
        create trigger trg_strategy_audit_chain
        before insert on strategy_audit_event
        for each row execute function validate_strategy_audit_chain()
        """
    )
    op.execute(
        """
        create function require_strategy_audit_event()
        returns trigger language plpgsql as $$
        declare
            expected_from varchar(32);
        begin
            expected_from := case when tg_op = 'INSERT' then null else old.state end;
            if not exists (
                select 1 from strategy_audit_event event
                where event.strategy_id = new.strategy_id
                  and event.strategy_version = new.strategy_version
                  and event.sequence = new.version
                  and event.from_state is not distinct from expected_from
                  and event.to_state = new.state
                  and event.evaluation_report_id is not distinct from new.evaluation_report_id
                  and event.evaluation_hash is not distinct from new.evaluation_hash
                  and event.occurred_at = new.updated_at
            ) then
                raise exception 'strategy mutation requires matching immutable audit event'
                    using errcode = '23514';
            end if;
            return null;
        end
        $$
        """
    )
    op.execute(
        """
        create constraint trigger trg_strategy_mutation_has_audit
        after insert or update on strategy_registry
        deferrable initially deferred
        for each row execute function require_strategy_audit_event()
        """
    )


def _grant_strategy_privileges() -> None:
    for table in _STRATEGY_TABLES:
        op.execute(
            f"revoke all on {table} from public, stonks_app, stonks_worker, stonks_reader"
        )
        op.execute(f"grant select on {table} to stonks_reader")
        op.execute(f"grant select, insert on {table} to stonks_app")
    op.execute(
        "grant update (state, evaluation_report_id, evaluation_hash, version, updated_at) "
        "on strategy_registry to stonks_app"
    )
