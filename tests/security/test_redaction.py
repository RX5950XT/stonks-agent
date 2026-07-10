from __future__ import annotations

from stonks_agent.domain.redaction import REDACTED, redact, redact_text


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
