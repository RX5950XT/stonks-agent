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
        "secret_ref": {"name": "test_openai_api_key"},
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


@pytest.mark.parametrize("environment", ["local", "development"])
@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:11434",
        "http://localhost:1234",
    ],
)
def test_local_development_routes_allow_exact_loopback_http(
    environment: str,
    origin: str,
) -> None:
    value = route(origin=origin, environment=environment)

    assert value.origin == origin


@pytest.mark.parametrize(
    ("environment", "origin"),
    [
        ("production", "http://127.0.0.1:11434"),
        ("staging", "http://localhost:1234"),
        ("test", "http://127.0.0.1:11434"),
        ("local", "http://0.0.0.0:11434"),
        ("development", "http://127.0.0.2:11434"),
        ("local", "http://localhost"),
        ("local", "http://localhost:11434/path"),
    ],
)
def test_plaintext_model_origin_is_restricted_to_local_loopback_port(
    environment: str,
    origin: str,
) -> None:
    with pytest.raises(ValidationError):
        route(origin=origin, environment=environment)


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
    assert "test_openai_api_key" not in repr(value)
    assert "test_openai_api_key" not in str(value.secret_ref)


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
