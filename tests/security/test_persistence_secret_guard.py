from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from stonks_agent.adapters.postgres.secret_free_json import (
    SecretFreeJSONB,
    SecretPersistenceError,
)

_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"


def _bind(payload: dict[str, object]) -> dict[str, object] | None:
    return SecretFreeJSONB().process_bind_param(payload, postgresql.dialect())


def test_secret_free_json_accepts_canonical_financial_payload_unchanged() -> None:
    payload = {
        "symbol": "AAPL",
        "content_hash": "a" * 64,
        "idempotency_key": "research-2026-07-16",
        "nested": {"attempt_nonce": "nonce-1"},
    }

    assert _bind(payload) is payload


@pytest.mark.parametrize(
    "payload",
    [
        {"authorization": "Bearer opaque-service-credential"},
        {"nested": {"api_key": "sk-proj-sensitive-value"}},
        {"error": "upstream URL https://user:password@example.test/path"},
        {"error": f"private_key={_PEM_BEGIN}"},
    ],
)
def test_secret_free_json_rejects_secret_shaped_durable_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(SecretPersistenceError) as raised:
        _bind(payload)
    assert "sensitive-value" not in str(raised.value)


def test_secret_free_json_allows_database_null() -> None:
    assert SecretFreeJSONB().process_bind_param(None, postgresql.dialect()) is None
