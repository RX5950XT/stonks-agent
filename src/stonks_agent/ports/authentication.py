"""Authentication boundary shared by local and future OIDC adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from stonks_agent.domain.auth import LocalPrincipal
from stonks_agent.domain.errors import Result


class AuthenticationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization: str | None = Field(default=None, max_length=4096)
    client_host: str | None = Field(default=None, max_length=255)


@runtime_checkable
class Authenticator(Protocol):
    def authenticate(
        self,
        request: AuthenticationRequest,
    ) -> Result[LocalPrincipal]: ...
