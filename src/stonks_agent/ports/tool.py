"""Typed boundary accepting only pre-authorized research tool calls."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_agent.domain.tool_policy import AuthorizedToolCall, ToolResult


@runtime_checkable
class ToolPort(Protocol):
    def execute(self, call: AuthorizedToolCall) -> Result[ToolResult]: ...
