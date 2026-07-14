from __future__ import annotations

import ast
import inspect
from typing import get_type_hints

import stonks_agent.application.research.community_feedback as community_feedback
from stonks_agent.application.research.community_feedback import (
    CommunityFeedbackAction,
    CommunityFeedbackDecision,
    apply_community_feedback,
)
from stonks_agent.ports.queue import JobEnqueuePort


def test_community_policy_has_no_control_plane_dependency() -> None:
    tree = ast.parse(inspect.getsource(community_feedback))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "stonks_agent.application.execution",
        "stonks_agent.application.portfolio",
        "stonks_agent.application.risk",
        "stonks_agent.application.signals",
        "stonks_agent.domain.orders",
        "stonks_agent.domain.portfolio",
        "stonks_agent.domain.risk",
        "stonks_agent.domain.signal",
        "stonks_agent.ports.execution",
        "stonks_agent.ports.trading_repository",
    }

    assert imported.isdisjoint(forbidden)
    assert get_type_hints(apply_community_feedback)["queue"] is JobEnqueuePort


def test_decision_surface_is_research_only_and_extra_fields_are_forbidden() -> None:
    fields = set(CommunityFeedbackDecision.model_fields)
    forbidden_fields = {
        "alpha_signal",
        "order_intent",
        "portfolio_target",
        "quantity",
        "risk_decision",
        "target_weight",
    }

    assert fields.isdisjoint(forbidden_fields)
    assert CommunityFeedbackDecision.model_config["extra"] == "forbid"
    assert {item.value for item in CommunityFeedbackAction} == {
        "ignore",
        "lower_confidence",
        "request_research",
    }
