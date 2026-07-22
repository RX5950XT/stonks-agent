from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from stonks_agent.config.resilience import (
    DrillPolicyLoadError,
    load_resilience_drill_policy,
)

POLICY_PATH = Path("config/resilience-drills.yaml")
EXPECTED_DRILLS = (
    "provider_outage",
    "llm_outage",
    "model_outage",
    "sidecar_outage",
    "database_restart",
    "worker_crash",
    "lease_expiry",
    "duplicate_event",
    "artifact_corruption",
    "ledger_mismatch",
    "database_restore",
)


def _payload() -> dict[str, object]:
    loaded = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    target = tmp_path / "resilience-drills.yaml"
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def test_loads_closed_frozen_drill_catalog() -> None:
    policy = load_resilience_drill_policy(POLICY_PATH)

    assert policy.policy_id == "stonks-resilience-drills/1"
    assert policy.execution_mode == "paper"
    assert tuple(item.drill_id for item in policy.drills) == EXPECTED_DRILLS
    assert all(item.forbidden_side_effects for item in policy.drills)
    assert all(item.required_audit_evidence for item in policy.drills)
    assert all(item.required_metric_evidence for item in policy.drills)
    assert all(item.recovery_preconditions for item in policy.drills)
    with pytest.raises(Exception, match="frozen"):
        policy.execution_mode = "live"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ("duplicate", "duplicated"),
        ("unknown_failure", "Input should be"),
        ("unknown_metric_label", "telemetry attribute value is not allowlisted"),
        ("partial", "Field required"),
        ("unsafe_recovery", "literal_error"),
        ("extra", "extra_forbidden"),
    ],
)
def test_loader_rejects_unknown_duplicate_partial_or_unsafe_policy(
    tmp_path: Path,
    mutation: str,
    expected_message: str,
) -> None:
    payload = deepcopy(_payload())
    drills = payload["drills"]
    assert isinstance(drills, list)
    first = drills[0]
    assert isinstance(first, dict)
    if mutation == "duplicate":
        drills[1] = deepcopy(first)
    elif mutation == "unknown_failure":
        first["failure_class"] = "unknown_failure"
    elif mutation == "unknown_metric_label":
        metrics = first["required_metric_evidence"]
        assert isinstance(metrics, list)
        metric = metrics[0]
        assert isinstance(metric, dict)
        labels = metric["labels"]
        assert isinstance(labels, list)
        labels[0] = {"name": "component", "value": "unbounded_component"}
    elif mutation == "partial":
        first.pop("expected_state")
    elif mutation == "unsafe_recovery":
        fail_closed = payload["fail_closed"]
        assert isinstance(fail_closed, dict)
        fail_closed["automatic_recovery"] = True
    else:
        first["unreviewed_action"] = "continue"

    with pytest.raises(DrillPolicyLoadError) as raised:
        load_resilience_drill_policy(_write_policy(tmp_path, payload))

    assert raised.value.error.code.value == "configuration_invalid"
    assert raised.value.error.details == {"file": "resilience-drills.yaml"}
    assert expected_message in str(raised.value.__cause__)


def test_catalog_requires_exact_known_order(tmp_path: Path) -> None:
    payload = _payload()
    drills = payload["drills"]
    assert isinstance(drills, list)
    drills.reverse()

    with pytest.raises(DrillPolicyLoadError) as raised:
        load_resilience_drill_policy(_write_policy(tmp_path, payload))

    assert "incomplete, duplicated, or reordered" in str(raised.value.__cause__)
