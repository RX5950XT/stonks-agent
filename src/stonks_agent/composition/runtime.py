"""Shared local resources owned by executable composition roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import Engine, create_engine

from stonks_agent.adapters.artifacts.local import LocalArtifactStore
from stonks_agent.ports.telemetry import OperationRecorderPort


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    engine: Engine
    artifacts: LocalArtifactStore
    http_client: httpx.Client
    telemetry: OperationRecorderPort | None = None

    def close(self) -> None:
        self.http_client.close()
        self.engine.dispose()


def build_local_runtime(
    *,
    database_url: str,
    artifact_root: Path = Path(".data/artifacts"),
    telemetry: OperationRecorderPort | None = None,
) -> LocalRuntime:
    return LocalRuntime(
        engine=create_engine(database_url, pool_pre_ping=True),
        artifacts=LocalArtifactStore(artifact_root),
        http_client=httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30),
        ),
        telemetry=telemetry,
    )
