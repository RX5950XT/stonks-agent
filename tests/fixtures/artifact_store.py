from __future__ import annotations

from typing import Never

from stonks_agent.adapters.artifacts.memory import MemoryArtifactStore


class FailOnFinalizeArtifactStore(MemoryArtifactStore):
    """Prove a failure path performs no artifact write."""

    def finalize(
        self,
        content: object,
        *,
        metadata: object,
        finalized_at: object,
    ) -> Never:
        del content, metadata, finalized_at
        raise AssertionError("artifact finalization must not be called")
