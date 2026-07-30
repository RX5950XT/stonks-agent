"""Atomic process-memory model settings for the local GUI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Literal
from uuid import uuid4

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore
from stonks_agent.composition.models import (
    LLMCompositionConfig,
    build_llm,
    build_model_http_client,
    build_model_policy,
)
from stonks_agent.domain.clock import utc_now
from stonks_agent.domain.errors import (
    ErrorCode,
    Failure,
    Result,
    StructuredError,
    Success,
)
from stonks_agent.domain.gui_model_settings import (
    ConfigureGuiModelSettings,
    GuiModelConnectionTest,
    GuiModelPublicConfig,
    GuiModelSettingsView,
)
from stonks_agent.domain.research import (
    LLMMessage,
    LLMRole,
    StructuredLLMRequest,
)
from stonks_agent.ports.artifact_store import ArtifactStore

type ModelConnectionTester = Callable[
    [dict[str, str]],
    Result[GuiModelConnectionTest],
]
_MODEL_ENVIRONMENT_KEYS = frozenset(
    {
        "STONKS_ENVIRONMENT",
        "STONKS_LLM_BASE_URL",
        "STONKS_LLM_ENDPOINT",
        "STONKS_LLM_MODEL",
        "STONKS_LLM_API_KEY",
        "STONKS_LLM_INPUT_COST_PER_MILLION",
        "STONKS_LLM_CACHED_INPUT_COST_PER_MILLION",
        "STONKS_LLM_CACHE_WRITE_INPUT_COST_PER_MILLION",
        "STONKS_LLM_OUTPUT_COST_PER_MILLION",
        "STONKS_LLM_MAX_OUTPUT_TOKENS",
        "STONKS_LLM_MAX_TOTAL_TOKENS",
        "STONKS_LLM_MAX_COST_USD",
        "STONKS_LLM_MAX_TRANSIENT_RETRIES",
        "STONKS_LLM_MAX_REPAIRS",
        "STONKS_LLM_TIMEOUT_SECONDS",
        "STONKS_LLM_MAX_RESPONSE_BYTES",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class _ActiveSettings:
    environment: dict[str, str]
    config: LLMCompositionConfig
    connection_test: GuiModelConnectionTest | None
    updated_at: datetime | None


class SessionModelSettings:
    """One lock-protected route+secret generation; values never become durable."""

    __slots__ = (
        "_baseline",
        "_clock",
        "_generation",
        "_lock",
        "_session",
        "_tester",
    )

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        tester: ModelConnectionTester,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or utc_now
        self._tester = tester
        self._lock = Lock()
        self._generation = 0
        self._session: _ActiveSettings | None = None
        self._baseline = _validated_environment(environment)

    def view(self) -> GuiModelSettingsView:
        with self._lock:
            return self._view_locked()

    def configure(
        self,
        command: ConfigureGuiModelSettings,
    ) -> Result[GuiModelSettingsView]:
        environment = _command_environment(command)
        try:
            config = _validated_config(environment)
        except (TypeError, ValueError):
            return _invalid_settings()
        tested = self._tester(dict(environment))
        if isinstance(tested, Failure):
            return tested
        active = _ActiveSettings(
            environment=environment,
            config=config,
            connection_test=tested.value,
            updated_at=self._clock(),
        )
        with self._lock:
            self._session = active
            self._generation += 1
            return Success(self._view_locked())

    def verify_environment(self) -> Result[GuiModelSettingsView]:
        """Probe a valid env seed once so existing launcher configuration still works."""

        with self._lock:
            baseline = self._baseline
        if baseline is None:
            return _invalid_settings()
        tested = self._tester(dict(baseline.environment))
        if isinstance(tested, Failure):
            return tested
        verified = _ActiveSettings(
            environment=baseline.environment,
            config=baseline.config,
            connection_test=tested.value,
            updated_at=self._clock(),
        )
        with self._lock:
            if self._baseline is baseline:
                self._baseline = verified
                self._generation += 1
            return Success(self._view_locked())

    def clear(self) -> Result[GuiModelSettingsView]:
        with self._lock:
            self._session = None
            self._generation += 1
            return Success(self._view_locked())

    def environment_snapshot(self) -> dict[str, str]:
        with self._lock:
            active = self._session or self._baseline
            if active is None:
                return {"STONKS_ENVIRONMENT": "local"}
            return dict(active.environment)

    def _view_locked(self) -> GuiModelSettingsView:
        active = self._session or self._baseline
        if active is None:
            return GuiModelSettingsView(
                state="unconfigured",
                source="none",
                detail="Model connection is not configured.",
                api_key_configured=False,
                verified=False,
                generation=self._generation,
            )
        source: Literal["session", "environment"] = (
            "session" if self._session is not None else "environment"
        )
        verified = active.connection_test is not None
        return GuiModelSettingsView(
            state="configured",
            source=source,
            detail=(
                "Structured model connection verified."
                if verified
                else "Model settings loaded; connection is not verified this session."
            ),
            api_key_configured=True,
            verified=verified,
            generation=self._generation,
            config=_public_config(active.config),
            connection_test=active.connection_test,
            updated_at=active.updated_at,
        )


def build_model_connection_tester(
    *,
    artifacts: ArtifactStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[dict[str, str]], Result[GuiModelConnectionTest]]:
    """Create a bounded real structured-completion probe with pinned networking."""

    selected_clock = clock or utc_now
    selected_artifacts = artifacts

    def test(environment: dict[str, str]) -> Result[GuiModelConnectionTest]:
        try:
            config = _validated_config(environment)
            started_at = selected_clock()
            with build_model_http_client(config) as client:
                llm = build_llm(
                    config,
                    environment=environment,
                    artifacts=selected_artifacts or MemoryArtifactStore(),
                    client=client,
                )
                completed = llm.complete(_probe_request(config, started_at))
            if isinstance(completed, Failure):
                return completed
            elapsed = completed.value.usage.elapsed_ms
            return Success(
                GuiModelConnectionTest(
                    provider_model=completed.value.model,
                    input_tokens=completed.value.usage.input_tokens,
                    output_tokens=completed.value.usage.output_tokens,
                    cost_usd=completed.value.usage.cost_usd,
                    elapsed_ms=elapsed,
                    tested_at=selected_clock(),
                )
            )
        except (OSError, TypeError, ValueError):
            return _invalid_settings()

    return test


def _probe_request(
    config: LLMCompositionConfig,
    now: datetime,
) -> StructuredLLMRequest:
    return StructuredLLMRequest(
        request_id=uuid4(),
        model="policy:research-models-v1",
        messages=(
            LLMMessage(
                role=LLMRole.SYSTEM,
                content="Return only the requested structured connectivity result.",
            ),
            LLMMessage(
                role=LLMRole.USER,
                content='Return {"status":"ok"} to verify structured output support.',
            ),
        ),
        output_schema_name="connection_test",
        output_schema_version="1.0.0",
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {"status": {"type": "string", "const": "ok"}},
        },
        max_output_tokens=min(config.max_output_tokens, 64),
        deadline_at=now + timedelta(seconds=float(config.timeout_seconds)),
    )


def _validated_environment(environment: Mapping[str, str]) -> _ActiveSettings | None:
    selected = {
        key: value
        for key, value in environment.items()
        if key in _MODEL_ENVIRONMENT_KEYS
    }
    selected["STONKS_ENVIRONMENT"] = selected.get("STONKS_ENVIRONMENT", "local")
    try:
        config = _validated_config(selected)
    except (TypeError, ValueError):
        return None
    return _ActiveSettings(
        environment=selected,
        config=config,
        connection_test=None,
        updated_at=None,
    )


def _validated_config(environment: Mapping[str, str]) -> LLMCompositionConfig:
    runtime_environment = environment.get("STONKS_ENVIRONMENT", "local")
    config = LLMCompositionConfig.from_environment(
        environment,
        runtime_environment=runtime_environment,
    )
    build_model_policy(config)
    secret = environment.get("STONKS_LLM_API_KEY")
    if (
        secret is None
        or not secret
        or secret.strip() != secret
        or len(secret) > 4_096
        or any(ord(character) < 33 or ord(character) > 126 for character in secret)
    ):
        raise ValueError("LLM configuration is invalid")
    return config


def _command_environment(command: ConfigureGuiModelSettings) -> dict[str, str]:
    return {
        "STONKS_ENVIRONMENT": "local",
        "STONKS_LLM_BASE_URL": command.base_url,
        "STONKS_LLM_ENDPOINT": command.endpoint,
        "STONKS_LLM_MODEL": command.model_id,
        "STONKS_LLM_API_KEY": command.api_key.get_secret_value(),
        "STONKS_LLM_INPUT_COST_PER_MILLION": str(command.input_cost_per_million),
        "STONKS_LLM_CACHED_INPUT_COST_PER_MILLION": str(
            command.cached_input_cost_per_million
        ),
        "STONKS_LLM_CACHE_WRITE_INPUT_COST_PER_MILLION": str(
            command.cache_write_input_cost_per_million
        ),
        "STONKS_LLM_OUTPUT_COST_PER_MILLION": str(command.output_cost_per_million),
        "STONKS_LLM_MAX_OUTPUT_TOKENS": str(command.max_output_tokens),
        "STONKS_LLM_MAX_TOTAL_TOKENS": str(command.max_total_tokens),
        "STONKS_LLM_MAX_COST_USD": str(command.max_cost_usd),
        "STONKS_LLM_MAX_TRANSIENT_RETRIES": str(command.max_transient_retries),
        "STONKS_LLM_MAX_REPAIRS": str(command.max_repairs),
        "STONKS_LLM_TIMEOUT_SECONDS": str(command.timeout_seconds),
        "STONKS_LLM_MAX_RESPONSE_BYTES": str(command.max_response_bytes),
    }


def _public_config(config: LLMCompositionConfig) -> GuiModelPublicConfig:
    return GuiModelPublicConfig(
        base_url=config.base_url,
        endpoint=config.endpoint,
        model_id=config.model_id,
        input_cost_per_million=config.input_cost_per_million,
        cached_input_cost_per_million=config.cached_input_cost_per_million,
        cache_write_input_cost_per_million=config.cache_write_input_cost_per_million,
        output_cost_per_million=config.output_cost_per_million,
        max_output_tokens=config.max_output_tokens,
        max_total_tokens=config.max_total_tokens,
        max_cost_usd=config.max_cost_usd,
        max_transient_retries=config.max_transient_retries,
        max_repairs=config.max_repairs,
        timeout_seconds=config.timeout_seconds,
        max_response_bytes=config.max_response_bytes,
    )


def _invalid_settings() -> Failure:
    return Failure(
        StructuredError(
            code=ErrorCode.INVALID_INPUT,
            message="Model settings are invalid",
        )
    )
