from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks_agent.adapters.artifacts import s3_xml

NS = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def xml(name: str, content: str) -> bytes:
    return f"<{name}{NS}>{content}</{name}>".encode()


def test_version_listing_parses_versions_delete_markers_and_pagination() -> None:
    body = xml(
        "ListVersionsResult",
        (
            "<IsTruncated>true</IsTruncated>"
            "<Version><Key>prod/objects/aa/hash</Key><VersionId>v1</VersionId>"
            "<IsLatest>true</IsLatest>"
            "<LastModified>2026-07-18T10:00:00Z</LastModified></Version>"
            "<DeleteMarker><Key>prod/objects/bb/hash</Key>"
            "<VersionId>delete-1</VersionId><IsLatest>false</IsLatest>"
            "<LastModified>2026-07-18T11:00:00+00:00</LastModified>"
            "</DeleteMarker>"
            "<NextKeyMarker>prod/objects/aa/hash</NextKeyMarker>"
            "<NextVersionIdMarker>v1</NextVersionIdMarker>"
        ),
    )

    result = s3_xml.parse_version_listing(body)

    assert result["IsTruncated"] is True
    assert result["Versions"][0]["VersionId"] == "v1"
    assert result["DeleteMarkers"][0]["VersionId"] == "delete-1"
    assert result["NextKeyMarker"] == "prod/objects/aa/hash"


@pytest.mark.parametrize(
    "body",
    (
        b"",
        b"<Wrong/>",
        b"<!DOCTYPE x><ListVersionsResult/>",
        xml("ListVersionsResult", "<IsTruncated>maybe</IsTruncated>"),
        xml("ListVersionsResult", "<IsTruncated>true</IsTruncated>"),
        xml(
            "ListVersionsResult",
            (
                "<IsTruncated>false</IsTruncated><Version>"
                "<Key>../escape</Key><VersionId>v1</VersionId>"
                "<IsLatest>true</IsLatest>"
                "<LastModified>invalid</LastModified></Version>"
            ),
        ),
    ),
)
def test_version_listing_rejects_malformed_or_unsafe_documents(body: bytes) -> None:
    with pytest.raises(s3_xml.S3DocumentError):
        s3_xml.parse_version_listing(body)


def test_retention_legal_hold_and_bucket_controls_are_exact() -> None:
    retention = s3_xml.parse_retention(
        xml(
            "Retention",
            "<Mode>COMPLIANCE</Mode>"
            "<RetainUntilDate>2027-07-18T10:00:00Z</RetainUntilDate>",
        )
    )

    assert retention == {
        "Mode": "COMPLIANCE",
        "RetainUntilDate": datetime(2027, 7, 18, 10, tzinfo=UTC),
    }
    assert s3_xml.parse_retention(b"") == {}
    assert s3_xml.parse_legal_hold(xml("LegalHold", "<Status>ON</Status>")) == {
        "Status": "ON"
    }
    assert s3_xml.parse_versioning(
        xml("VersioningConfiguration", "<Status>Enabled</Status>")
    ) == {"Status": "Enabled"}
    assert s3_xml.parse_object_lock_configuration(
        xml(
            "ObjectLockConfiguration",
            "<ObjectLockEnabled>Enabled</ObjectLockEnabled>",
        )
    ) == {"ObjectLockEnabled": "Enabled"}


@pytest.mark.parametrize(
    ("parser", "body"),
    (
        (s3_xml.parse_retention, xml("Retention", "<Mode>INVALID</Mode>")),
        (s3_xml.parse_legal_hold, xml("LegalHold", "<Status>OFFLINE</Status>")),
        (
            s3_xml.parse_versioning,
            xml("VersioningConfiguration", "<Status>Disabled</Status>"),
        ),
        (
            s3_xml.parse_object_lock_configuration,
            xml(
                "ObjectLockConfiguration",
                "<ObjectLockEnabled>Disabled</ObjectLockEnabled>",
            ),
        ),
    ),
)
def test_control_documents_reject_unknown_state(parser, body: bytes) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(s3_xml.S3DocumentError):
        parser(body)


def test_retention_and_hold_serialization_is_bounded_and_canonical() -> None:
    body = s3_xml.retention_xml(
        {
            "Mode": "GOVERNANCE",
            "RetainUntilDate": datetime(2027, 7, 18, 10, tzinfo=UTC),
        }
    )

    assert b"<Mode>GOVERNANCE</Mode>" in body
    assert b"2027-07-18T10:00:00Z" in body
    assert s3_xml.legal_hold_xml().endswith(b"<Status>ON</Status></LegalHold>")
    with pytest.raises(ValueError):
        s3_xml.retention_xml({"Mode": "GOVERNANCE"})
    with pytest.raises(ValueError):
        s3_xml.retention_xml({"Mode": "INVALID", "RetainUntilDate": datetime.now(UTC)})


def test_error_code_and_datetime_parsing_never_accept_unsafe_declarations() -> None:
    assert s3_xml.parse_error_code(b"<Error><Code>NoSuchKey</Code></Error>") == (
        "NoSuchKey"
    )
    assert (
        s3_xml.parse_error_code(b"<!ENTITY x 'secret'><Error><Code>&x;</Code></Error>")
        is None
    )
    assert s3_xml.parse_error_code(b"<Error><Code>bad-code</Code></Error>") is None
    assert s3_xml.parse_datetime("not-a-time") is None
    assert s3_xml.normalize_datetime(datetime(2026, 7, 18, 10)) is None


@pytest.mark.parametrize(
    "body",
    (
        xml(
            "Retention",
            '<Mode attacker="1">GOVERNANCE</Mode>'
            "<RetainUntilDate>2027-07-18T10:00:00Z</RetainUntilDate>",
        ),
        xml(
            "Retention",
            "<Unknown>value</Unknown>"
            "<Mode>GOVERNANCE</Mode>"
            "<RetainUntilDate>2027-07-18T10:00:00Z</RetainUntilDate>",
        ),
        xml(
            "Retention",
            ("<Mode>" * 9) + "GOVERNANCE" + ("</Mode>" * 9),
        ),
        b"<?xml version='1.0'?><Retention><Mode>GOVERNANCE</Mode></Retention>",
        b"<Retention><!-- comment --><Mode>GOVERNANCE</Mode></Retention>",
    ),
)
def test_xml_preflight_rejects_attribute_name_and_depth_hash_flood_surfaces(
    body: bytes,
) -> None:
    with pytest.raises(s3_xml.S3DocumentError):
        s3_xml.parse_retention(body)
