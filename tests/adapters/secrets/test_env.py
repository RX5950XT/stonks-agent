from __future__ import annotations

from stonks_agent.adapters.secrets.env import EnvSecretProvider
from stonks_agent.domain.errors import ErrorCode, Failure, Success
from stonks_agent.domain.secrets import SecretAccessRequest, SecretRef

REQUEST = SecretAccessRequest(
    reference=SecretRef(name="openai_api_key"),
    purpose="llm.openai",
)


def test_env_provider_resolves_exact_binding_and_observes_rotation() -> None:
    environment = {"OPENAI_API_KEY": "first-sensitive-value"}
    provider = EnvSecretProvider(
        runtime_environment="development",
        environment=environment,
        bindings={REQUEST: "OPENAI_API_KEY"},
    )

    first = provider.resolve(REQUEST)
    environment["OPENAI_API_KEY"] = "second-sensitive-value"
    second = provider.resolve(REQUEST)

    assert isinstance(first, Success)
    assert first.value.reveal() == "first-sensitive-value"
    assert first.value.version == "env:OPENAI_API_KEY"
    assert isinstance(second, Success)
    assert second.value.reveal() == "second-sensitive-value"


def test_env_provider_requires_exact_reference_and_purpose_without_fallback() -> None:
    provider = EnvSecretProvider(
        runtime_environment="test",
        environment={"OPENAI_API_KEY": "sensitive-value"},
        bindings={REQUEST: "OPENAI_API_KEY"},
    )
    wrong_purpose = REQUEST.model_copy(update={"purpose": "llm.anthropic"})
    wrong_reference = REQUEST.model_copy(
        update={"reference": SecretRef(name="anthropic_api_key")}
    )

    for result in (provider.resolve(wrong_purpose), provider.resolve(wrong_reference)):
        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CONFIGURATION_INVALID
        assert "sensitive-value" not in repr(result.error)


def test_env_provider_rejects_missing_or_invalid_value_without_disclosure() -> None:
    for value in (
        None,
        "",
        "   ",
        " leading",
        "trailing ",
        "line\nbreak",
        "contains\x00nul",
        "unicode\u0085control",
    ):
        environment = {} if value is None else {"OPENAI_API_KEY": value}
        provider = EnvSecretProvider(
            runtime_environment="local",
            environment=environment,
            bindings={REQUEST: "OPENAI_API_KEY"},
        )

        result = provider.resolve(REQUEST)

        assert isinstance(result, Failure)
        assert result.error.code is ErrorCode.CONFIGURATION_INVALID
        if value:
            assert value not in repr(result.error)


def test_env_provider_repr_does_not_disclose_current_value() -> None:
    secret = "env-sensitive-value"
    provider = EnvSecretProvider(
        runtime_environment="local",
        environment={"OPENAI_API_KEY": secret},
        bindings={REQUEST: "OPENAI_API_KEY"},
    )

    assert secret not in repr(provider)
