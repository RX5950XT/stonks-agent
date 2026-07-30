from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from stonks_agent.domain.errors import ErrorCode, Failure, StructuredError, Success
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelConnectionTest,
    GuiModelPublicConfig,
    GuiModelSettingsView,
)
from stonks_agent.domain.latest_market_data import (
    LatestMarketDataQuery,
)
from stonks_agent.entrypoints.api.gui import create_gui_app
from stonks_agent.entrypoints.api.gui_research import GuiResearchApiOptions

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
CANARY = "browser-session-canary-key"
TOKEN = "intent-" + "a" * 32


class Source:
    def fetch(
        self,
        query: LatestMarketDataQuery,
        *,
        observed_at: datetime,
    ) -> Failure:
        del query, observed_at
        return Failure(
            StructuredError(
                code=ErrorCode.DATA_UNAVAILABLE,
                message="Market data is unavailable",
            )
        )


class ModelSettings:
    def __init__(self) -> None:
        self.commands: list[ConfigureGuiModelSettings] = []
        self.clear_calls = 0
        self.current = unconfigured()

    def view(self) -> GuiModelSettingsView:
        return self.current

    def configure(
        self,
        command: ConfigureGuiModelSettings,
    ) -> Success[GuiModelSettingsView]:
        self.commands.append(command)
        self.current = configured(command)
        return Success(self.current)

    def clear(self) -> Success[GuiModelSettingsView]:
        self.clear_calls += 1
        self.current = unconfigured(generation=self.current.generation + 1)
        return Success(self.current)


def unconfigured(*, generation: int = 0) -> GuiModelSettingsView:
    return GuiModelSettingsView(
        state="unconfigured",
        source="none",
        detail="Model connection is not configured.",
        api_key_configured=False,
        verified=False,
        generation=generation,
    )


def configured(command: ConfigureGuiModelSettings) -> GuiModelSettingsView:
    return GuiModelSettingsView(
        state="configured",
        source="session",
        detail="Structured model connection verified.",
        api_key_configured=True,
        verified=True,
        generation=1,
        config=GuiModelPublicConfig.model_validate(
            command.model_dump(exclude={"api_key"})
        ),
        connection_test=GuiModelConnectionTest(
            provider_model=command.model_id,
            input_tokens=10,
            output_tokens=2,
            cost_usd=Decimal("0"),
            elapsed_ms=20,
            tested_at=NOW,
        ),
        updated_at=NOW,
    )


def body(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "base_url": "http://127.0.0.1:11434",
        "endpoint": "/v1/chat/completions",
        "model_id": "local-model",
        "api_key": CANARY,
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
    return values


def headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:8787",
        "X-Stonks-Intent": TOKEN,
    }


def client(settings: object | None) -> TestClient:
    app = create_gui_app(
        Source(),
        clock=lambda: NOW,
        model_settings=settings,  # type: ignore[arg-type]
        research_api=GuiResearchApiOptions(intent_token=TOKEN),
    )
    return TestClient(
        app,
        base_url="http://127.0.0.1:8787",
        client=("127.0.0.1", 50_000),
    )


def test_model_settings_get_and_capability_never_return_secret() -> None:
    settings = ModelSettings()
    settings.configure(ConfigureGuiModelSettings.model_validate(body()))

    with client(settings) as browser:
        status = browser.get("/api/v1/settings/llm")
        capabilities = browser.get("/api/v1/capabilities")

    rendered = status.text + capabilities.text
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["data"]["state"] == "configured"
    assert CANARY not in rendered
    assert f'"api_key":"{CANARY}"' not in rendered


def test_model_settings_put_validates_tests_and_returns_only_safe_metadata() -> None:
    settings = ModelSettings()

    with client(settings) as browser:
        response = browser.put(
            "/api/v1/settings/llm",
            headers=headers(),
            json=body(),
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["verified"] is True
    assert response.json()["data"]["config"]["model_id"] == "local-model"
    assert CANARY not in response.text
    assert settings.commands[0].api_key.get_secret_value() == CANARY


def test_model_settings_delete_clears_only_session_configuration() -> None:
    settings = ModelSettings()
    settings.configure(ConfigureGuiModelSettings.model_validate(body()))

    with client(settings) as browser:
        response = browser.request(
            "DELETE",
            "/api/v1/settings/llm",
            headers=headers(),
            content="{}",
        )

    assert response.status_code == 200
    assert response.json()["data"]["state"] == "unconfigured"
    assert settings.clear_calls == 1
    assert CANARY not in response.text


def test_model_settings_mutations_reject_cross_origin_unknown_and_missing_intent() -> (
    None
):
    settings = ModelSettings()
    attempts = (
        ({**headers(), "Origin": "https://attacker.invalid"}, body()),
        (
            {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8787"},
            body(),
        ),
        (headers(), body(order_side="buy")),
    )

    with client(settings) as browser:
        responses = tuple(
            browser.put("/api/v1/settings/llm", headers=value, json=payload)
            for value, payload in attempts
        )

    assert all(response.status_code in {400, 403} for response in responses)
    assert settings.commands == []


def test_model_settings_update_has_bounded_cost_rate_limit() -> None:
    settings = ModelSettings()

    with client(settings) as browser:
        responses = tuple(
            browser.put("/api/v1/settings/llm", headers=headers(), json=body())
            for _ in range(4)
        )

    assert tuple(response.status_code for response in responses) == (200, 200, 200, 429)
    assert responses[-1].headers["retry-after"] == "60"
    assert len(settings.commands) == 3


def test_model_settings_routes_are_stable_but_fail_closed_without_composition() -> None:
    with client(None) as browser:
        status = browser.get("/api/v1/settings/llm")
        mutation = browser.put(
            "/api/v1/settings/llm",
            headers=headers(),
            json=body(),
        )

    assert status.status_code == 503
    assert mutation.status_code == 503
    assert status.json()["error"]["code"] == "data_unavailable"
