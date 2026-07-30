from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import SecretStr

from stonks_agent.composition.model_settings import SessionModelSettings
from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelConnectionTest,
)

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
CANARY = "session-only-canary-key"


class ConnectionTester:
    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []
        self.failure: Failure | None = None

    def __call__(
        self,
        environment: dict[str, str],
    ) -> Success[GuiModelConnectionTest] | Failure:
        self.environments.append(environment)
        if self.failure is not None:
            return self.failure
        return Success(
            GuiModelConnectionTest(
                provider_model=environment["STONKS_LLM_MODEL"],
                input_tokens=12,
                output_tokens=3,
                cost_usd=Decimal("0.0001"),
                elapsed_ms=25,
                tested_at=NOW,
            )
        )


def command(**overrides: object) -> ConfigureGuiModelSettings:
    values: dict[str, object] = {
        "base_url": "http://127.0.0.1:11434",
        "endpoint": "/v1/chat/completions",
        "model_id": "local-model",
        "api_key": SecretStr(CANARY),
        "input_cost_per_million": "0",
        "cached_input_cost_per_million": "0",
        "cache_write_input_cost_per_million": "0",
        "output_cost_per_million": "0",
        "max_output_tokens": 4096,
        "max_total_tokens": 32768,
        "max_cost_usd": "1",
        "max_transient_retries": 1,
        "max_repairs": 1,
        "timeout_seconds": "30",
        "max_response_bytes": 1_048_576,
    }
    values.update(overrides)
    return ConfigureGuiModelSettings.model_validate(values)


def test_session_settings_are_unconfigured_without_environment() -> None:
    settings = SessionModelSettings({}, tester=ConnectionTester(), clock=lambda: NOW)

    view = settings.view()

    assert view.state == "unconfigured"
    assert view.source == "none"
    assert view.api_key_configured is False
    assert view.config is None
    assert settings.environment_snapshot() == {"STONKS_ENVIRONMENT": "local"}


def test_successful_structured_probe_atomically_activates_secret_safe_settings() -> (
    None
):
    tester = ConnectionTester()
    settings = SessionModelSettings({}, tester=tester, clock=lambda: NOW)

    result = settings.configure(command())

    assert isinstance(result, Success)
    assert result.value.state == "configured"
    assert result.value.source == "session"
    assert result.value.verified is True
    assert result.value.config is not None
    assert result.value.config.model_id == "local-model"
    assert result.value.connection_test is not None
    assert result.value.connection_test.provider_model == "local-model"
    assert settings.environment_snapshot()["STONKS_LLM_API_KEY"] == CANARY
    assert tester.environments[0]["STONKS_LLM_API_KEY"] == CANARY
    rendered = result.value.model_dump_json()
    assert CANARY not in rendered
    assert CANARY not in repr(settings)
    assert CANARY not in repr(command())


def test_environment_seed_is_verified_without_reentering_the_secret() -> None:
    tester = ConnectionTester()
    settings = SessionModelSettings(
        {
            "STONKS_ENVIRONMENT": "local",
            "STONKS_LLM_BASE_URL": "https://api.example.com",
            "STONKS_LLM_MODEL": "environment-model",
            "STONKS_LLM_API_KEY": "environment-key",
        },
        tester=tester,
        clock=lambda: NOW,
    )

    result = settings.verify_environment()

    assert isinstance(result, Success)
    assert result.value.source == "environment"
    assert result.value.verified is True
    assert result.value.generation == 1
    assert tester.environments[0]["STONKS_LLM_API_KEY"] == "environment-key"


def test_failed_environment_probe_stays_unverified_and_preserves_generation() -> None:
    tester = ConnectionTester()
    tester.failure = Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message="Model provider rejected the request",
        )
    )
    settings = SessionModelSettings(
        {
            "STONKS_ENVIRONMENT": "local",
            "STONKS_LLM_BASE_URL": "https://api.example.com",
            "STONKS_LLM_MODEL": "environment-model",
            "STONKS_LLM_API_KEY": "environment-key",
        },
        tester=tester,
        clock=lambda: NOW,
    )

    result = settings.verify_environment()

    assert isinstance(result, Failure)
    assert settings.view().verified is False
    assert settings.view().generation == 0


def test_failed_probe_keeps_previous_generation_and_never_activates_new_secret() -> (
    None
):
    tester = ConnectionTester()
    settings = SessionModelSettings({}, tester=tester, clock=lambda: NOW)
    first = settings.configure(command())
    assert isinstance(first, Success)
    generation = first.value.generation
    tester.failure = Failure(
        StructuredError(
            code=ErrorCode.UNAUTHORIZED,
            message="Model provider rejected the request",
        )
    )

    failed = settings.configure(
        command(model_id="rejected-model", api_key=SecretStr("rejected-secret"))
    )

    assert isinstance(failed, Failure)
    current = settings.view()
    assert current.generation == generation
    assert current.config is not None
    assert current.config.model_id == "local-model"
    snapshot = settings.environment_snapshot()
    assert snapshot["STONKS_LLM_API_KEY"] == CANARY
    assert "rejected-secret" not in str(failed.error)


def test_clear_removes_session_secret_and_reverts_to_environment_configuration() -> (
    None
):
    baseline = {
        "STONKS_ENVIRONMENT": "local",
        "STONKS_LLM_BASE_URL": "https://api.example.com",
        "STONKS_LLM_MODEL": "environment-model",
        "STONKS_LLM_API_KEY": "environment-key",
    }
    settings = SessionModelSettings(
        baseline,
        tester=ConnectionTester(),
        clock=lambda: NOW,
    )
    configured = settings.configure(command())
    assert isinstance(configured, Success)

    cleared = settings.clear()

    assert isinstance(cleared, Success)
    assert cleared.value.state == "configured"
    assert cleared.value.source == "environment"
    assert cleared.value.verified is False
    assert cleared.value.config is not None
    assert cleared.value.config.model_id == "environment-model"
    assert settings.environment_snapshot()["STONKS_LLM_API_KEY"] == "environment-key"


def test_partial_or_invalid_environment_is_not_exposed_as_configured() -> None:
    settings = SessionModelSettings(
        {"STONKS_LLM_BASE_URL": "https://api.example.com"},
        tester=ConnectionTester(),
        clock=lambda: NOW,
    )

    view = settings.view()

    assert view.state == "unconfigured"
    assert view.source == "none"
    assert view.detail == "Model connection is not configured."
