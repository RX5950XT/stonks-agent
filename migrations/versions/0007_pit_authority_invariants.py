"""Harden point-in-time links and linked-run authority.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-12 01:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_TRANSITION_COLUMNS = ("status", "version", "updated_at")
RUN_MUTATORS = ("stonks_app", "stonks_worker")


def upgrade() -> None:
    _harden_snapshot_evidence_validation()
    _serialize_run_snapshot_links()
    _protect_linked_run_authority()
    _restrict_run_update_grants()


def downgrade() -> None:
    _restore_run_update_grants()
    op.execute("drop trigger trg_run_linked_authority_immutable on run")
    op.execute("drop function reject_linked_run_authority_mutation()")
    _restore_run_snapshot_validation()
    _restore_snapshot_evidence_validation()


def _harden_snapshot_evidence_validation() -> None:
    op.execute(
        """
        create or replace function validate_snapshot_evidence_pit()
        returns trigger
        language plpgsql
        as $$
        declare
            snapshot_as_of timestamptz;
            evidence_available_at timestamptz;
            evidence_as_of timestamptz;
            evidence_certainty varchar(16);
            evidence_strict boolean;
        begin
            select as_of into strict snapshot_as_of
            from dataset_snapshot
            where snapshot_id = new.snapshot_id;

            select available_at, as_of, availability_certainty,
                   strict_point_in_time
            into strict evidence_available_at, evidence_as_of,
                        evidence_certainty, evidence_strict
            from evidence_item
            where evidence_id = new.evidence_id;

            if evidence_available_at > snapshot_as_of
               or evidence_as_of > snapshot_as_of
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


def _serialize_run_snapshot_links() -> None:
    op.execute(
        """
        create or replace function validate_run_snapshot_pit()
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
            where run_id = new.run_id
            for update;

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


def _protect_linked_run_authority() -> None:
    op.execute(
        """
        create function reject_linked_run_authority_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if exists (
                select 1 from run_dataset_snapshot where run_id = old.run_id
            ) and (
                new.run_id is distinct from old.run_id
                or new.run_type is distinct from old.run_type
                or new.as_of is distinct from old.as_of
                or new.policy_id is distinct from old.policy_id
                or new.idempotency_key is distinct from old.idempotency_key
                or new.input_hash is distinct from old.input_hash
                or new.created_at is distinct from old.created_at
            ) then
                raise exception 'linked run authority is immutable'
                    using errcode = '55000';
            end if;
            return new;
        end
        $$
        """
    )
    op.execute(
        """
        create trigger trg_run_linked_authority_immutable
        before update on run
        for each row execute function reject_linked_run_authority_mutation()
        """
    )


def _restrict_run_update_grants() -> None:
    roles = ", ".join(RUN_MUTATORS)
    columns = ", ".join(RUN_TRANSITION_COLUMNS)
    op.execute(f"revoke update on run from {roles}")
    op.execute(f"grant update ({columns}) on run to {roles}")


def _restore_run_update_grants() -> None:
    roles = ", ".join(RUN_MUTATORS)
    columns = ", ".join(RUN_TRANSITION_COLUMNS)
    op.execute(f"revoke update ({columns}) on run from {roles}")
    op.execute(f"grant update on run to {roles}")


def _restore_snapshot_evidence_validation() -> None:
    op.execute(
        """
        create or replace function validate_snapshot_evidence_pit()
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


def _restore_run_snapshot_validation() -> None:
    op.execute(
        """
        create or replace function validate_run_snapshot_pit()
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
