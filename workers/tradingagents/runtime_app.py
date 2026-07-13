"""Production entrypoint: one immutable runtime profile per process."""

from __future__ import annotations

import os

import httpx

from workers.tradingagents.adapter import (
    TradingAgentsWorker,
    WorkerPolicy,
    validate_worker_environment,
)
from workers.tradingagents.app import create_app
from workers.tradingagents.artifacts import FixedOriginArtifactResolver
from workers.tradingagents.runtime import PinnedTradingAgentsRuntime

environment = validate_worker_environment(os.environ)
policy = WorkerPolicy(
    profile=environment.profile,
    selected_analysts=("market", "fundamentals", "news", "social"),
    max_evidence_bytes=1_048_576,
    network_egress="deny",
)
runtime = PinnedTradingAgentsRuntime(selected_analysts=policy.selected_analysts)
artifact_client = httpx.Client()
artifacts = FixedOriginArtifactResolver(
    client=artifact_client,
    origin=os.environ.get("STONKS_ARTIFACT_ORIGIN", "http://artifact-service:8080"),
    max_bytes=policy.max_evidence_bytes,
    timeout_seconds=5,
)
app = create_app(
    worker=TradingAgentsWorker(policy=policy, runtime=runtime, artifacts=artifacts)
)
