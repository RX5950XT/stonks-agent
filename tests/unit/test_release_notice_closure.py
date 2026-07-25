from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.release_verifier_common import ReleaseError
from scripts.release_verifier_reports import verify_notices


def _policy() -> dict[str, object]:
    return {
        "bundle": {
            "required_payload_files": [
                "payload/THIRD_PARTY_NOTICES.md",
                "payload/config/features.yaml",
                "payload/workers/example/NOTICE.md",
            ]
        },
        "legal": {
            "notices_path": "payload/THIRD_PARTY_NOTICES.md",
            "required_notice_ids": ["EXAMPLE-MIT-WORKER"],
            "feature_notices": [
                {
                    "integration": "example",
                    "root_notice_id": "EXAMPLE-MIT-WORKER",
                    "paths": ["workers/example/NOTICE.md"],
                    "execution_authority": False,
                }
            ],
        },
    }


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    notice = bundle / "payload" / "workers" / "example" / "NOTICE.md"
    notice.parent.mkdir(parents=True)
    notice.write_text("MIT attribution", encoding="utf-8")
    (bundle / "payload" / "THIRD_PARTY_NOTICES.md").write_text(
        "## EXAMPLE-MIT-WORKER\nNo execution authority.\n", encoding="utf-8"
    )
    return bundle


def test_feature_notice_closure_accepts_only_signed_authority_free_files(
    tmp_path: Path,
) -> None:
    verify_notices(_bundle(tmp_path), _policy())


def test_required_notice_id_must_be_an_exact_heading_not_a_substring(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "payload" / "THIRD_PARTY_NOTICES.md").write_text(
        "## PREFIX-EXAMPLE-MIT-WORKER-SUFFIX\nNo execution authority.\n",
        encoding="utf-8",
    )
    policy = _policy()
    legal = policy["legal"]
    assert isinstance(legal, dict)
    legal.pop("feature_notices")

    with pytest.raises(ReleaseError, match="required third-party notice is missing"):
        verify_notices(bundle, policy)


def test_required_notice_ids_must_be_unique(tmp_path: Path) -> None:
    policy = _policy()
    legal = policy["legal"]
    assert isinstance(legal, dict)
    legal["required_notice_ids"].append("EXAMPLE-MIT-WORKER")

    with pytest.raises(ReleaseError, match="required notice identity is duplicated"):
        verify_notices(_bundle(tmp_path), policy)


def test_dedicated_notice_hash_is_verified_from_signed_payload(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    notice = bundle / "payload" / "docs" / "legal" / "notices" / "EXAMPLE.md"
    notice.parent.mkdir(parents=True)
    notice.write_text("MIT License\nCopyright example\n", encoding="utf-8")
    policy = _policy()
    legal = policy["legal"]
    required = policy["bundle"]
    assert isinstance(legal, dict) and isinstance(required, dict)
    relative = "payload/docs/legal/notices/EXAMPLE.md"
    required["required_payload_files"].append(relative)
    legal["dedicated_notices"] = [
        {
            "id": "EXAMPLE-MIT-WORKER",
            "path": relative,
            "sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
            "runtime_path": "/usr/share/licenses/stonks-agent/EXAMPLE.md",
        }
    ]

    verify_notices(bundle, policy)
    notice.write_text("tampered", encoding="utf-8")
    with pytest.raises(ReleaseError, match="dedicated notice content drifted"):
        verify_notices(bundle, policy)


@pytest.mark.parametrize("mutation", ["unsigned", "extra_field", "runtime_escape"])
def test_dedicated_notice_policy_rejects_incomplete_closure(
    tmp_path: Path, mutation: str
) -> None:
    bundle = _bundle(tmp_path)
    notice = bundle / "payload" / "docs" / "legal" / "notices" / "EXAMPLE.md"
    notice.parent.mkdir(parents=True)
    notice.write_text("MIT License\nCopyright example\n", encoding="utf-8")
    policy = _policy()
    legal = policy["legal"]
    required = policy["bundle"]
    assert isinstance(legal, dict) and isinstance(required, dict)
    relative = "payload/docs/legal/notices/EXAMPLE.md"
    required["required_payload_files"].append(relative)
    item = {
        "id": "EXAMPLE-MIT-WORKER",
        "path": relative,
        "sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
        "runtime_path": "/usr/share/licenses/stonks-agent/EXAMPLE.md",
    }
    legal["dedicated_notices"] = [item]
    if mutation == "unsigned":
        required["required_payload_files"].remove(relative)
    elif mutation == "extra_field":
        item["unexpected"] = True
    else:
        item["runtime_path"] = "/tmp/EXAMPLE.md"

    with pytest.raises(ReleaseError):
        verify_notices(bundle, policy)


@pytest.mark.parametrize("duplicate", ["id", "path", "runtime_path"])
def test_dedicated_notice_identity_fields_must_each_be_unique(
    tmp_path: Path, duplicate: str
) -> None:
    bundle = _bundle(tmp_path)
    notices = bundle / "payload" / "THIRD_PARTY_NOTICES.md"
    notices.write_text(
        notices.read_text(encoding="utf-8")
        + "## SECOND-MIT-NOTICE\nNo execution authority.\n",
        encoding="utf-8",
    )
    policy = _policy()
    legal = policy["legal"]
    required = policy["bundle"]
    assert isinstance(legal, dict) and isinstance(required, dict)
    legal["required_notice_ids"].append("SECOND-MIT-NOTICE")
    first_path = "payload/docs/legal/notices/EXAMPLE.md"
    second_path = "payload/docs/legal/notices/SECOND.md"
    if duplicate == "runtime_path":
        second_path = "payload/docs/legal/notices/nested/EXAMPLE.md"
    for path, body in ((first_path, b"first"), (second_path, b"second")):
        target = bundle / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        required["required_payload_files"].append(path)
    first = {
        "id": "EXAMPLE-MIT-WORKER",
        "path": first_path,
        "sha256": hashlib.sha256(b"first").hexdigest(),
        "runtime_path": "/usr/share/licenses/stonks-agent/EXAMPLE.md",
    }
    second = {
        "id": "SECOND-MIT-NOTICE",
        "path": second_path,
        "sha256": hashlib.sha256(b"second").hexdigest(),
        "runtime_path": f"/usr/share/licenses/stonks-agent/{Path(second_path).name}",
    }
    second[duplicate] = first[duplicate]
    if duplicate == "path":
        second["runtime_path"] = first["runtime_path"]
        second["sha256"] = first["sha256"]
    legal["dedicated_notices"] = [first, second]

    with pytest.raises(ReleaseError, match="dedicated notice closure is invalid"):
        verify_notices(bundle, policy)


@pytest.mark.parametrize(
    "mutation", ["authority", "missing", "extra_field", "unsigned"]
)
def test_feature_notice_closure_rejects_policy_and_file_drift(
    tmp_path: Path, mutation: str
) -> None:
    bundle = _bundle(tmp_path)
    policy = _policy()
    legal = policy["legal"]
    assert isinstance(legal, dict)
    feature = legal["feature_notices"][0]
    assert isinstance(feature, dict)
    if mutation == "authority":
        feature["execution_authority"] = True
    elif mutation == "missing":
        (bundle / "payload" / "workers" / "example" / "NOTICE.md").unlink()
    elif mutation == "extra_field":
        feature["unexpected"] = True
    else:
        required = policy["bundle"]
        assert isinstance(required, dict)
        required["required_payload_files"].remove("payload/workers/example/NOTICE.md")

    with pytest.raises(ReleaseError):
        verify_notices(bundle, policy)
