from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict, SecretBytes, SecretStr

from stonks_agent.domain.redaction import (
    REDACTED,
    TRUNCATED,
    RedactionLimits,
    SecretLeakDetected,
    ensure_secret_free,
    redact,
    redact_text,
)

_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_PEM_END = "-----END " + "PRIVATE KEY-----"


def test_recursive_redaction_does_not_mutate_the_source() -> None:
    source = {
        "authorization": "Bearer top-secret-token",
        "nested": {"api_key": "sk-proj-super-secret", "symbol": "AAPL"},
        "items": [{"password": "hunter2"}],
    }

    result = redact(source)

    assert result == {
        "authorization": REDACTED,
        "nested": {"api_key": REDACTED, "symbol": "AAPL"},
        "items": [{"password": REDACTED}],
    }
    assert source["nested"]["api_key"] == "sk-proj-super-secret"


def test_text_redaction_removes_common_secret_forms() -> None:
    rendered = redact_text(
        "Authorization: Bearer abc.def.ghi password=hunter2 key=sk-proj-1234567890"
    )

    assert "abc.def.ghi" not in rendered
    assert "hunter2" not in rendered
    assert "sk-proj-1234567890" not in rendered
    assert REDACTED in rendered


class _SecretModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    token: SecretStr
    payload: SecretBytes


@dataclass(frozen=True)
class _SecretRecord:
    credential: str
    model: _SecretModel


def test_structured_redaction_copies_models_dataclasses_sets_bytes_and_exceptions() -> (
    None
):
    secret = "opaque-runtime-credential"
    model = _SecretModel(
        label="safe",
        token=SecretStr(secret),
        payload=SecretBytes(secret.encode()),
    )
    source = {
        "record": _SecretRecord(credential=secret, model=model),
        "items": {"safe", secret},
        "binary": secret.encode(),
        "error": RuntimeError(f"provider failed credential={secret}"),
    }

    result = redact(source, known_secrets=(secret,))
    rendered = repr(result)

    assert secret not in rendered
    assert REDACTED in rendered
    assert source["record"].credential == secret
    assert model.token.get_secret_value() == secret


def test_text_redaction_covers_urls_jwt_pem_and_provider_credentials() -> None:
    credentials = (
        "https://alice:password@example.test/path?access_token=query-secret",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlX3ZhbHVl",
        f"{_PEM_BEGIN}\nprivate-material\n{_PEM_END}",
        "sk-ant-api03-" + "a" * 40,
        "ghp_" + "b" * 40,
        "AKIA" + "C" * 16,
        "AIza" + "D" * 35,
    )

    rendered = redact_text(" | ".join(credentials))

    assert all(value not in rendered for value in credentials)
    assert "private-material" not in rendered
    assert "query-secret" not in rendered
    assert rendered.count(REDACTED) >= len(credentials)


def test_explicit_known_secrets_are_removed_even_without_secret_shaped_context() -> (
    None
):
    secret = "correct-horse-battery-staple"

    rendered = redact_text(
        f"provider returned {secret} in an otherwise ordinary sentence",
        known_secrets=(secret,),
    )

    assert secret not in rendered
    assert REDACTED in rendered


def test_redaction_bounds_and_cycles_fail_closed_without_mutating_input() -> None:
    limits = RedactionLimits(
        max_depth=2,
        max_items=3,
        max_string_length=16,
        max_bytes_length=16,
    )
    cyclic: dict[str, object] = {"safe": "value"}
    cyclic["self"] = cyclic
    source = {
        "cycle": cyclic,
        "too_many": [1, 2, 3, 4],
        "too_long": "x" * 17,
    }

    result = redact(source, limits=limits)

    assert TRUNCATED in repr(result)
    assert cyclic["self"] is cyclic


@pytest.mark.parametrize(
    "candidate",
    [
        {"authorization": "Bearer opaque-value"},
        {"message": "client_secret=opaque-value"},
        {"message": f"private_key={_PEM_BEGIN}"},
        {"message": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"},
    ],
)
def test_secret_detector_rejects_secret_shaped_values(candidate: object) -> None:
    with pytest.raises(SecretLeakDetected):
        ensure_secret_free(candidate)


def test_secret_detector_uses_explicit_values_and_fails_closed_on_bounds() -> None:
    secret = "opaque-value-without-a-label"

    with pytest.raises(SecretLeakDetected):
        ensure_secret_free({"message": secret}, known_secrets=(secret,))
    with pytest.raises(SecretLeakDetected):
        ensure_secret_free(
            {"message": "x" * 17},
            limits=RedactionLimits(max_string_length=16),
        )

    sanitized = redact({"message": secret}, known_secrets=(secret,))
    ensure_secret_free(sanitized, known_secrets=(secret,))
    ensure_secret_free({"message": "tokenized security research is allowed"})
    ensure_secret_free({"message": "The grouping key=value is public metadata."})
