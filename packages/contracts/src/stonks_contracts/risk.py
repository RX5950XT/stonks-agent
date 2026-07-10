"""Deterministic hard-risk and serialized reservation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import (
    ContractModel,
    DecimalString,
    NonEmptyString,
    PositiveDecimal,
    Sha256,
    UTCDateTime,
)
from .portfolio import PortfolioTarget


class RiskCheck(ContractModel):
    code: NonEmptyString
    passed: bool
    actual: DecimalString | None = None
    limit: DecimalString | None = None
    reason: str | None = None


class RiskDecision(ContractModel):
    decision_id: UUID
    portfolio_target_id: UUID
    account_id: NonEmptyString
    approved: bool
    normalized_target: PortfolioTarget | None = None
    reasons: tuple[str, ...]
    checks: tuple[RiskCheck, ...] = ()
    limits_snapshot_hash: Sha256
    policy_version: NonEmptyString
    policy_hash: Sha256
    decided_at: UTCDateTime
    expires_at: UTCDateTime

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.approved and self.normalized_target is None:
            raise ValueError("approved decision requires normalized_target")
        if self.expires_at <= self.decided_at:
            raise ValueError("expires_at must be later than decided_at")
        return self


class ReservationKind(StrEnum):
    CASH = "cash"
    POSITION = "position"


class ReservationState(StrEnum):
    OPEN = "open"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class AccountReservation(ContractModel):
    reservation_id: UUID
    account_id: NonEmptyString
    kind: ReservationKind
    commodity: NonEmptyString
    amount: PositiveDecimal
    risk_decision_id: UUID
    portfolio_sequence: int
    order_intent_id: UUID
    state: ReservationState = ReservationState.OPEN
    created_at: UTCDateTime
    expires_at: UTCDateTime

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self

    @property
    def status(self) -> ReservationState:
        """Expose domain terminology without duplicating the serialized state."""
        return self.state
