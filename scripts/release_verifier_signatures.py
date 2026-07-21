"""Keyless Sigstore verification for formal releases."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.release_verifier_common import (
    CommandRunner,
    ReleaseError,
    as_mapping,
    as_string,
    regular_status,
    safe_join,
)


def verify_signatures(
    bundle: Path,
    policy: Mapping[str, Any],
    *,
    manifest_path: Path,
    image: str,
    repository: str,
    tag: str,
    commit: str,
    runner: CommandRunner,
) -> None:
    signing = as_mapping(policy.get("signing"), "policy.signing")
    issuer = as_string(signing.get("issuer"), "signing.issuer")
    if issuer != "https://token.actions.githubusercontent.com":
        raise ReleaseError("signing issuer is not trusted")
    workflow = as_string(signing.get("workflow"), "signing.workflow")
    if workflow != ".github/workflows/release.yml":
        raise ReleaseError("signing workflow identity drifted")
    image_bundle = safe_join(
        bundle,
        as_string(signing.get("image_bundle"), "signing.image_bundle"),
    )
    manifest_bundle = safe_join(
        bundle,
        as_string(signing.get("manifest_bundle"), "signing.manifest_bundle"),
    )
    regular_status(image_bundle, max_bytes=32 * 1024 * 1024)
    regular_status(manifest_bundle, max_bytes=32 * 1024 * 1024)
    identity = f"https://github.com/{repository}/{workflow}@refs/tags/{tag}"
    common = (
        "--certificate-identity",
        identity,
        "--certificate-oidc-issuer",
        issuer,
        "--certificate-github-workflow-repository",
        repository,
        "--certificate-github-workflow-ref",
        f"refs/tags/{tag}",
        "--certificate-github-workflow-sha",
        commit,
        "--certificate-github-workflow-trigger",
        "push",
    )
    commands = (
        (
            "cosign",
            "verify",
            "--bundle",
            str(image_bundle),
            *common,
            image,
        ),
        (
            "cosign",
            "verify-blob",
            "--bundle",
            str(manifest_bundle),
            *common,
            str(manifest_path),
        ),
    )
    for command in commands:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            raise ReleaseError("keyless signature verification failed")
