"""Bounded single-host capacity evidence; never a production SLA benchmark."""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Engine, create_engine

from scripts.capacity_probe_common import ProbeError
from scripts.capacity_probe_database import (
    DatabaseProbe,
    validate_capacity_database_url,
)
from scripts.capacity_probe_local import (
    asgi_security_contract_once,
    forecast_contract_once,
    paper_cycle_once,
)
from scripts.capacity_probe_measurement import measure_capacity_workload
from stonks_agent.config.capacity import load_capacity_policy
from stonks_agent.domain.capacity import (
    CapacityPolicy,
    CapacityReport,
    CapacityVerification,
    CapacityWorkload,
    CapacityWorkloadReport,
    verify_capacity_report,
)

_DATABASE_ENV = "STONKS_CAPACITY_DATABASE_URL"
_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _ROOT / "config" / "capacity.yaml"


def _parse_output(argv: Sequence[str]) -> Path:
    if len(argv) != 2 or argv[0] != "--output":
        raise ProbeError("capacity probe arguments are invalid")
    raw = argv[1]
    if not raw or len(raw) > 4096 or "\x00" in raw:
        raise ProbeError("capacity report path is invalid")
    path = Path(raw)
    if path.suffix.lower() != ".json" or path.is_symlink() or not path.parent.exists():
        raise ProbeError("capacity report path is invalid")
    return path


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > 1_048_576:
        raise ProbeError("capacity report exceeded size limit")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ProbeError("capacity report path is invalid")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_capacity_probe(
    policy: CapacityPolicy, database_url: str
) -> tuple[CapacityReport, CapacityVerification]:
    """Measure the closed catalog, then re-compute every claim in domain code."""
    engine = _capacity_engine(policy, database_url)
    identity = secrets.token_hex(8)
    probe = DatabaseProbe(engine, identity=identity)
    reports: list[CapacityWorkloadReport] = []
    try:
        probe.prepare()
        operations = {
            CapacityWorkload.API: asgi_security_contract_once,
            CapacityWorkload.QUEUE: probe.queue_once,
            CapacityWorkload.SNAPSHOT: probe.snapshot_once,
            CapacityWorkload.RESEARCH: probe.research_once,
            CapacityWorkload.FORECAST: forecast_contract_once,
            CapacityWorkload.PAPER_CYCLE: paper_cycle_once,
        }
        for definition in policy.workloads:
            reports.append(
                measure_capacity_workload(
                    policy,
                    definition,
                    operations[definition.workload],
                )
            )
    finally:
        probe.verify_evidence_scope()
        engine.dispose()
    report = CapacityReport(
        schema_version=1,
        policy_id=policy.policy_id,
        report_id=f"capacity_{identity}",
        execution_mode="paper",
        scope=policy.scope,
        resource_observation_scope=policy.resource_observation_scope,
        workloads=tuple(reports),
    )
    return report, verify_capacity_report(policy, report)


def _capacity_engine(policy: CapacityPolicy, database_url: str) -> Engine:
    parsed = validate_capacity_database_url(database_url)
    maximum_concurrency = max(item.concurrency for item in policy.workloads)
    return create_engine(
        parsed,
        pool_pre_ping=True,
        pool_size=maximum_concurrency,
        max_overflow=0,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=5000 -c lock_timeout=2000",
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    selected = tuple(sys.argv[1:] if argv is None else argv)
    try:
        output = _parse_output(selected)
        database_url = os.environ.get(_DATABASE_ENV)
        if database_url is None:
            raise ProbeError("capacity database configuration is missing")
        policy = load_capacity_policy(_POLICY_PATH)
        report, verification = run_capacity_probe(policy, database_url)
        _atomic_write(output, report.model_dump(mode="json"))
        if not verification.passed:
            print("capacity probe failed", file=sys.stderr)
            return 1
    except Exception:
        print("capacity probe failed", file=sys.stderr)
        return 2
    print("capacity probe completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
