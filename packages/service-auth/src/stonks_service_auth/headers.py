"""Unambiguous bounded Authorization header extraction for ASGI ingress."""

from __future__ import annotations

from collections.abc import Iterable


def invalid_or_oversized_content_length(
    declared: str | None,
    maximum: int,
) -> bool:
    """Reject malformed or oversized lengths without unbounded integer parsing."""
    if declared is None:
        return False
    maximum_text = str(maximum)
    return (
        not declared.isdecimal()
        or len(declared) > len(maximum_text)
        or (len(declared) == len(maximum_text) and declared > maximum_text)
    )


def exactly_one_authorization_header(
    headers: Iterable[tuple[bytes, bytes]],
) -> str | None:
    values = tuple(value for name, value in headers if name.lower() == b"authorization")
    if len(values) != 1 or not 1 <= len(values[0]) <= 4103:
        return None
    try:
        decoded = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    if decoded.strip() != decoded or any(
        not 0x20 <= ord(character) <= 0x7E for character in decoded
    ):
        return None
    return decoded
