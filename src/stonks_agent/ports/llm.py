"""Typed structured-output LLM boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.research import StructuredLLMRequest, StructuredLLMResponse


@runtime_checkable
class LLMPort(Protocol):
    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]: ...
