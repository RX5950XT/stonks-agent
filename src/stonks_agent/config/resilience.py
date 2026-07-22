"""Strict loader for the machine-readable resilience drill policy."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.domain.resilience import ResilienceDrillPolicy


class DrillPolicyLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Resilience drill policy configuration is invalid")


def load_resilience_drill_policy(path: Path) -> ResilienceDrillPolicy:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ResilienceDrillPolicy.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise DrillPolicyLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Resilience drill policy configuration is invalid",
                details={"file": path.name},
            )
        ) from error
