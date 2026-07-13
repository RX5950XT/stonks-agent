"""Add canonical paper account, order, reservation, fill, and journal state.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-13 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.paper_trading_0010_guards import (
    TRADING_TABLES,
    grant_trading_privileges,
    protect_trading_state,
)

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_account_tables()
    _create_decision_tables()
    _create_execution_tables()
    _create_journal_tables()
    _create_kill_switch()
    protect_trading_state()
    grant_trading_privileges()


def downgrade() -> None:
    for trigger, table in (
        ("trg_journal_posting_balanced", "journal_posting"),
        ("trg_journal_transaction_balanced", "journal_transaction"),
        ("trg_journal_transaction_chain", "journal_transaction"),
        ("trg_order_event_chain", "order_event"),
        ("trg_reservation_mutation_has_event", "account_reservation"),
        ("trg_reservation_event_chain", "reservation_event"),
        ("trg_account_reservation_projection", "account_reservation"),
        ("trg_paper_position_projection", "paper_position_projection"),
        ("trg_paper_cash_projection", "paper_cash_projection"),
        ("trg_paper_account_mutation_has_event", "paper_account"),
        ("trg_paper_account_event_chain", "paper_account_event"),
        ("trg_paper_account_mutation", "paper_account"),
        ("trg_paper_kill_switch_mutation", "paper_kill_switch"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    for function in (
        "require_balanced_journal",
        "validate_journal_transaction_chain",
        "validate_order_event_chain",
        "require_reservation_event",
        "validate_reservation_event_chain",
        "validate_account_reservation_projection",
        "validate_paper_position_projection",
        "validate_paper_cash_projection",
        "require_paper_account_event",
        "validate_paper_account_event_chain",
        "validate_paper_account_mutation",
        "validate_paper_kill_switch_mutation",
    ):
        op.execute(f"drop function if exists {function}()")
    op.drop_constraint(
        "fk_account_reservation_order_intent",
        "account_reservation",
        type_="foreignkey",
    )
    for table in reversed(TRADING_TABLES):
        op.drop_table(table)


def _create_account_tables() -> None:
    op.create_table(
        "paper_account",
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column(
            "aggregate_sequence", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "portfolio_sequence", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "ledger_sequence", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("ledger_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "aggregate_sequence >= 0 and portfolio_sequence >= 0 and ledger_sequence >= 0",
            name="paper_account_sequences_nonnegative",
        ),
        sa.CheckConstraint(
            "(ledger_sequence = 0 and ledger_hash is null) or "
            "(ledger_sequence > 0 and ledger_hash is not null)",
            name="paper_account_ledger_head_shape",
        ),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "paper_account_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("aggregate_ref_type", sa.String(64), nullable=False),
        sa.Column("aggregate_ref_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "sequence > 0", name="paper_account_event_sequence_positive"
        ),
        sa.CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="paper_account_event_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "account_id", "sequence", name="uq_paper_account_event_sequence"
        ),
        sa.UniqueConstraint("event_hash", name="uq_paper_account_event_hash"),
    )
    op.create_index(
        "ix_paper_account_event_identity",
        "paper_account_event",
        ["account_id", "sequence"],
    )
    op.create_table(
        "paper_cash_projection",
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("settled_amount", sa.Numeric(), nullable=False),
        sa.Column("reserved_amount", sa.Numeric(), nullable=False),
        sa.Column("quantum", sa.Numeric(), nullable=False),
        sa.Column("updated_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "settled_amount >= 0 and reserved_amount >= 0 "
            "and reserved_amount <= settled_amount and quantum > 0",
            name="paper_cash_projection_amounts_valid",
        ),
        sa.CheckConstraint(
            "mod(settled_amount, quantum) = 0 and mod(reserved_amount, quantum) = 0",
            name="paper_cash_projection_quantized",
        ),
        sa.CheckConstraint(
            "updated_sequence >= 0", name="paper_cash_projection_sequence_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("account_id", "currency"),
    )
    op.create_table(
        "paper_position_projection",
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("sellable_quantity", sa.Numeric(), nullable=False),
        sa.Column("reserved_quantity", sa.Numeric(), nullable=False),
        sa.Column("quantum", sa.Numeric(), nullable=False),
        sa.Column("updated_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quantity >= 0 and sellable_quantity >= 0 and reserved_quantity >= 0 "
            "and sellable_quantity <= quantity and reserved_quantity <= sellable_quantity "
            "and quantum > 0",
            name="paper_position_projection_amounts_valid",
        ),
        sa.CheckConstraint(
            "mod(quantity, quantum) = 0 and mod(sellable_quantity, quantum) = 0 "
            "and mod(reserved_quantity, quantum) = 0",
            name="paper_position_projection_quantized",
        ),
        sa.CheckConstraint(
            "updated_sequence >= 0",
            name="paper_position_projection_sequence_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("account_id", "instrument_id"),
    )


def _create_decision_tables() -> None:
    op.create_table(
        "portfolio_target",
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("portfolio_snapshot_id", sa.UUID(), nullable=False),
        sa.Column("account_aggregate_sequence", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_sequence", sa.BigInteger(), nullable=False),
        sa.Column("calculation_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("target_id"),
        sa.UniqueConstraint("calculation_hash", name="uq_portfolio_target_hash"),
    )
    op.create_index(
        "ix_portfolio_target_account_sequence",
        "portfolio_target",
        ["account_id", "portfolio_sequence"],
    )
    op.create_table(
        "risk_decision",
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_target_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("account_aggregate_sequence", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_sequence", sa.BigInteger(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("input_target_hash", sa.String(64), nullable=False),
        sa.Column("authorized_target_hash", sa.String(64), nullable=True),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_target_id"], ["portfolio_target.target_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("decision_hash", name="uq_risk_decision_hash"),
    )
    op.create_index(
        "ix_risk_decision_account_sequence",
        "risk_decision",
        ["account_id", "account_aggregate_sequence"],
    )


def _create_execution_tables() -> None:
    op.create_table(
        "account_reservation",
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("order_intent_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("commodity", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("remaining_amount", sa.Numeric(), nullable=False),
        sa.Column("quantum", sa.Numeric(), nullable=False),
        sa.Column("risk_decision_id", sa.UUID(), nullable=False),
        sa.Column("risk_decision_hash", sa.String(64), nullable=False),
        sa.Column("portfolio_target_id", sa.UUID(), nullable=False),
        sa.Column("authorized_target_hash", sa.String(64), nullable=False),
        sa.Column("risk_account_aggregate_sequence", sa.BigInteger(), nullable=False),
        sa.Column("account_aggregate_sequence", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_sequence", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "amount > 0 and remaining_amount >= 0 and remaining_amount <= amount "
            "and quantum > 0",
            name="account_reservation_amounts_valid",
        ),
        sa.CheckConstraint(
            "mod(amount, quantum) = 0 and mod(remaining_amount, quantum) = 0",
            name="account_reservation_quantized",
        ),
        sa.CheckConstraint(
            "account_aggregate_sequence = risk_account_aggregate_sequence + 1",
            name="account_reservation_sequence_advance",
        ),
        sa.CheckConstraint(
            "(event_sequence = 1 and previous_event_hash is null) or "
            "(event_sequence > 1 and previous_event_hash is not null)",
            name="account_reservation_event_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"], ["risk_decision.decision_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_target_id"], ["portfolio_target.target_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("reservation_id"),
        sa.UniqueConstraint(
            "order_intent_id", name="uq_account_reservation_order_intent"
        ),
    )
    op.create_index(
        "ix_account_reservation_open",
        "account_reservation",
        ["account_id", "state", "expires_at"],
    )
    op.create_table(
        "reservation_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("remaining_amount", sa.Numeric(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence > 0", name="reservation_event_sequence_positive"),
        sa.CheckConstraint(
            "(sequence = 1 and previous_event_hash is null) or "
            "(sequence > 1 and previous_event_hash is not null)",
            name="reservation_event_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["account_reservation.reservation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "reservation_id", "sequence", name="uq_reservation_event_sequence"
        ),
        sa.UniqueConstraint("event_hash", name="uq_reservation_event_hash"),
    )
    op.create_table(
        "order_intent",
        sa.Column("intent_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("risk_decision_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_target_id", sa.UUID(), nullable=False),
        sa.Column("account_aggregate_sequence", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_sequence", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["account_reservation.reservation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"], ["risk_decision.decision_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_target_id"], ["portfolio_target.target_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name="uq_order_intent_idempotency"
        ),
        sa.UniqueConstraint("intent_hash", name="uq_order_intent_hash"),
    )
    op.create_index(
        "ix_order_intent_account_created",
        "order_intent",
        ["account_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_account_reservation_order_intent",
        "account_reservation",
        "order_intent",
        ["order_intent_id"],
        ["intent_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "order_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("order_intent_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("cumulative_filled_quantity", sa.Numeric(), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(256), nullable=True),
        sa.Column("previous_event_hash", sa.String(64), nullable=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence > 0", name="order_event_sequence_positive"),
        sa.CheckConstraint(
            "(sequence = 1 and previous_event_hash is null) or "
            "(sequence > 1 and previous_event_hash is not null)",
            name="order_event_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["order_intent_id"], ["order_intent.intent_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "order_intent_id", "sequence", name="uq_order_event_sequence"
        ),
        sa.UniqueConstraint("event_hash", name="uq_order_event_hash"),
    )
    op.create_index(
        "ix_order_event_identity", "order_event", ["order_intent_id", "sequence"]
    )
    op.create_table(
        "paper_fill",
        sa.Column("fill_id", sa.UUID(), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("order_intent_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("quantity_quantum", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("price_quantum", sa.Numeric(), nullable=False),
        sa.Column("fee_currency", sa.String(3), nullable=False),
        sa.Column("fees", sa.Numeric(), nullable=False),
        sa.Column("fee_quantum", sa.Numeric(), nullable=False),
        sa.Column("slippage", sa.Numeric(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_intent_id"], ["order_intent.intent_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("fill_id"),
    )
    op.create_index(
        "ix_paper_fill_order_time", "paper_fill", ["order_intent_id", "occurred_at"]
    )


def _create_journal_tables() -> None:
    op.create_table(
        "journal_transaction",
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=True),
        sa.Column("source_order_intent_id", sa.UUID(), nullable=False),
        sa.Column("source_fill_id", sa.UUID(), nullable=False),
        sa.Column("posting_count", sa.Integer(), nullable=False),
        sa.Column("transaction_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "sequence > 0", name="journal_transaction_sequence_positive"
        ),
        sa.CheckConstraint(
            "posting_count >= 2", name="journal_transaction_posting_count"
        ),
        sa.CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="journal_transaction_chain_shape",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_order_intent_id"],
            ["order_intent.intent_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_fill_id"], ["paper_fill.fill_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint(
            "account_id", "sequence", name="uq_journal_transaction_sequence"
        ),
        sa.UniqueConstraint("transaction_hash", name="uq_journal_transaction_hash"),
        sa.UniqueConstraint("source_fill_id", name="uq_journal_transaction_fill"),
    )
    op.create_index(
        "ix_journal_transaction_account_sequence",
        "journal_transaction",
        ["account_id", "sequence"],
    )
    op.create_table(
        "journal_posting",
        sa.Column("posting_id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("posting_index", sa.Integer(), nullable=False),
        sa.Column("ledger_account", sa.String(256), nullable=False),
        sa.Column("commodity", sa.String(128), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("quantum", sa.Numeric(), nullable=False),
        sa.Column("memo", sa.String(512), nullable=True),
        sa.CheckConstraint(
            "posting_index >= 0", name="journal_posting_index_nonnegative"
        ),
        sa.CheckConstraint(
            "amount > 0 and quantum > 0", name="journal_posting_amount_positive"
        ),
        sa.CheckConstraint(
            "mod(amount, quantum) = 0", name="journal_posting_quantized"
        ),
        sa.CheckConstraint(
            "side in ('debit', 'credit')", name="journal_posting_side_valid"
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["journal_transaction.transaction_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("posting_id"),
        sa.UniqueConstraint(
            "transaction_id", "posting_index", name="uq_journal_posting_index"
        ),
    )


def _create_kill_switch() -> None:
    op.create_table(
        "paper_kill_switch",
        sa.Column("switch_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'global' and account_id is null) or "
            "(scope = 'account' and account_id is not null)",
            name="paper_kill_switch_scope_shape",
        ),
        sa.CheckConstraint("version > 0", name="paper_kill_switch_version_positive"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_account.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("switch_id"),
        sa.UniqueConstraint("account_id", name="uq_paper_kill_switch_account"),
    )
