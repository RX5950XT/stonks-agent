"""Immutable hard-risk decisions bound to exact account and portfolio sequences."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.portfolio import PortfolioTarget
from stonks_contracts.common import (
    DecimalString,
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class RiskCheck(TradingModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    passed: bool
    actual: DecimalString | None = None
    limit: DecimalString | None = None
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if not self.passed and (self.reason is None or not self.reason.strip()):
            raise ValueError("failed risk check requires a reason")
        return self


class RiskDecision(TradingModel):
    decision_id: UUID
    portfolio_target_id: UUID
    input_target_hash: Sha256
    account_id: NonEmptyString
    account_aggregate_sequence: int = Field(ge=0)
    portfolio_sequence: int = Field(ge=0)
    approved: bool
    normalized_target: PortfolioTarget | None = None
    checks: tuple[RiskCheck, ...] = Field(min_length=1, max_length=256)
    policy_version: NonEmptyString
    policy_hash: Sha256
    decided_at: UTCDateTime
    expires_at: UTCDateTime
    decision_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        decision_id: UUID,
        target: PortfolioTarget,
        approved: bool,
        normalized_target: PortfolioTarget | None,
        checks: tuple[RiskCheck, ...],
        policy_version: str,
        policy_hash: str,
        decided_at: datetime,
        expires_at: datetime,
    ) -> RiskDecision:
        values = {
            "decision_id": decision_id,
            "portfolio_target_id": target.target_id,
            "input_target_hash": target.calculation_hash,
            "account_id": target.account_id,
            "account_aggregate_sequence": target.account_aggregate_sequence,
            "portfolio_sequence": target.portfolio_sequence,
            "approved": approved,
            "normalized_target": normalized_target,
            "checks": checks,
            "policy_version": policy_version,
            "policy_hash": policy_hash,
            "decided_at": decided_at,
            "expires_at": expires_at,
        }
        provisional = cls.model_construct(
            decision_id=decision_id,
            portfolio_target_id=target.target_id,
            input_target_hash=target.calculation_hash,
            account_id=target.account_id,
            account_aggregate_sequence=target.account_aggregate_sequence,
            portfolio_sequence=target.portfolio_sequence,
            approved=approved,
            normalized_target=normalized_target,
            checks=checks,
            policy_version=policy_version,
            policy_hash=policy_hash,
            decided_at=decided_at,
            expires_at=expires_at,
            decision_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"decision_hash": provisional.expected_decision_hash()}
        )

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.expires_at <= self.decided_at:
            raise ValueError("risk decision expiry must be after decision time")
        codes = tuple(item.code for item in self.checks)
        if codes != tuple(sorted(codes)) or len(codes) != len(set(codes)):
            raise ValueError("risk checks must be unique and stably sorted")
        if self.approved:
            if self.normalized_target is None:
                raise ValueError("approved decision requires a normalized target")
            if not all(item.passed for item in self.checks):
                raise ValueError("approved decision requires every risk check to pass")
        elif all(item.passed for item in self.checks):
            raise ValueError(
                "rejected decision requires at least one failed risk check"
            )
        self._validate_normalized_target()
        if self.decision_hash != self.expected_decision_hash():
            raise ValueError("risk decision hash does not match payload")
        return self

    def _validate_normalized_target(self) -> None:
        target = self.normalized_target
        if target is None:
            return
        if (
            target.account_id != self.account_id
            or target.account_aggregate_sequence != self.account_aggregate_sequence
            or target.portfolio_sequence != self.portfolio_sequence
        ):
            raise ValueError("normalized target must preserve account sequence binding")

    def is_current(
        self,
        *,
        account_aggregate_sequence: int,
        portfolio_sequence: int,
        at: object,
    ) -> bool:
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            return False
        normalized_at = at.astimezone(UTC)
        return (
            self.approved
            and self.decided_at <= normalized_at < self.expires_at
            and account_aggregate_sequence == self.account_aggregate_sequence
            and portfolio_sequence == self.portfolio_sequence
        )

    @property
    def authorized_target_hash(self) -> str | None:
        if self.normalized_target is None:
            return None
        return self.normalized_target.calculation_hash

    def expected_decision_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"decision_hash"})
        )
