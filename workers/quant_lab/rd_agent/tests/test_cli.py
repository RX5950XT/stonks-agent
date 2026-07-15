from __future__ import annotations

from datetime import timedelta

from stonks_contracts.rd_agent import RDSandboxInvocation
from workers.quant_lab.rd_agent.cli import RuntimeSettings, process_request

from .test_adapter import INSTANCE_ID, FakeRunner, job, runtime, sandbox_policy


def settings() -> RuntimeSettings:
    return RuntimeSettings(
        max_request_bytes=16_777_216,
        runtime=runtime(),
        sandbox=sandbox_policy(),
    )


def test_one_shot_cli_uses_standard_envelope_and_fenced_receipt() -> None:
    invocation = RDSandboxInvocation(
        sandbox_instance_id=INSTANCE_ID,
        job=job(),
    )

    payload = process_request(
        invocation.canonical_json().encode("utf-8"),
        settings(),
        runner=FakeRunner(),
        clock=lambda: job().requested_at + timedelta(minutes=1),
        platform_name=lambda: "Linux",
    )

    assert payload["success"] is True
    assert payload["status"] == 200
    assert payload["error"] is None
    assert payload["metadata"] is None
    assert payload["data"]["result"]["sandbox_instance_id"] == str(  # type: ignore[index]
        INSTANCE_ID
    )


def test_cli_rejects_invalid_or_oversized_body_without_echo() -> None:
    secret = b'{"token":"must-not-leak"}'
    invalid = process_request(secret, settings(), runner=FakeRunner())
    tiny = settings().model_copy(update={"max_request_bytes": 4})
    oversized = process_request(secret, tiny, runner=FakeRunner())

    assert invalid["status"] == 400
    assert oversized["status"] == 413
    assert "must-not-leak" not in str(invalid)
    assert "must-not-leak" not in str(oversized)
