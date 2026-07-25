from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "verify_release_boundaries_under_test",
    ROOT / "scripts" / "verify_release.py",
)
assert SPEC is not None and SPEC.loader is not None
verify_release: ModuleType = module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_release
SPEC.loader.exec_module(verify_release)


def test_release_verifier_facade_preserves_supported_api() -> None:
    expected = {
        "ReleaseError",
        "audit_locks",
        "create_manifest",
        "load_json",
        "stage_release",
        "verify_grype_database_identity",
        "verify_grype_report",
        "verify_image_report",
        "verify_openbb_source",
        "verify_formal_release",
        "verify_release",
    }

    assert expected <= set(vars(verify_release))


def test_release_verifier_modules_stay_below_project_size_limit() -> None:
    modules = sorted((ROOT / "scripts").glob("release_verifier*.py"))

    assert modules
    assert all(
        len(path.read_text(encoding="utf-8").splitlines()) < 800 for path in modules
    )
    assert (
        len(
            (ROOT / "scripts" / "verify_release.py")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        < 800
    )
