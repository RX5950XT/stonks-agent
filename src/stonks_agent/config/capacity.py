"""Strict loader for the machine-readable paper capacity policy."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from stonks_agent.domain.capacity import CapacityPolicy
from stonks_agent.domain.errors import ErrorCode, StructuredError


class CapacityPolicyLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Capacity policy configuration is invalid")


def load_capacity_policy(path: Path) -> CapacityPolicy:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return CapacityPolicy.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise CapacityPolicyLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Capacity policy configuration is invalid",
                details={"file": path.name},
            )
        ) from error
