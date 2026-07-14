"""SQLAlchemy mappings for the canonical PostgreSQL paper fund."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from stonks_agent.adapters.postgres.models.core import Base


class PaperAccountRow(Base):
    __tablename__ = "paper_account"
    __table_args__ = (
        CheckConstraint(
            "aggregate_sequence >= 0 and portfolio_sequence >= 0 "
            "and ledger_sequence >= 0",
            name="paper_account_sequences_nonnegative",
        ),
        CheckConstraint(
            "(ledger_sequence = 0 and ledger_hash is null) or "
            "(ledger_sequence > 0 and ledger_hash is not null)",
            name="paper_account_ledger_head_shape",
        ),
    )

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    aggregate_sequence: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0")
    )
    portfolio_sequence: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0")
    )
    ledger_sequence: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    ledger_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperAccountOpeningSnapshotRow(Base):
    __tablename__ = "paper_account_opening_snapshot"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_paper_account_opening_snapshot_id"),
        UniqueConstraint(
            "snapshot_hash", name="uq_paper_account_opening_snapshot_hash"
        ),
    )

    account_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("paper_account.account_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperAccountEventRow(Base):
    __tablename__ = "paper_account_event"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "sequence", name="uq_paper_account_event_sequence"
        ),
        UniqueConstraint("event_hash", name="uq_paper_account_event_hash"),
        CheckConstraint("sequence > 0", name="paper_account_event_sequence_positive"),
        CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="paper_account_event_chain_shape",
        ),
        Index("ix_paper_account_event_identity", "account_id", "sequence"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_ref_type: Mapped[str] = mapped_column(String(64))
    aggregate_ref_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class PaperCashProjectionRow(Base):
    __tablename__ = "paper_cash_projection"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", "currency"),
        CheckConstraint(
            "settled_amount >= 0 and reserved_amount >= 0 "
            "and reserved_amount <= settled_amount and quantum > 0",
            name="paper_cash_projection_amounts_valid",
        ),
        CheckConstraint(
            "mod(settled_amount, quantum) = 0 and mod(reserved_amount, quantum) = 0",
            name="paper_cash_projection_quantized",
        ),
        CheckConstraint(
            "updated_sequence >= 0", name="paper_cash_projection_sequence_nonnegative"
        ),
    )

    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    settled_amount: Mapped[Decimal] = mapped_column(Numeric)
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric)
    quantum: Mapped[Decimal] = mapped_column(Numeric)
    updated_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperPositionProjectionRow(Base):
    __tablename__ = "paper_position_projection"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", "instrument_id"),
        CheckConstraint(
            "quantity >= 0 and sellable_quantity >= 0 and reserved_quantity >= 0 "
            "and sellable_quantity <= quantity "
            "and reserved_quantity <= sellable_quantity and quantum > 0",
            name="paper_position_projection_amounts_valid",
        ),
        CheckConstraint(
            "mod(quantity, quantum) = 0 and mod(sellable_quantity, quantum) = 0 "
            "and mod(reserved_quantity, quantum) = 0",
            name="paper_position_projection_quantized",
        ),
        CheckConstraint(
            "updated_sequence >= 0",
            name="paper_position_projection_sequence_nonnegative",
        ),
    )

    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("instrument.instrument_id", ondelete="RESTRICT"),
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    sellable_quantity: Mapped[Decimal] = mapped_column(Numeric)
    reserved_quantity: Mapped[Decimal] = mapped_column(Numeric)
    quantum: Mapped[Decimal] = mapped_column(Numeric)
    updated_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperLedgerAccountProjectionRow(Base):
    __tablename__ = "paper_ledger_account_projection"
    __table_args__ = (
        PrimaryKeyConstraint("account_id", "ledger_account", "commodity"),
        CheckConstraint(
            "quantum > 0 and debit_total >= 0 and credit_total >= 0",
            name="paper_ledger_projection_amounts_valid",
        ),
        CheckConstraint(
            "mod(debit_total, quantum) = 0 and mod(credit_total, quantum) = 0",
            name="paper_ledger_projection_quantized",
        ),
        CheckConstraint(
            "updated_ledger_sequence >= 0",
            name="paper_ledger_projection_sequence_nonnegative",
        ),
    )

    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    ledger_account: Mapped[str] = mapped_column(String(256))
    commodity: Mapped[str] = mapped_column(String(128))
    quantum: Mapped[Decimal] = mapped_column(Numeric)
    debit_total: Mapped[Decimal] = mapped_column(Numeric)
    credit_total: Mapped[Decimal] = mapped_column(Numeric)
    updated_ledger_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioTargetRow(Base):
    __tablename__ = "portfolio_target"
    __table_args__ = (
        UniqueConstraint("calculation_hash", name="uq_portfolio_target_hash"),
        Index(
            "ix_portfolio_target_account_sequence", "account_id", "portfolio_sequence"
        ),
    )

    target_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    portfolio_snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    account_aggregate_sequence: Mapped[int] = mapped_column(BigInteger)
    portfolio_sequence: Mapped[int] = mapped_column(BigInteger)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskDecisionRow(Base):
    __tablename__ = "risk_decision"
    __table_args__ = (
        UniqueConstraint("decision_hash", name="uq_risk_decision_hash"),
        Index(
            "ix_risk_decision_account_sequence",
            "account_id",
            "account_aggregate_sequence",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    portfolio_target_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("portfolio_target.target_id", ondelete="RESTRICT"),
    )
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    account_aggregate_sequence: Mapped[int] = mapped_column(BigInteger)
    portfolio_sequence: Mapped[int] = mapped_column(BigInteger)
    approved: Mapped[bool] = mapped_column(Boolean)
    decision_hash: Mapped[str] = mapped_column(String(64))
    input_target_hash: Mapped[str] = mapped_column(String(64))
    authorized_target_hash: Mapped[str | None] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountReservationRow(Base):
    __tablename__ = "account_reservation"
    __table_args__ = (
        UniqueConstraint("order_intent_id", name="uq_account_reservation_order_intent"),
        ForeignKeyConstraint(
            ["order_intent_id"],
            ["order_intent.intent_id"],
            name="fk_account_reservation_order_intent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "amount > 0 and remaining_amount >= 0 and remaining_amount <= amount "
            "and quantum > 0",
            name="account_reservation_amounts_valid",
        ),
        CheckConstraint(
            "mod(amount, quantum) = 0 and mod(remaining_amount, quantum) = 0",
            name="account_reservation_quantized",
        ),
        CheckConstraint(
            "account_aggregate_sequence = risk_account_aggregate_sequence + 1",
            name="account_reservation_sequence_advance",
        ),
        CheckConstraint(
            "(event_sequence = 1 and previous_event_hash is null) or "
            "(event_sequence > 1 and previous_event_hash is not null)",
            name="account_reservation_event_chain_shape",
        ),
        Index("ix_account_reservation_open", "account_id", "state", "expires_at"),
    )

    reservation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("instrument.instrument_id", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    commodity: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Decimal] = mapped_column(Numeric)
    remaining_amount: Mapped[Decimal] = mapped_column(Numeric)
    quantum: Mapped[Decimal] = mapped_column(Numeric)
    risk_decision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("risk_decision.decision_id", ondelete="RESTRICT"),
    )
    risk_decision_hash: Mapped[str] = mapped_column(String(64))
    portfolio_target_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("portfolio_target.target_id", ondelete="RESTRICT"),
    )
    authorized_target_hash: Mapped[str] = mapped_column(String(64))
    risk_account_aggregate_sequence: Mapped[int] = mapped_column(BigInteger)
    account_aggregate_sequence: Mapped[int] = mapped_column(BigInteger)
    portfolio_sequence: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class ReservationEventRow(Base):
    __tablename__ = "reservation_event"
    __table_args__ = (
        UniqueConstraint(
            "reservation_id", "sequence", name="uq_reservation_event_sequence"
        ),
        UniqueConstraint("event_hash", name="uq_reservation_event_hash"),
        CheckConstraint("sequence > 0", name="reservation_event_sequence_positive"),
        CheckConstraint(
            "(sequence = 1 and previous_event_hash is null) or "
            "(sequence > 1 and previous_event_hash is not null)",
            name="reservation_event_chain_shape",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    reservation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account_reservation.reservation_id", ondelete="RESTRICT"),
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric)
    remaining_amount: Mapped[Decimal] = mapped_column(Numeric)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(256))
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class OrderIntentRow(Base):
    __tablename__ = "order_intent"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_order_intent_idempotency"
        ),
        UniqueConstraint("intent_hash", name="uq_order_intent_hash"),
        Index("ix_order_intent_account_created", "account_id", "created_at"),
    )

    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("instrument.instrument_id", ondelete="RESTRICT"),
    )
    reservation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account_reservation.reservation_id", ondelete="RESTRICT"),
    )
    risk_decision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("risk_decision.decision_id", ondelete="RESTRICT"),
    )
    portfolio_target_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("portfolio_target.target_id", ondelete="RESTRICT"),
    )
    account_aggregate_sequence: Mapped[int] = mapped_column(BigInteger)
    portfolio_sequence: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(256))
    intent_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderEventRow(Base):
    __tablename__ = "order_event"
    __table_args__ = (
        UniqueConstraint("order_intent_id", "sequence", name="uq_order_event_sequence"),
        UniqueConstraint("event_hash", name="uq_order_event_hash"),
        CheckConstraint("sequence > 0", name="order_event_sequence_positive"),
        CheckConstraint(
            "(sequence = 1 and previous_event_hash is null) or "
            "(sequence > 1 and previous_event_hash is not null)",
            name="order_event_chain_shape",
        ),
        Index("ix_order_event_identity", "order_intent_id", "sequence"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("order_intent.intent_id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(Numeric)
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(256))
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class PaperFillRow(Base):
    __tablename__ = "paper_fill"
    __table_args__ = (
        Index("ix_paper_fill_order_time", "order_intent_id", "occurred_at"),
    )

    fill_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    order_intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("order_intent.intent_id", ondelete="RESTRICT")
    )
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("instrument.instrument_id", ondelete="RESTRICT"),
    )
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    quantity_quantum: Mapped[Decimal] = mapped_column(Numeric)
    price: Mapped[Decimal] = mapped_column(Numeric)
    price_quantum: Mapped[Decimal] = mapped_column(Numeric)
    fee_currency: Mapped[str] = mapped_column(String(3))
    fees: Mapped[Decimal] = mapped_column(Numeric)
    fee_quantum: Mapped[Decimal] = mapped_column(Numeric)
    slippage: Mapped[Decimal] = mapped_column(Numeric)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class PaperExecutionReceiptRow(Base):
    __tablename__ = "paper_execution_receipt"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "idempotency_key", name="uq_paper_execution_idempotency"
        ),
        UniqueConstraint("command_id", name="uq_paper_execution_command"),
        UniqueConstraint("receipt_hash", name="uq_paper_execution_receipt_hash"),
        UniqueConstraint("outcome_hash", name="uq_paper_execution_outcome_hash"),
        Index("ix_paper_execution_account_time", "account_id", "created_at"),
    )

    receipt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(256))
    command_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    command_hash: Mapped[str] = mapped_column(String(64))
    order_intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("order_intent.intent_id", ondelete="RESTRICT")
    )
    intent_hash: Mapped[str] = mapped_column(String(64))
    receipt_hash: Mapped[str] = mapped_column(String(64))
    outcome_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JournalTransactionRow(Base):
    __tablename__ = "journal_transaction"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "sequence", name="uq_journal_transaction_sequence"
        ),
        UniqueConstraint("transaction_hash", name="uq_journal_transaction_hash"),
        UniqueConstraint("source_fill_id", name="uq_journal_transaction_fill"),
        CheckConstraint("sequence > 0", name="journal_transaction_sequence_positive"),
        CheckConstraint("posting_count >= 2", name="journal_transaction_posting_count"),
        CheckConstraint(
            "(sequence = 1 and previous_hash is null) or "
            "(sequence > 1 and previous_hash is not null)",
            name="journal_transaction_chain_shape",
        ),
        Index("ix_journal_transaction_account_sequence", "account_id", "sequence"),
    )

    transaction_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    source_order_intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("order_intent.intent_id", ondelete="RESTRICT")
    )
    source_fill_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("paper_fill.fill_id", ondelete="RESTRICT")
    )
    posting_count: Mapped[int] = mapped_column(Integer)
    transaction_hash: Mapped[str] = mapped_column(String(64))


class JournalPostingRow(Base):
    __tablename__ = "journal_posting"
    __table_args__ = (
        UniqueConstraint(
            "transaction_id", "posting_index", name="uq_journal_posting_index"
        ),
        CheckConstraint("posting_index >= 0", name="journal_posting_index_nonnegative"),
        CheckConstraint(
            "amount > 0 and quantum > 0", name="journal_posting_amount_positive"
        ),
        CheckConstraint("mod(amount, quantum) = 0", name="journal_posting_quantized"),
        CheckConstraint(
            "side in ('debit', 'credit')", name="journal_posting_side_valid"
        ),
    )

    posting_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    transaction_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("journal_transaction.transaction_id", ondelete="RESTRICT"),
    )
    posting_index: Mapped[int] = mapped_column(Integer)
    ledger_account: Mapped[str] = mapped_column(String(256))
    commodity: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(Numeric)
    quantum: Mapped[Decimal] = mapped_column(Numeric)
    memo: Mapped[str | None] = mapped_column(String(512))


class PaperKillSwitchRow(Base):
    __tablename__ = "paper_kill_switch"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_paper_kill_switch_account"),
        Index(
            "uq_paper_kill_switch_global",
            "scope",
            unique=True,
            postgresql_where=text("scope='global'"),
        ),
        CheckConstraint(
            "(scope = 'global' and account_id is null) or "
            "(scope = 'account' and account_id is not null)",
            name="paper_kill_switch_scope_shape",
        ),
        CheckConstraint("version > 0", name="paper_kill_switch_version_positive"),
    )

    switch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    active: Mapped[bool] = mapped_column(Boolean)
    reason_code: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperOperatorAuditHeadRow(Base):
    __tablename__ = "paper_operator_audit_head"
    __table_args__ = (
        CheckConstraint("head_id = 1", name="paper_operator_head_singleton"),
        CheckConstraint("sequence >= 0", name="paper_operator_head_sequence"),
        CheckConstraint(
            "(sequence = 0 and action_hash is null) or "
            "(sequence > 0 and action_hash is not null)",
            name="paper_operator_head_hash_shape",
        ),
    )

    head_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    action_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperOperatorActionRow(Base):
    __tablename__ = "paper_operator_action"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="paper_operator_action_sequence"),
        CheckConstraint("switch_version > 0", name="paper_operator_switch_version"),
        CheckConstraint(
            "(scope = 'global' and account_id is null) or "
            "(scope = 'account' and account_id is not null)",
            name="paper_operator_action_scope_shape",
        ),
        CheckConstraint(
            "(sequence = 1 and previous_action_hash is null) or "
            "(sequence > 1 and previous_action_hash is not null)",
            name="paper_operator_action_chain_shape",
        ),
        UniqueConstraint("sequence", name="uq_paper_operator_action_sequence"),
        UniqueConstraint("action_hash", name="uq_paper_operator_action_hash"),
    )

    action_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    action_type: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("paper_account.account_id", ondelete="RESTRICT")
    )
    actor: Mapped[str] = mapped_column(String(128))
    reason_code: Mapped[str] = mapped_column(String(128))
    switch_version: Mapped[int] = mapped_column(Integer)
    previous_action_hash: Mapped[str | None] = mapped_column(String(64))
    action_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
