from __future__ import annotations

from pathlib import Path

import pytest

from stonks_agent.config.budgets import (
    BudgetCatalogLoadError,
    load_budget_catalog,
)
from stonks_agent.domain.errors import ErrorCode
from stonks_agent.domain.operational_budget import BudgetScope, BudgetStatus

ROOT = Path(__file__).parents[2]


def test_repository_budget_catalog_is_closed_and_complete() -> None:
    catalog = load_budget_catalog(ROOT / "config" / "budgets.yaml")

    assert catalog.schema_version == 1
    assert tuple(item.scope for item in catalog.budgets) == tuple(BudgetScope)
    assert catalog.policy_for(BudgetScope.RESEARCH).degraded.action is (
        BudgetStatus.DEGRADED
    )
    assert catalog.policy_for(BudgetScope.PAPER_CYCLE).failed.action is (
        BudgetStatus.FAILED
    )


@pytest.mark.parametrize(
    "yaml_text",
    (
        """
schema_version: 1
unknown: true
budgets: []
""",
        """
schema_version: 1
budgets:
  - scope: research
    degraded: {action: retry, max_cost_usd: "2", max_elapsed_seconds: "30"}
    failed: {action: failed, max_cost_usd: "5", max_elapsed_seconds: "60"}
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
""",
        """
schema_version: 1
budgets:
  - scope: research
    degraded:
      action: degraded
      max_cost_usd: "2"
      max_elapsed_seconds: "30"
      unknown: true
    failed: {action: failed, max_cost_usd: "5", max_elapsed_seconds: "60"}
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
""",
        """
schema_version: 1
budgets:
  - scope: research
    degraded: {action: degraded, max_cost_usd: "1000000.01", max_elapsed_seconds: "30"}
    failed: {action: failed, max_cost_usd: "1000000.02", max_elapsed_seconds: "60"}
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
""",
        """
schema_version: 1
budgets:
  - scope: research
    degraded: {action: degraded, max_cost_usd: "2", max_elapsed_seconds: "86400.01"}
    failed: {action: failed, max_cost_usd: "5", max_elapsed_seconds: "86400.02"}
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
""",
        """
schema_version: 1
budgets:
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
  - scope: research
    degraded: {action: degraded, max_cost_usd: "2", max_elapsed_seconds: "30"}
    failed: {action: failed, max_cost_usd: "5", max_elapsed_seconds: "60"}
""",
        """
schema_version: 1
budgets:
  - scope: research
    degraded: {action: degraded, max_cost_usd: 2, max_elapsed_seconds: "30"}
    failed: {action: failed, max_cost_usd: "5", max_elapsed_seconds: "60"}
  - scope: paper_cycle
    degraded: {action: degraded, max_cost_usd: "1", max_elapsed_seconds: "10"}
    failed: {action: failed, max_cost_usd: "2", max_elapsed_seconds: "20"}
""",
    ),
)
def test_loader_rejects_unknown_actions_fields_unbounded_values_and_drift(
    tmp_path: Path,
    yaml_text: str,
) -> None:
    path = tmp_path / "budgets.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(BudgetCatalogLoadError) as caught:
        load_budget_catalog(path)

    assert caught.value.error.code is ErrorCode.CONFIGURATION_INVALID
    assert caught.value.error.details == {"file": "budgets.yaml"}


def test_loader_fails_closed_for_missing_or_invalid_yaml(tmp_path: Path) -> None:
    paths = (tmp_path / "missing.yaml", tmp_path / "invalid.yaml")
    paths[1].write_text("budgets: [", encoding="utf-8")

    for path in paths:
        with pytest.raises(BudgetCatalogLoadError):
            load_budget_catalog(path)
