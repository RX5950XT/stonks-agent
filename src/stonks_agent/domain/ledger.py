"""Immutable canonical ledger projection state."""

from __future__ import annotations

from pydantic import Field, model_validator

from stonks_agent.domain._trading import TradingModel
from stonks_contracts.common import NonEmptyString, Sha256


class LedgerHead(TradingModel):
    account_id: NonEmptyString
    sequence: int = Field(ge=0)
    transaction_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_head(self) -> LedgerHead:
        if (self.sequence == 0) != (self.transaction_hash is None):
            raise ValueError("only genesis ledger head may omit transaction hash")
        return self
