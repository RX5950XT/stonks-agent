"""Production entrypoint: one immutable runtime profile per process."""

from __future__ import annotations

import os

from workers.tradingagents.adapter import (
    TradingAgentsWorker,
    WorkerPolicy,
    validate_worker_environment,
)
from workers.tradingagents.app import create_app
from workers.tradingagents.runtime import PinnedTradingAgentsRuntime

environment = validate_worker_environment(os.environ)
policy = WorkerPolicy(
    profile=environment.profile,
    selected_analysts=("market", "fundamentals", "news", "social"),
    max_evidence_bytes=1_048_576,
    network_egress="deny",
)
runtime = PinnedTradingAgentsRuntime(selected_analysts=policy.selected_analysts)
app = create_app(worker=TradingAgentsWorker(policy=policy, runtime=runtime))
