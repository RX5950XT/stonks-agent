from __future__ import annotations

import io
import logging

from stonks_agent.adapters.observability.logging import RedactingFormatter


def test_formatter_redacts_structured_arguments_and_known_secrets() -> None:
    secret = "opaque-logging-secret"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        RedactingFormatter(
            "%(levelname)s %(message)s",
            known_secrets=(secret,),
        )
    )
    logger = logging.getLogger("tests.redaction.structured")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.error("provider failed %s", {"credential": secret, "symbol": "AAPL"})

    rendered = output.getvalue()
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert "AAPL" in rendered


def test_formatter_redacts_exception_message_but_keeps_exception_type() -> None:
    secret = "opaque-exception-secret"
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter(known_secrets=(secret,)))
    logger = logging.getLogger("tests.redaction.exception")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError(f"request failed token={secret}")
    except RuntimeError:
        logger.exception("provider request failed")

    rendered = output.getvalue()
    assert secret not in rendered
    assert "RuntimeError" in rendered
    assert "[REDACTED]" in rendered
