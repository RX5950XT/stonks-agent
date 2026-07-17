from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks_agent.config.settings import (
    ExecutionMode,
    SecretRef,
    Settings,
    SettingsLoadError,
    load_settings,
)
from stonks_agent.domain.errors import ErrorCode

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["live", "backtest", "unknown", "PAPER"])
def test_non_paper_execution_modes_fail_fast(mode: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"execution_mode": mode})


def test_paper_is_the_only_execution_mode() -> None:
    settings = Settings()

    assert settings.execution_mode is ExecutionMode.PAPER


def test_settings_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"execution_mode": "paper", "enable_live": True})


def test_secret_refs_are_named_and_never_serialized() -> None:
    settings = Settings(secret_refs={"llm": SecretRef(name="openai_api_key")})

    rendered = (
        repr(settings) + settings.model_dump_json() + repr(settings.safe_snapshot())
    )
    assert "openai_api_key" not in rendered
    assert "secret_refs" not in settings.model_dump()


@pytest.mark.parametrize("value", ["UPPERCASE", "../secret", "A B", ""])
def test_secret_ref_accepts_only_transport_neutral_names(value: str) -> None:
    with pytest.raises(ValidationError):
        SecretRef(name=value)


def test_committed_defaults_are_valid_and_paper_only() -> None:
    settings = load_settings(ROOT / "config" / "defaults.toml")
    paper_policy = (ROOT / "config" / "policies" / "paper.yaml").read_text(
        encoding="utf-8"
    )

    assert settings.execution_mode is ExecutionMode.PAPER
    assert "mode: paper" in paper_policy
    assert "mode: live" not in paper_policy


def test_invalid_startup_config_raises_structured_error(tmp_path: Path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text('execution_mode = "live"\n', encoding="utf-8")

    with pytest.raises(SettingsLoadError) as raised:
        load_settings(config)

    assert raised.value.error.code is ErrorCode.CONFIGURATION_INVALID
    assert "live" not in str(raised.value)
