"""Frozen paper kill-switch and operator audit contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_agent.domain.ledger import LedgerReconciliationReport
from stonks_contracts.common import (
    NonEmptyString,
    Sha256,
    UTCDateTime,
    stable_payload_hash,
)

_PLACEHOLDER_HASH = "0" * 64


class KillSwitchScope(StrEnum):
    GLOBAL = "global"
    ACCOUNT = "account"


class OperatorActionType(StrEnum):
    ACTIVATED = "activated"
    RECONCILED = "reconciled"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RESUMED = "resumed"
    RESUME_REJECTED = "resume_rejected"


class PaperKillSwitchState(TradingModel):
    switch_id: UUID
    scope: KillSwitchScope
    account_id: NonEmptyString | None = None
    active: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    actor: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    version: int = Field(ge=1)
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.scope is KillSwitchScope.GLOBAL) != (self.account_id is None):
            raise ValueError("kill switch scope and account shape differ")
        if self.updated_at < self.created_at:
            raise ValueError("kill switch timeline is invalid")
        return self


class PaperOperatorAction(TradingModel):
    action_id: UUID
    sequence: int = Field(ge=1)
    action_type: OperatorActionType
    scope: KillSwitchScope
    account_id: NonEmptyString | None = None
    actor: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    switch_version: int = Field(ge=1)
    cancelled_order_ids: tuple[UUID, ...] = Field(max_length=100_000)
    reconciliation_hashes: tuple[Sha256, ...] = Field(max_length=100_000)
    mismatch_reasons: tuple[NonEmptyString, ...] = Field(max_length=100_000)
    occurred_at: UTCDateTime
    previous_action_hash: Sha256 | None = None
    action_hash: Sha256

    @classmethod
    def create(
        cls,
        *,
        action_id: UUID,
        sequence: int,
        action_type: OperatorActionType,
        scope: KillSwitchScope,
        account_id: str | None,
        actor: str,
        reason_code: str,
        switch_version: int,
        cancelled_order_ids: tuple[UUID, ...],
        reconciliation_hashes: tuple[str, ...],
        mismatch_reasons: tuple[str, ...],
        occurred_at: datetime,
        previous_action_hash: str | None,
    ) -> PaperOperatorAction:
        values = {
            "action_id": action_id,
            "sequence": sequence,
            "action_type": action_type,
            "scope": scope,
            "account_id": account_id,
            "actor": actor,
            "reason_code": reason_code,
            "switch_version": switch_version,
            "cancelled_order_ids": cancelled_order_ids,
            "reconciliation_hashes": reconciliation_hashes,
            "mismatch_reasons": mismatch_reasons,
            "occurred_at": occurred_at,
            "previous_action_hash": previous_action_hash,
        }
        provisional = cls.model_construct(
            action_id=action_id,
            sequence=sequence,
            action_type=action_type,
            scope=scope,
            account_id=account_id,
            actor=actor,
            reason_code=reason_code,
            switch_version=switch_version,
            cancelled_order_ids=cancelled_order_ids,
            reconciliation_hashes=reconciliation_hashes,
            mismatch_reasons=mismatch_reasons,
            occurred_at=occurred_at,
            previous_action_hash=previous_action_hash,
            action_hash=_PLACEHOLDER_HASH,
        )
        return cls.model_validate(
            values | {"action_hash": provisional.expected_action_hash()}
        )

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.scope is KillSwitchScope.GLOBAL) != (self.account_id is None):
            raise ValueError("operator action scope and account shape differ")
        if (self.sequence == 1) != (self.previous_action_hash is None):
            raise ValueError("only genesis operator action may omit previous hash")
        _require_sorted_unique(
            tuple(str(item) for item in self.cancelled_order_ids),
            "cancelled order IDs",
        )
        _require_sorted_unique(self.reconciliation_hashes, "reconciliation hashes")
        _require_sorted_unique(self.mismatch_reasons, "mismatch reasons")
        if self.action_hash != self.expected_action_hash():
            raise ValueError("operator action hash does not match payload")
        return self

    def expected_action_hash(self) -> str:
        return stable_payload_hash(
            self.model_dump(mode="json", exclude={"action_hash"})
        )


class PaperOperationRecord(TradingModel):
    state: PaperKillSwitchState
    action: PaperOperatorAction

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.state.scope is not self.action.scope
            or self.state.account_id != self.action.account_id
            or self.state.version != self.action.switch_version
        ):
            raise ValueError("operator action does not match resulting switch state")
        return self


class PaperReconciliationResult(TradingModel):
    report: LedgerReconciliationReport
    state: PaperKillSwitchState
    action: PaperOperatorAction


class ResumePreparation(TradingModel):
    state: PaperKillSwitchState
    account_ids: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_preparation(self) -> Self:
        _require_sorted_unique(self.account_ids, "resume account IDs")
        if not self.state.active:
            raise ValueError("resume preparation requires an active switch")
        if self.state.scope is KillSwitchScope.ACCOUNT and self.account_ids != (
            self.state.account_id,
        ):
            raise ValueError("account resume must lock its exact account")
        return self


class ActivateKillSwitchCommand(TradingModel):
    action_id: UUID
    scope: KillSwitchScope
    account_id: NonEmptyString | None = None
    expected_version: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        _validate_scope(self.scope, self.account_id)
        if self.scope is KillSwitchScope.GLOBAL and self.expected_version < 1:
            raise ValueError("global switch must already exist")
        return self


class ReconcilePaperCommand(TradingModel):
    action_id: UUID
    account_id: NonEmptyString
    requested_at: UTCDateTime


class ResumePaperCommand(TradingModel):
    action_id: UUID
    scope: KillSwitchScope
    account_id: NonEmptyString | None = None
    expected_version: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        _validate_scope(self.scope, self.account_id)
        return self


def reconciliation_report_hash(report: LedgerReconciliationReport) -> str:
    return stable_payload_hash(report.model_dump(mode="json"))


def _validate_scope(scope: KillSwitchScope, account_id: str | None) -> None:
    if (scope is KillSwitchScope.GLOBAL) != (account_id is None):
        raise ValueError("kill switch scope and account shape differ")


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and stably sorted")
