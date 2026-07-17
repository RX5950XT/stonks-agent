from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest
from fixtures.secret_provider import ScriptedSecretProvider
from pydantic import BaseModel

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.adapters.platform import (
    AI_TRADER_ENDPOINT_TEMPLATES,
    AiTraderEventPollRequest,
    AiTraderHttpAdapter,
    AiTraderReplyRequest,
    MemoryPlatformEventInbox,
)
from stonks_agent.domain.secrets import SecretRef
from stonks_agent.ports.platform import PlatformPort
from stonks_contracts.platform import (
    ChallengeRequest,
    ChallengeResult,
    ExperimentRequest,
    ExperimentResult,
    FeedbackPage,
    FeedbackPollRequest,
    PublishedThesis,
    PublishThesisRequest,
)

ROOT = Path(__file__).parents[2]
_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"target_agent", "agent_id", "target", "order", "risk", "execution"}
)
_PUBLIC_AI_TRADER_MODELS: tuple[type[BaseModel], ...] = (
    PublishThesisRequest,
    PublishedThesis,
    FeedbackPollRequest,
    FeedbackPage,
    ChallengeRequest,
    ChallengeResult,
    ExperimentRequest,
    ExperimentResult,
    AiTraderReplyRequest,
    AiTraderEventPollRequest,
)


def test_ai_trader_adapter_has_no_control_plane_dependency() -> None:
    path = ROOT / "src" / "stonks_agent" / "adapters" / "platform" / "ai_trader.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        token in module
        for module in imports
        for token in (
            "adapters.postgres",
            "domain.orders",
            "domain.reservations",
            "domain.risk",
            "ports.execution",
            "ports.queue",
        )
    )


def test_ai_trader_adapter_structurally_satisfies_research_only_platform_port() -> None:
    adapter = AiTraderHttpAdapter(
        client=object(),  # type: ignore[arg-type]
        artifacts=MemoryArtifactStore(),
        event_inbox=MemoryPlatformEventInbox(),
        secret_provider=ScriptedSecretProvider(("opaque-token", "test-version-1")),
        secret_ref=SecretRef(name="ai_trader_access_token"),
    )

    assert isinstance(adapter, PlatformPort)
    assert not hasattr(adapter, "submit_order")
    assert not hasattr(adapter, "copy_trade")
    assert all("trade" not in endpoint for endpoint in AI_TRADER_ENDPOINT_TEMPLATES)


def test_public_ai_trader_contract_graph_has_no_target_or_execution_authority() -> None:
    unsafe = {
        path
        for path, field_name in _model_field_paths(_PUBLIC_AI_TRADER_MODELS)
        if _is_authority_field(field_name)
    }

    assert unsafe == set()


@pytest.mark.parametrize(
    "field_name",
    [
        "target_agent",
        "agent_id",
        "portfolio_target",
        "order_intent_id",
        "risk_decision_id",
        "execution_command",
    ],
)
def test_public_contract_guard_detects_nested_authority_fields(
    field_name: str,
) -> None:
    assert _is_authority_field(field_name)


def _model_field_paths(
    roots: tuple[type[BaseModel], ...],
) -> frozenset[tuple[str, str]]:
    pending = list(roots)
    visited: set[type[BaseModel]] = set()
    paths: set[tuple[str, str]] = set()
    while pending:
        model = pending.pop()
        if model in visited:
            continue
        visited.add(model)
        for field_name, field in model.model_fields.items():
            paths.add((f"{model.__module__}.{model.__name__}.{field_name}", field_name))
            pending.extend(_nested_models(field.annotation))
    return frozenset(paths)


def _nested_models(annotation: object) -> tuple[type[BaseModel], ...]:
    nested: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        nested.append(annotation)
    for argument in get_args(annotation):
        nested.extend(_nested_models(argument))
    return tuple(nested)


def _is_authority_field(field_name: str) -> bool:
    normalized = field_name.lower()
    segments = frozenset(normalized.split("_"))
    return normalized in _FORBIDDEN_AUTHORITY_FIELDS or bool(
        segments & {"target", "order", "risk", "execution"}
    )
