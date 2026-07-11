"""pit_snapshot_references

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11 08:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "dataset_snapshot_cutoff_by_as_of",
        "dataset_snapshot",
        "cutoff_at <= as_of",
    )
    op.create_table(
        "dataset_snapshot_evidence",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["dataset_snapshot.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_item.evidence_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "evidence_id"),
    )
    op.create_index(
        "ix_dataset_snapshot_evidence_evidence_id",
        "dataset_snapshot_evidence",
        ["evidence_id"],
    )
    op.create_table(
        "run_dataset_snapshot",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["dataset_snapshot.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_run_dataset_snapshot_snapshot_id",
        "run_dataset_snapshot",
        ["snapshot_id"],
    )
    _create_pit_triggers()
    _protect_append_only_links()
    _grant_snapshot_completion_privileges()


def downgrade() -> None:
    op.drop_index(
        "ix_run_dataset_snapshot_snapshot_id",
        table_name="run_dataset_snapshot",
    )
    op.drop_table("run_dataset_snapshot")
    op.drop_index(
        "ix_dataset_snapshot_evidence_evidence_id",
        table_name="dataset_snapshot_evidence",
    )
    op.drop_table("dataset_snapshot_evidence")
    op.execute("drop function if exists validate_run_snapshot_pit()")
    op.execute("drop function if exists validate_snapshot_evidence_pit()")
    op.drop_constraint(
        "dataset_snapshot_cutoff_by_as_of",
        "dataset_snapshot",
        type_="check",
    )


def _create_pit_triggers() -> None:
    op.execute(
        """
        create function validate_snapshot_evidence_pit()
        returns trigger
        language plpgsql
        as $$
        declare
            snapshot_as_of timestamptz;
            evidence_available_at timestamptz;
            evidence_certainty varchar(16);
            evidence_strict boolean;
        begin
            select as_of into strict snapshot_as_of
            from dataset_snapshot
            where snapshot_id = new.snapshot_id;

            select available_at, availability_certainty, strict_point_in_time
            into strict evidence_available_at, evidence_certainty, evidence_strict
            from evidence_item
            where evidence_id = new.evidence_id;

            if evidence_available_at > snapshot_as_of
               or evidence_certainty <> 'proven'
               or evidence_strict is not true then
                raise exception 'snapshot cannot reference future evidence'
                    using errcode = '23514';
            end if;
            return new;
        end
        $$
        """
    )
    op.execute(
        """
        create trigger trg_dataset_snapshot_evidence_pit
        before insert on dataset_snapshot_evidence
        for each row execute function validate_snapshot_evidence_pit()
        """
    )
    op.execute(
        """
        create function validate_run_snapshot_pit()
        returns trigger
        language plpgsql
        as $$
        declare
            run_as_of timestamptz;
            snapshot_as_of timestamptz;
            snapshot_cutoff timestamptz;
        begin
            select as_of into strict run_as_of
            from run
            where run_id = new.run_id;

            select as_of, cutoff_at into strict snapshot_as_of, snapshot_cutoff
            from dataset_snapshot
            where snapshot_id = new.snapshot_id;

            if snapshot_as_of > run_as_of or snapshot_cutoff > run_as_of then
                raise exception 'run cannot reference a future snapshot'
                    using errcode = '23514';
            end if;
            return new;
        end
        $$
        """
    )
    op.execute(
        """
        create trigger trg_run_dataset_snapshot_pit
        before insert on run_dataset_snapshot
        for each row execute function validate_run_snapshot_pit()
        """
    )


def _protect_append_only_links() -> None:
    for table in ("dataset_snapshot_evidence", "run_dataset_snapshot"):
        op.execute(
            f"""
            create trigger trg_{table}_append_only
            before update or delete on {table}
            for each row execute function reject_append_only_mutation()
            """
        )


def _grant_snapshot_completion_privileges() -> None:
    op.execute(
        """
        grant select on dataset_snapshot_evidence, run_dataset_snapshot
        to stonks_reader, stonks_app, stonks_worker
        """
    )
    op.execute(
        """
        grant insert on dataset_snapshot_evidence, run_dataset_snapshot
        to stonks_app, stonks_worker
        """
    )
    op.execute(
        """
        grant select, insert on evidence_item, evidence_edge, dataset_snapshot
        to stonks_worker
        """
    )
    op.execute("grant select, update on run to stonks_worker")
