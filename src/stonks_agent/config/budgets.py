"""Closed loader for production cost and latency budgets."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stonks_agent.domain.errors import ErrorCode, StructuredError
from stonks_agent.domain.operational_budget import (
    BudgetScope,
    OperationalBudgetPolicy,
)


class OperationalBudgetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    budgets: tuple[OperationalBudgetPolicy, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_complete_stable_catalog(self) -> Self:
        if tuple(item.scope for item in self.budgets) != tuple(BudgetScope):
            raise ValueError("budget catalog is incomplete, duplicated, or reordered")
        return self

    def policy_for(self, scope: BudgetScope) -> OperationalBudgetPolicy:
        return next(item for item in self.budgets if item.scope is scope)


class BudgetCatalogLoadError(RuntimeError):
    def __init__(self, error: StructuredError) -> None:
        self.error = error
        super().__init__("Budget configuration is invalid")


def load_budget_catalog(path: Path) -> OperationalBudgetCatalog:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return OperationalBudgetCatalog.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise BudgetCatalogLoadError(
            StructuredError(
                code=ErrorCode.CONFIGURATION_INVALID,
                message="Budget configuration is invalid",
                details={"file": path.name},
            )
        ) from error
