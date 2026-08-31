from __future__ import annotations

import io
import json
import sys
import tarfile
import urllib.error
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "generate_alpine_source_under_test",
    ROOT / "scripts" / "generate_alpine_source.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

AlpineSourceError = MODULE.AlpineSourceError
AlpinePackage = MODULE.AlpinePackage
build_aports_archive_url: Any = MODULE.build_aports_archive_url
create_source_archive: Any = MODULE.create_source_archive
extract_recipe_archive: Any = MODULE.extract_recipe_archive
fetch_aports_archive: Any = MODULE.fetch_aports_archive
parse_apk_database: Any = MODULE.parse_apk_database
run_fetch_verify_sandbox: Any = MODULE.run_fetch_verify_sandbox
validate_reviewed_inventory: Any = MODULE.validate_reviewed_inventory

COMMIT = "f8c94d2e1d318ab29eb4ac5f00225341c877ed65"
LIBNSL_COMMIT = "79af4a786b7acb5968a4ed25105d4919ed9fa396"
SANDBOX = (
    "docker.io/library/alpine:3.23@sha256:"
    "fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40"
)


def _database(*, commit: str = COMMIT, origin: str = "zlib") -> str:
    return "\n".join(
        (
            "C:Q1fake",
            "P:zlib",
            "V:1.3.2-r0",
            "A:x86_64",
            "L:Zlib",
            f"o:{origin}",
            f"c:{commit}",
            "F:lib",
            "R:libz.so.1",
            "",
        )
    )


def _archive(
    members: list[tuple[str, bytes | None, str]],
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../escape"
                archive.addfile(info)
            elif kind == "safe_symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "post-install"
                archive.addfile(info)
            else:
                assert body is not None
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def test_parse_apk_database_requires_complete_exact_records() -> None:
    packages = parse_apk_database(_database())

    assert packages == (
        AlpinePackage(
            name="zlib",
            version="1.3.2-r0",
            license="Zlib",
            origin="zlib",
            aports_commit=COMMIT,
        ),
    )

    with pytest.raises(AlpineSourceError, match="40-character"):
        parse_apk_database(_database(commit="main"))
    with pytest.raises(AlpineSourceError, match="unsafe origin"):
        parse_apk_database(_database(origin="../zlib"))
    with pytest.raises(AlpineSourceError, match="duplicate package"):
        parse_apk_database(_database() + "\n" + _database())


def test_reviewed_inventory_must_match_image_metadata_exactly() -> None:
    packages = parse_apk_database(_database())
    reviewed = [
        {
            "name": "zlib",
            "version": "1.3.2-r0",
            "license": "Zlib",
            "origin": "zlib",
            "aports_commit": COMMIT,
        }
    ]

    validate_reviewed_inventory(packages, reviewed)

    reviewed[0]["license"] = "MIT"
    with pytest.raises(AlpineSourceError, match="does not match"):
        validate_reviewed_inventory(packages, reviewed)


def test_aports_url_is_official_and_binds_exact_sha_and_path() -> None:
    assert build_aports_archive_url("zlib", COMMIT) == (
        "https://gitlab.alpinelinux.org/api/v4/projects/1/repository/"
        "archive.tar.gz?sha=f8c94d2e1d318ab29eb4ac5f00225341c877ed65"
        "&path=main%2Fzlib"
    )

    with pytest.raises(AlpineSourceError, match="unsafe origin"):
        build_aports_archive_url("zlib&path=main/busybox", COMMIT)

    assert build_aports_archive_url(
        "libnsl",
        LIBNSL_COMMIT,
        aports_path="community/libnsl",
    ) == (
        "https://gitlab.alpinelinux.org/api/v4/projects/1/repository/"
        "archive.tar.gz?sha=79af4a786b7acb5968a4ed25105d4919ed9fa396"
        "&path=community%2Flibnsl"
    )
    with pytest.raises(AlpineSourceError, match="aports path"):
        build_aports_archive_url("zlib", COMMIT, aports_path="testing/zlib")


def test_recipe_archive_extracts_only_bounded_expected_regular_files(
    tmp_path: Path,
) -> None:
    root = f"aports-{COMMIT}-{COMMIT}-main-zlib"
    payload = _archive(
        [
            (f"{root}/", None, "dir"),
            (f"{root}/main/", None, "dir"),
            (f"{root}/main/zlib/", None, "dir"),
            (f"{root}/main/zlib/APKBUILD", b"source='zlib.tar.gz'\n", "file"),
            (f"{root}/main/zlib/security.patch", b"diff --git a b\n", "file"),
        ]
    )

    files = extract_recipe_archive(
        payload,
        origin="zlib",
        commit=COMMIT,
        destination=tmp_path,
    )

    assert [item.path for item in files] == ["APKBUILD", "security.patch"]
    assert (tmp_path / "APKBUILD").read_bytes() == b"source='zlib.tar.gz'\n"


@pytest.mark.parametrize(
    "member",
    [
        ("root/main/zlib/../../escape", b"x", "file"),
        ("root/main/zlib/APKBUILD", None, "symlink"),
        ("root/main/other/APKBUILD", b"x", "file"),
    ],
)
def test_recipe_archive_rejects_paths_links_and_wrong_origin(
    tmp_path: Path,
    member: tuple[str, bytes | None, str],
) -> None:
    with pytest.raises(AlpineSourceError):
        extract_recipe_archive(
            _archive([member]),
            origin="zlib",
            commit=COMMIT,
            destination=tmp_path,
        )


def test_recipe_archive_rejects_duplicate_member(tmp_path: Path) -> None:
    path = f"aports-{COMMIT}-{COMMIT}-main-zlib/main/zlib/APKBUILD"
    payload = _archive([(path, b"first", "file"), (path, b"second", "file")])

    with pytest.raises(AlpineSourceError, match="duplicate"):
        extract_recipe_archive(
            payload,
            origin="zlib",
            commit=COMMIT,
            destination=tmp_path,
        )


def test_recipe_archive_materializes_only_safe_internal_symlink(tmp_path: Path) -> None:
    root = f"aports-{COMMIT}-{COMMIT}-main-zlib/main/zlib"
    payload = _archive(
        [
            (f"{root}/APKBUILD", b"source='zlib.tar.gz'\n", "file"),
            (f"{root}/post-install", b"#!/bin/sh\n", "file"),
            (f"{root}/post-upgrade", None, "safe_symlink"),
        ]
    )

    files = extract_recipe_archive(
        payload,
        origin="zlib",
        commit=COMMIT,
        destination=tmp_path,
    )

    assert [item.path for item in files] == ["APKBUILD", "post-install", "post-upgrade"]
    assert (tmp_path / "post-upgrade").read_bytes() == b"#!/bin/sh\n"


def test_aports_network_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def fail(*_: object, **__: object) -> object:
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fail)

    with pytest.raises(AlpineSourceError, match="download failed"):
        fetch_aports_archive(build_aports_archive_url("zlib", COMMIT), 1024)
    assert attempts == 3


class _Runner:
    def __init__(self, *, fail_on_fetch: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_on_fetch = fail_on_fetch

    def __call__(
        self,
        command: tuple[str, ...],
        **_: object,
    ) -> Any:
        import subprocess

        self.commands.append(command)
        if self.fail_on_fetch and command[-3:] == ("abuild", "fetch", "verify"):
            raise subprocess.CalledProcessError(1, command, stderr="checksum failed")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_fetch_verify_sandbox_has_no_mount_or_secret_and_always_cleans_up(
    tmp_path: Path,
) -> None:
    recipe = tmp_path / "zlib" / "recipe"
    recipe.mkdir(parents=True)
    (recipe / "APKBUILD").write_text("source='zlib.tar.gz'\n", encoding="utf-8")
    runner = _Runner(fail_on_fetch=True)

    with pytest.raises(AlpineSourceError, match="abuild fetch verify failed"):
        run_fetch_verify_sandbox(
            origins=("zlib",),
            staging_root=tmp_path,
            sandbox_image=SANDBOX,
            runner=runner,
            container_name="stonks-alpine-source-test",
        )

    flattened = "\n".join(" ".join(command) for command in runner.commands)
    assert "--mount" not in flattened
    assert "--volume" not in flattened
    assert "-v " not in flattened
    assert "secret" not in flattened.lower()
    assert "apk add --no-cache alpine-sdk=1.1-r0" in flattened
    assert any(
        command[-3:] == ("abuild", "fetch", "verify") for command in runner.commands
    )
    fetch_command = next(
        command
        for command in runner.commands
        if command[-3:] == ("abuild", "fetch", "verify")
    )
    assert (
        "DISTFILES_MIRROR=https://distfiles.alpinelinux.org/distfiles/v3.23"
        in fetch_command
    )
    assert runner.commands[-1] == (
        "docker",
        "rm",
        "--force",
        "stonks-alpine-source-test",
    )


def test_source_archive_is_deterministic_and_manifest_is_closed(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    recipe = staging / "zlib" / "recipe"
    distfiles = staging / "zlib" / "distfiles"
    recipe.mkdir(parents=True)
    distfiles.mkdir()
    (recipe / "APKBUILD").write_bytes(b"source='zlib.tar.gz'\n")
    (distfiles / "zlib.tar.gz").write_bytes(b"verified source")
    packages = parse_apk_database(_database())
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_summary = create_source_archive(
        packages=packages,
        staging_root=staging,
        output=first,
        package_database_sha256="a" * 64,
    )
    second_summary = create_source_archive(
        packages=packages,
        staging_root=staging,
        output=second,
        package_database_sha256="a" * 64,
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_summary == second_summary
    with tarfile.open(first, "r:gz") as archive:
        names = archive.getnames()
        manifest_handle = archive.extractfile("manifest.json")
        assert manifest_handle is not None
        manifest = json.load(manifest_handle)
    assert names == [
        "manifest.json",
        "origins/zlib/distfiles/zlib.tar.gz",
        "origins/zlib/recipe/APKBUILD",
    ]
    assert manifest["schema_version"] == "stonks-agent/alpine-source/v1"
    assert [item["path"] for item in manifest["files"]] == names[1:]
    assert manifest["origins"] == [
        {
            "aports_commit": COMMIT,
            "aports_path": "main/zlib",
            "origin": "zlib",
        }
    ]


def test_source_archive_records_reviewed_community_path(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    recipe = staging / "zlib" / "recipe"
    distfiles = staging / "zlib" / "distfiles"
    recipe.mkdir(parents=True)
    distfiles.mkdir()
    (recipe / "APKBUILD").write_bytes(b"source='zlib.tar.gz'\n")

    output = tmp_path / "source.tar.gz"
    create_source_archive(
        packages=parse_apk_database(_database()),
        staging_root=staging,
        output=output,
        package_database_sha256="a" * 64,
        origin_paths={"zlib": "community/zlib"},
    )

    with tarfile.open(output, "r:gz") as archive:
        handle = archive.extractfile("manifest.json")
        assert handle is not None
        manifest = json.load(handle)
    assert manifest["origins"][0]["aports_path"] == "community/zlib"


def test_source_archive_rejects_unlisted_symlink(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    recipe = staging / "zlib" / "recipe"
    distfiles = staging / "zlib" / "distfiles"
    recipe.mkdir(parents=True)
    distfiles.mkdir()
    (recipe / "APKBUILD").write_bytes(b"source='zlib.tar.gz'\n")
    link = distfiles / "escape"
    try:
        link.symlink_to(tmp_path / "outside")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(AlpineSourceError, match="regular file"):
        create_source_archive(
            packages=parse_apk_database(_database()),
            staging_root=staging,
            output=tmp_path / "source.tar.gz",
            package_database_sha256="a" * 64,
        )
