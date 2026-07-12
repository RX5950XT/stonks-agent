from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks_agent.domain.model_policy import (
    ModelPolicy,
    ModelProvider,
    ModelRoute,
    load_model_policy,
)


def route(**overrides: object) -> ModelRoute:
    values: dict[str, object] = {
        "request_model": "policy:test-openai",
        "provider": ModelProvider.OPENAI_COMPATIBLE,
        "provider_model": "gpt-test-2026-01-01",
        "origin": "https://api.example.test",
        "endpoint": "/v1/chat/completions",
        "secret_ref": {"environment_variable": "TEST_OPENAI_API_KEY"},
        "input_cost_per_million": Decimal("1.25"),
        "cached_input_cost_per_million": Decimal("0.625"),
        "cache_write_input_cost_per_million": Decimal("1.25"),
        "output_cost_per_million": Decimal("5"),
        "max_output_tokens": 4096,
        "max_total_tokens_per_request": 20_000,
        "max_cost_usd_per_request": Decimal("1"),
        "max_transient_retries": 2,
        "max_repairs": 1,
        "timeout_seconds": Decimal("15"),
        "max_response_bytes": 1_048_576,
    }
    values.update(overrides)
    return ModelRoute.model_validate(values)


def test_checked_in_policy_is_frozen_and_resolves_only_allowlisted_models() -> None:
    policy = load_model_policy(Path("config/models.yaml"))

    default = policy.resolve("policy:models-v1")
    assert default.provider is ModelProvider.FAKE
    assert default.provider_model == "fake-structured-v1"
    assert policy.resolve("policy:openai-research-v1").origin == (
        "https://api.openai.com"
    )
    assert policy.resolve("policy:anthropic-research-v1").endpoint == "/v1/messages"
    with pytest.raises(LookupError, match="not allowlisted"):
        policy.resolve("gpt-attacker-controlled")
    with pytest.raises(ValidationError):
        default.max_repairs = 99  # type: ignore[misc]


def test_policy_rejects_duplicate_request_models() -> None:
    duplicated = route()

    with pytest.raises(ValidationError, match="request models must be unique"):
        ModelPolicy(policy_id="test-v1", routes=(duplicated, duplicated))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "http://api.example.test"),
        ("origin", "https://user:password@api.example.test"),
        ("origin", "https://api.example.test/path"),
        ("endpoint", "//attacker.test/v1/messages"),
        ("endpoint", "https://attacker.test/v1/messages"),
        ("max_transient_retries", 4),
        ("max_repairs", 3),
        ("max_response_bytes", 0),
        ("timeout_seconds", Decimal("0")),
        ("max_cost_usd_per_request", Decimal("-0.01")),
    ],
)
def test_remote_route_rejects_unsafe_or_unbounded_configuration(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        route(**{field: value})


def test_provider_specific_route_shape_and_secret_repr_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="remote model route"):
        route(secret_ref=None)
    with pytest.raises(ValidationError, match="fake model route"):
        route(
            provider=ModelProvider.FAKE,
            origin="https://api.example.test",
            endpoint="/v1/messages",
            secret_ref=None,
        )

    value = route()
    assert "TEST_OPENAI_API_KEY" not in repr(value)
    assert "TEST_OPENAI_API_KEY" not in str(value.secret_ref)


def test_loader_maps_io_yaml_and_validation_failures_to_safe_value_error(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "models.yaml"
    malformed.write_text("routes: [", encoding="utf-8")

    with pytest.raises(ValueError, match="could not be loaded") as malformed_error:
        load_model_policy(malformed)
    with pytest.raises(ValueError, match="could not be loaded") as missing_error:
        load_model_policy(tmp_path / "missing.yaml")

    assert "routes: [" not in str(malformed_error.value)
    assert str(tmp_path) not in str(missing_error.value)
