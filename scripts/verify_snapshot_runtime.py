"""Materialize one live OpenBB snapshot through the durable worker."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from stonks_agent.adapters.postgres.models import (
    DatasetSnapshotEvidenceRow,
    DatasetSnapshotRow,
    EvidenceItemRow,
    RunDatasetSnapshotRow,
)
from stonks_agent.adapters.postgres.snapshot_requests import (
    PostgresSnapshotRequestStore,
)
from stonks_agent.application.data.create_snapshot import request_snapshot
from stonks_agent.composition.runtime import build_local_runtime
from stonks_agent.composition.worker import build_worker_composition
from stonks_agent.domain.auth import Role
from stonks_agent.domain.errors import Failure, Success
from stonks_agent.domain.snapshot import CreateSnapshotRequest
from stonks_agent.entrypoints.cli_commands._local_auth import local_cli_principal
from stonks_agent.entrypoints.gui import (
    OpenBBSidecarManager,
    prepare_ephemeral_openbb_runtime,
)
from stonks_agent.entrypoints.worker import run_worker_once


def main() -> None:
    arguments = _arguments()
    root = arguments.root.resolve(strict=True)
    requested_at = datetime.now(UTC)
    as_of = requested_at + timedelta(minutes=15)
    end_date = requested_at.date() - timedelta(days=1)
    start_date = end_date - timedelta(days=45)
    with tempfile.TemporaryDirectory(prefix="stonks-openbb-verify-") as raw:
        ephemeral = prepare_ephemeral_openbb_runtime(Path(raw) / "auth")
        manager = OpenBBSidecarManager(
            root=root,
            environment=ephemeral.environment,
        )
        manager.start()
        runtime = build_local_runtime(
            database_url=arguments.database_url,
            artifact_root=Path(raw) / "artifacts",
        )
        try:
            command = CreateSnapshotRequest(
                market="US",
                capability="prices",
                as_of=as_of,
                query={
                    "symbol": arguments.symbol,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "interval": "1d",
                },
                provider_policy_id="us-prices/1",
                idempotency_key=(
                    f"actual-{arguments.symbol.lower()}-{requested_at:%Y%m%d%H%M%S}"
                ),
                owner_subject="local-cli",
                requested_at=requested_at,
            )
            submitted = request_snapshot(
                local_cli_principal(
                    subject="local-cli",
                    role=Role.RESEARCHER,
                ),
                command,
                PostgresSnapshotRequestStore(runtime.engine),
            )
            if isinstance(submitted, Failure):
                raise RuntimeError(
                    f"Snapshot submit failed closed: {submitted.error.code.value}"
                )
            composition = build_worker_composition(
                runtime,
                environment={"STONKS_ENVIRONMENT": "local"},
                root=root,
                credentials=ephemeral.credentials,
            )
            processed = run_worker_once(
                composition.queue,
                handlers=composition.handlers,
                worker_id="actual-openbb-worker",
                now=datetime.now(UTC),
                lease_for=timedelta(seconds=60),
            )
            if not isinstance(processed, Success) or not processed.value:
                code = (
                    processed.error.code.value
                    if isinstance(processed, Failure)
                    else "not_processed"
                )
                raise RuntimeError(f"Snapshot worker failed closed: {code}")
            output = _snapshot_projection(
                runtime.engine,
                submitted.value.run_id,
            )
            print(
                json.dumps(
                    {
                        "success": True,
                        "status": 200,
                        "data": output,
                        "error": None,
                        "metadata": {
                            "provider": "openbb_rest",
                            "provider_backend": "yfinance",
                            "fixture_fallback": False,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        finally:
            runtime.close()
            manager.stop()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        required=True,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--symbol", default="AAPL")
    arguments = parser.parse_args()
    normalized = arguments.symbol.strip().upper()
    if (
        not normalized
        or len(normalized) > 16
        or any(
            not (character.isalnum() or character in ".-") for character in normalized
        )
    ):
        parser.error("symbol is invalid")
    arguments.symbol = normalized
    return arguments


def _snapshot_projection(engine: Engine, run_id: UUID) -> dict[str, object]:
    with Session(engine) as session:
        snapshot = session.scalar(
            select(DatasetSnapshotRow)
            .join(
                RunDatasetSnapshotRow,
                RunDatasetSnapshotRow.snapshot_id == DatasetSnapshotRow.snapshot_id,
            )
            .where(RunDatasetSnapshotRow.run_id == run_id)
        )
        if snapshot is None:
            raise RuntimeError("Snapshot result is missing")
        evidence_count = session.scalar(
            select(func.count())
            .select_from(DatasetSnapshotEvidenceRow)
            .where(DatasetSnapshotEvidenceRow.snapshot_id == snapshot.snapshot_id)
        )
        available_at = session.scalar(
            select(func.max(EvidenceItemRow.available_at))
            .join(
                DatasetSnapshotEvidenceRow,
                DatasetSnapshotEvidenceRow.evidence_id == EvidenceItemRow.evidence_id,
            )
            .where(DatasetSnapshotEvidenceRow.snapshot_id == snapshot.snapshot_id)
        )
        return {
            "run_id": str(run_id),
            "snapshot_id": str(snapshot.snapshot_id),
            "evidence_count": int(evidence_count or 0),
            "as_of": snapshot.as_of.isoformat(),
            "cutoff_at": snapshot.cutoff_at.isoformat(),
            "latest_available_at": (
                available_at.isoformat() if available_at is not None else None
            ),
        }


if __name__ == "__main__":
    main()
