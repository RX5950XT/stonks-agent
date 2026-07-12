from __future__ import annotations

from stonks_agent.domain.errors import Result
from stonks_agent.domain.research import (
    ResearchArtifact,
    ResearchRequest,
    StructuredLLMRequest,
    StructuredLLMResponse,
)
from stonks_agent.domain.tool_policy import AuthorizedToolCall, ToolResult
from stonks_agent.ports.llm import LLMPort
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


def test_research_ports_are_runtime_checkable_and_capability_narrow() -> None:
    assert isinstance(ResearchWorker(), ResearchWorkerPort)
    assert isinstance(LLM(), LLMPort)
    assert isinstance(Tool(), ToolPort)
    assert set(dir(ResearchWorkerPort)) >= {"research"}
    assert "submit" not in dir(ResearchWorkerPort)
    assert "execute" not in dir(ResearchWorkerPort)
