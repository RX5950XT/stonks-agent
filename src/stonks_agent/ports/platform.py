"""Research-only boundary for optional external community platforms."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stonks_agent.domain.errors import Result
from stonks_contracts.common import Sha256
from stonks_contracts.platform import (
    ChallengeRequest,
    ChallengeResult,
    ExperimentRequest,
    ExperimentResult,
    FeedbackPage,
    FeedbackPollRequest,
    PublishedThesis,
    PublishThesisRequest,
)


@runtime_checkable
class PlatformEventInboxPort(Protocol):
    def accept(self, event_id: str, payload_hash: Sha256) -> Result[bool]: ...


@runtime_checkable
class PlatformPort(Protocol):
    def publish_thesis(
        self, request: PublishThesisRequest
    ) -> Result[PublishedThesis]: ...

    def poll_feedback(self, request: FeedbackPollRequest) -> Result[FeedbackPage]: ...

    def submit_challenge(
        self, request: ChallengeRequest
    ) -> Result[ChallengeResult]: ...

    def submit_experiment(
        self, request: ExperimentRequest
    ) -> Result[ExperimentResult]: ...
