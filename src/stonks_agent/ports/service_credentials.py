"""Short-lived attempt-bound credential boundary for service dispatch."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Self, runtime_checkable
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from stonks_agent.domain.auth import AccessTarget, Permission
from stonks_agent.domain.errors import Result
from stonks_contracts.common import Sha256, UTCDateTime


class ServiceReceiver(StrEnum):
    KRONOS = "kronos"
    TRADINGAGENTS = "tradingagents"
    QUANT_LAB = "quant_lab"
    NAUTILUS = "nautilus"
    LEAN = "lean"
    OPENBB = "openbb"


class ServiceCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receiver: ServiceReceiver
    permission: Permission
    target: AccessTarget
    request_id: UUID | None = None
    run_id: UUID | None = None
    attempt_generation: int = Field(ge=0)
    attempt_nonce_hash: Sha256
    request_hash: Sha256
    expires_no_later_than: UTCDateTime

    @model_validator(mode="after")
    def validate_dispatch_binding(self) -> Self:
        expected = {
            ServiceReceiver.KRONOS: {
                (Permission.DISPATCH_ASSIGNED_RESEARCH, "job"),
                (Permission.PREFLIGHT_ASSIGNED_RESEARCH, "job"),
            },
            ServiceReceiver.TRADINGAGENTS: {
                (Permission.DISPATCH_ASSIGNED_RESEARCH, "job")
            },
            ServiceReceiver.QUANT_LAB: {(Permission.DISPATCH_ASSIGNED_RESEARCH, "job")},
            ServiceReceiver.NAUTILUS: {
                (Permission.DISPATCH_ASSIGNED_BACKTEST, "backtest_job")
            },
            ServiceReceiver.LEAN: {
                (Permission.DISPATCH_ASSIGNED_BACKTEST, "backtest_job")
            },
            ServiceReceiver.OPENBB: {
                (Permission.DISPATCH_ASSIGNED_MARKET_DATA, "market")
            },
        }
        if (self.permission, self.target.kind.value) not in expected[self.receiver]:
            raise ValueError("service credential receiver authority is invalid")
        unleased = self.attempt_generation == 0
        if unleased:
            valid_permission = (
                self.permission is Permission.DISPATCH_ASSIGNED_MARKET_DATA
                and self.receiver is ServiceReceiver.OPENBB
                and self.target.kind.value == "market"
            ) or (
                self.permission is Permission.PREFLIGHT_ASSIGNED_RESEARCH
                and self.receiver is ServiceReceiver.KRONOS
                and self.target.kind.value == "job"
            )
            valid = (
                self.request_id is None
                and self.run_id is None
                and self.attempt_nonce_hash == self.request_hash
                and valid_permission
            )
        else:
            valid = self.request_id is not None and self.run_id is not None
        if not valid:
            raise ValueError("service credential dispatch binding is invalid")
        return self


class ServiceBearerCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: SecretStr = Field(repr=False, exclude=True)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if (
            not 1 <= len(raw) <= 4096
            or raw.strip() != raw
            or any(not 0x21 <= ord(character) <= 0x7E for character in raw)
        ):
            raise ValueError("service bearer credential is invalid")
        return value

    def authorization_header(self) -> str:
        return f"Bearer {self.token.get_secret_value()}"


@runtime_checkable
class ServiceCredentialProvider(Protocol):
    def issue(
        self,
        request: ServiceCredentialRequest,
    ) -> Result[ServiceBearerCredential]: ...
