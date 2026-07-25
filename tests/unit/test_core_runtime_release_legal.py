from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "verify_release_legal_under_test",
    ROOT / "scripts" / "verify_release.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ReleaseError = MODULE.ReleaseError
verify_notices: Any = MODULE._verify_notices


def test_release_verifier_blocks_incomplete_alpine_source_closure(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    policy_target = (
        bundle / "payload" / "config" / "release" / "core-runtime-legal.json"
    )
    policy_target.parent.mkdir(parents=True)
    runtime_policy = json.loads(
        (ROOT / "config" / "release" / "core-runtime-legal.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_policy["alpine"]["corresponding_source"]["status"] = "missing"
    runtime_policy["alpine"]["corresponding_source"]["release_decision"] = "block"
    policy_target.write_text(json.dumps(runtime_policy), encoding="utf-8")
    notices = bundle / "payload" / "THIRD_PARTY_NOTICES.md"
    notices.parent.mkdir(parents=True, exist_ok=True)
    notices.write_text(
        "## CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT\n## ALPINE-3.23-CORE-RUNTIME\n",
        encoding="utf-8",
    )
    release_policy = {
        "legal": {
            "notices_path": "payload/THIRD_PARTY_NOTICES.md",
            "core_runtime_policy_path": (
                "payload/config/release/core-runtime-legal.json"
            ),
            "required_notice_ids": [
                "CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT",
                "ALPINE-3.23-CORE-RUNTIME",
            ],
        }
    }

    with pytest.raises(
        ReleaseError,
        match="Alpine corresponding source closure is incomplete",
    ):
        verify_notices(bundle, release_policy)


def test_release_verifier_rejects_runtime_legal_schema_drift(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    policy_target = (
        bundle / "payload" / "config" / "release" / "core-runtime-legal.json"
    )
    policy_target.parent.mkdir(parents=True)
    policy_target.write_text(
        json.dumps(
            {
                "schema_version": "unknown",
                "alpine": {
                    "corresponding_source": {
                        "required_for_distribution": True,
                        "status": "verified",
                        "release_decision": "allow",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    notices = bundle / "payload" / "THIRD_PARTY_NOTICES.md"
    notices.parent.mkdir(parents=True, exist_ok=True)
    notices.write_text("notices", encoding="utf-8")
    release_policy = {
        "legal": {
            "notices_path": "payload/THIRD_PARTY_NOTICES.md",
            "core_runtime_policy_path": (
                "payload/config/release/core-runtime-legal.json"
            ),
            "required_notice_ids": [],
        }
    }

    with pytest.raises(ReleaseError, match="legal policy schema is invalid"):
        verify_notices(bundle, release_policy)
