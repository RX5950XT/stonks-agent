"""Bounded XML documents used by the narrow S3 maintenance surface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from xml.etree import ElementTree

MAX_CONTROL_BODY = 4_194_304
_KEY_PATTERN = re.compile(r"^[a-z0-9][A-Za-z0-9._/-]{0,1023}$")


class S3DocumentError(ValueError):
    """An invalid provider document without provider-controlled detail."""

    def __init__(self) -> None:
        super().__init__("S3 control document is invalid")


def parse_version_listing(body: bytes) -> dict[str, object]:
    root = _xml_root(body, "ListVersionsResult")
    versions = tuple(_parse_version_node(node) for node in _children(root, "Version"))
    markers = tuple(
        _parse_version_node(node) for node in _children(root, "DeleteMarker")
    )
    if any(value is None for value in (*versions, *markers)):
        raise S3DocumentError
    truncated = _required_text(root, "IsTruncated")
    if truncated not in {"true", "false"}:
        raise S3DocumentError
    result: dict[str, object] = {
        "IsTruncated": truncated == "true",
        "Versions": [value for value in versions if value is not None],
        "DeleteMarkers": [value for value in markers if value is not None],
    }
    if truncated == "true":
        next_key = _required_text(root, "NextKeyMarker")
        next_version = _required_text(root, "NextVersionIdMarker")
        if (
            next_key is None
            or not _valid_key(next_key)
            or next_version is None
            or not _valid_version_id(next_version)
        ):
            raise S3DocumentError
        result.update(
            {
                "NextKeyMarker": next_key,
                "NextVersionIdMarker": next_version,
            }
        )
    return result


def parse_retention(body: bytes) -> dict[str, object]:
    if not body:
        return {}
    root = _xml_root(body, "Retention")
    mode = _required_text(root, "Mode")
    until = parse_datetime(_required_text(root, "RetainUntilDate"))
    if mode not in {"GOVERNANCE", "COMPLIANCE"} or until is None:
        raise S3DocumentError
    return {"Mode": mode, "RetainUntilDate": until}


def parse_legal_hold(body: bytes) -> dict[str, str]:
    root = _xml_root(body, "LegalHold")
    status = _required_text(root, "Status")
    if status not in {"ON", "OFF"}:
        raise S3DocumentError
    return {"Status": status}


def parse_error_code(body: bytes) -> str | None:
    if not body or len(body) > 8_192 or _unsafe_declaration(body):
        return None
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return None
    matching = tuple(node for node in root.iter() if _local_name(node.tag) == "Code")
    if len(matching) != 1 or matching[0].text is None:
        return None
    value = matching[0].text.strip()
    return (
        value if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", value) is not None else None
    )


def parse_versioning(body: bytes) -> dict[str, str]:
    root = _xml_root(body, "VersioningConfiguration")
    status = _required_text(root, "Status")
    if status not in {"Enabled", "Suspended"}:
        raise S3DocumentError
    return {"Status": status}


def parse_object_lock_configuration(body: bytes) -> dict[str, str]:
    root = _xml_root(body, "ObjectLockConfiguration")
    enabled = _required_text(root, "ObjectLockEnabled")
    if enabled != "Enabled":
        raise S3DocumentError
    return {"ObjectLockEnabled": enabled}


def retention_xml(retention: Mapping[object, object]) -> bytes:
    if set(retention) != {"Mode", "RetainUntilDate"}:
        raise ValueError("S3 retention is invalid")
    mode = retention.get("Mode")
    until = normalize_datetime(retention.get("RetainUntilDate"))
    if mode not in {"GOVERNANCE", "COMPLIANCE"} or until is None:
        raise ValueError("S3 retention is invalid")
    text = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<Retention xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Mode>{mode}</Mode><RetainUntilDate>{text}</RetainUntilDate>"
        "</Retention>"
    ).encode()


def legal_hold_xml() -> bytes:
    return (
        b'<LegalHold xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<Status>ON</Status></LegalHold>"
    )


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    return normalize_datetime(parsed)


def normalize_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _parse_version_node(node: ElementTree.Element) -> dict[str, object] | None:
    key = _required_text(node, "Key")
    version = _required_text(node, "VersionId")
    latest = _required_text(node, "IsLatest")
    modified = parse_datetime(_required_text(node, "LastModified"))
    if (
        key is None
        or not _valid_key(key)
        or version is None
        or not _valid_version_id(version)
        or latest not in {"true", "false"}
        or modified is None
    ):
        return None
    return {
        "Key": key,
        "VersionId": version,
        "IsLatest": latest == "true",
        "LastModified": modified,
    }


def _xml_root(body: bytes, expected: str) -> ElementTree.Element:
    if not body or len(body) > MAX_CONTROL_BODY or _unsafe_declaration(body):
        raise S3DocumentError
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError) as error:
        raise S3DocumentError from error
    if _local_name(root.tag) != expected:
        raise S3DocumentError
    return root


def _children(
    root: ElementTree.Element,
    name: str,
) -> tuple[ElementTree.Element, ...]:
    return tuple(child for child in root if _local_name(child.tag) == name)


def _required_text(root: ElementTree.Element, name: str) -> str | None:
    matching = _children(root, name)
    if len(matching) != 1 or matching[0].text is None:
        return None
    value = matching[0].text.strip()
    return value if value and value.isascii() and len(value) <= 2_048 else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _valid_version_id(value: str) -> bool:
    return (
        value.isascii()
        and 1 <= len(value) <= 1_024
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _valid_key(value: str) -> bool:
    return (
        value.isascii()
        and _KEY_PATTERN.fullmatch(value) is not None
        and "//" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _unsafe_declaration(body: bytes) -> bool:
    lowered = body.lower()
    return b"<!doctype" in lowered or b"<!entity" in lowered
