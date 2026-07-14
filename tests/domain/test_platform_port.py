from __future__ import annotations

from stonks_agent.domain.errors import Result
from stonks_agent.ports.platform import PlatformPort
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


class ExternalPlatformAdapter:
    def publish_thesis(self, request: PublishThesisRequest) -> Result[PublishedThesis]:
        raise NotImplementedError

    def poll_feedback(self, request: FeedbackPollRequest) -> Result[FeedbackPage]:
        raise NotImplementedError

    def submit_challenge(self, request: ChallengeRequest) -> Result[ChallengeResult]:
        raise NotImplementedError

    def submit_experiment(self, request: ExperimentRequest) -> Result[ExperimentResult]:
        raise NotImplementedError


def test_platform_port_is_runtime_checkable_and_capability_narrow() -> None:
    adapter = ExternalPlatformAdapter()

    assert isinstance(adapter, PlatformPort)
    operations = {
        name
        for name, value in vars(PlatformPort).items()
        if not name.startswith("_") and callable(value)
    }
    assert operations == {
        "publish_thesis",
        "poll_feedback",
        "submit_challenge",
        "submit_experiment",
    }
    assert not hasattr(adapter, "submit_order")
    assert not hasattr(adapter, "copy_trade")
