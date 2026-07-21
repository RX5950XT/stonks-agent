from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location(
    "patch_cpython_stdlib_under_test",
    ROOT / "scripts" / "patch_cpython_stdlib.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PatchError = MODULE.PatchError
patch_cookies: Any = MODULE.patch_cookies


def test_cookie_patch_closes_update_pickle_inplace_and_js_control_paths() -> None:
    source = "\n".join(MODULE.EXPECTED_SNIPPETS)

    patched = patch_cookies(source)

    assert "def __ior__(self, values):" in patched
    assert "if _has_control_character(key, val):" in patched
    assert "if _has_control_character(key, value, coded_value):" in patched
    assert "output_string = self.OutputString(attrs)" in patched
    assert patched != source


def test_cookie_patch_rejects_unknown_or_partially_patched_source() -> None:
    source = "\n".join(MODULE.EXPECTED_SNIPPETS)
    with pytest.raises(PatchError, match="exactly once"):
        patch_cookies(source.replace(MODULE.EXPECTED_SNIPPETS[0], "drifted"))

    patched = patch_cookies(source)
    with pytest.raises(PatchError, match="exactly once"):
        patch_cookies(patched)
