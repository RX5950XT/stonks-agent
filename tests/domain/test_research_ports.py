from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from stonks_agent.domain.errors import Result
from stonks_agent.domain.paper_cycle import CanonicalCycleReference
from stonks_agent.domain.research import (
    ResearchArtifact,
    ResearchRequest,
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.tool_policy import AuthorizedToolCall, ToolResult
from stonks_agent.ports.llm import LLMPort
from stonks_agent.ports.paper_cycle import PaperCycleObjectResolver
from stonks_agent.ports.research_worker import ResearchWorkerPort
from stonks_agent.ports.tool import ToolPort


class ResearchWorker:
    def research(self, request: ResearchRequest) -> Result[ResearchArtifact]:
        raise NotImplementedError


class LLM:
    def complete(
        self,
        request: StructuredLLMRequest,
    ) -> Result[StructuredLLMResponse]:
        raise NotImplementedError


class Tool:
    def execute(self, call: AuthorizedToolCall) -> Result[ToolResult]:
        raise NotImplementedError


class DurableCycleResolver:
    def resolve[T: BaseModel](
        self,
        reference: CanonicalCycleReference,
        *,
        object_type: type[T],
        object_id: Callable[[T], str],
        semantic_hash: Callable[[T], str],
    ) -> Result[T]:
        del reference, object_type, object_id, semantic_hash
        raise NotImplementedError


def test_research_ports_are_runtime_checkable_and_capability_narrow() -> None:
    assert isinstance(ResearchWorker(), ResearchWorkerPort)
    assert isinstance(LLM(), LLMPort)
    assert isinstance(Tool(), ToolPort)
    assert isinstance(DurableCycleResolver(), PaperCycleObjectResolver)
    assert set(dir(ResearchWorkerPort)) >= {"research"}
    assert "submit" not in dir(ResearchWorkerPort)
    assert "execute" not in dir(ResearchWorkerPort)
    assert set(dir(PaperCycleObjectResolver)) >= {"resolve"}
    assert "put" not in dir(PaperCycleObjectResolver)
