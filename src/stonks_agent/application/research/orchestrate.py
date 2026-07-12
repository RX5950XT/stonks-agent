"""Compose context building, bounded tool use, and artifact construction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from stonks_agent.application.research.context_builder import build_research_context
from stonks_agent.application.research.tool_loop import (
    ResearchLoopResult,
    run_tool_loop,
)
from stonks_agent.domain.errors import Failure, Result
from stonks_agent.domain.research import ResearchArtifact, ResearchRequest
from stonks_agent.domain.tool_policy import ResearchPrincipal, ToolPolicy
from stonks_agent.ports.artifact_store import ArtifactReaderPort
from stonks_agent.ports.llm import LLMPort
from stonks_agent.ports.tool import ToolPort
from stonks_contracts.evidence import EvidenceItem


class ResearchArtifactBuilder(Protocol):
    def build(
        self,
        request: ResearchRequest,
        result: ResearchLoopResult,
        *,
        created_at: datetime,
    ) -> Result[ResearchArtifact]: ...


def orchestrate_research(
    *,
    request: ResearchRequest,
    evidence_items: tuple[EvidenceItem, ...],
    principal: ResearchPrincipal,
    policy: ToolPolicy,
    llm: LLMPort,
    tool: ToolPort,
    artifacts: ArtifactReaderPort,
    builder: ResearchArtifactBuilder,
    clock: Callable[[], datetime],
) -> Result[ResearchArtifact]:
    context = build_research_context(request, evidence_items, artifacts)
    if isinstance(context, Failure):
        return context
    loop = run_tool_loop(
        request=request,
        context=context.value,
        principal=principal,
        policy=policy,
        llm=llm,
        tool=tool,
        artifacts=artifacts,
        clock=clock,
    )
    if isinstance(loop, Failure):
        return loop
    return builder.build(request, loop.value, created_at=clock())
