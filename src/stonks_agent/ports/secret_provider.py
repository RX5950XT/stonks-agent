"""Named secret resolution boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.secrets import ResolvedSecret, SecretAccessRequest


@runtime_checkable
class SecretProvider(Protocol):
    def resolve(self, request: SecretAccessRequest) -> Result[ResolvedSecret]: ...
