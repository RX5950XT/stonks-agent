from __future__ import annotations

from pydantic import SecretStr

from stonks_agent.domain.errors import Failure, Result, Success
from stonks_agent.domain.secrets import (
    ResolvedSecret,
    SecretAccessRequest,
)

type ScriptedSecret = tuple[str, str] | Failure


class ScriptedSecretProvider:
    """Return versioned test secrets while recording exact access requests."""

    def __init__(self, *results: ScriptedSecret) -> None:
        if not results:
            raise ValueError("at least one scripted secret result is required")
        self._results = results
        self.requests: list[SecretAccessRequest] = []

    def resolve(self, request: SecretAccessRequest) -> Result[ResolvedSecret]:
        self.requests.append(request)
        result = self._results[min(len(self.requests) - 1, len(self._results) - 1)]
        if isinstance(result, Failure):
            return result
        value, version = result
        return Success(ResolvedSecret(value=SecretStr(value), version=version))
