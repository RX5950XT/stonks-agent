#!/usr/bin/env python3
"""Apply the reviewed CPython CVE-2026-3644 cookie hardening patch."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path

BASE_COOKIES_SHA256 = "e79e3858e22266a709c3cac3b0c0b14b9a3f074621145d67e1abc01fb6613ae3"
PATCHED_COOKIES_SHA256 = (
    "6387f676095ae5374943eff99fbcd2d9c681172c00209fadf54c311cf7228149"
)
UPSTREAM_COMMIT = "57e88c1cf95e1481b94ae57abe1010469d47a6b4"

EXPECTED_SNIPPETS = (
    """            if key not in self._reserved:
                raise CookieError("Invalid attribute %r" % (key,))
            data[key] = val
        dict.update(self, data)
""",
    """    def __setstate__(self, state):
        self._key = state['key']
        self._value = state['value']
        self._coded_value = state['coded_value']
""",
    """    def js_output(self, attrs=None):
        # Print javascript
        return \"\"\"
        <script type=\"text/javascript\">
        <!-- begin hiding
        document.cookie = \\\"%s\\\";
        // end hiding -->
        </script>
        \"\"\" % (self.OutputString(attrs).replace('\"', r'\\\"'))
""",
)

_REPLACEMENTS = (
    """            if key not in self._reserved:
                raise CookieError("Invalid attribute %r" % (key,))
            if _has_control_character(key, val):
                raise CookieError("Control characters are not allowed in "
                                  f"cookies {key!r} {val!r}")
            data[key] = val
        dict.update(self, data)

    def __ior__(self, values):
        self.update(values)
        return self
""",
    """    def __setstate__(self, state):
        key = state['key']
        value = state['value']
        coded_value = state['coded_value']
        if _has_control_character(key, value, coded_value):
            raise CookieError("Control characters are not allowed in cookies "
                              f"{key!r} {value!r} {coded_value!r}")
        self._key = key
        self._value = value
        self._coded_value = coded_value
""",
    """    def js_output(self, attrs=None):
        # Print javascript
        output_string = self.OutputString(attrs)
        if _has_control_character(output_string):
            raise CookieError("Control characters are not allowed in cookies")
        return \"\"\"
        <script type=\"text/javascript\">
        <!-- begin hiding
        document.cookie = \\\"%s\\\";
        // end hiding -->
        </script>
        \"\"\" % (output_string.replace('\"', r'\\\"'))
""",
)


class PatchError(ValueError):
    """Raised when the exact reviewed source or patch contract drifted."""


def patch_cookies(source: str) -> str:
    patched = source
    for expected, replacement in zip(
        EXPECTED_SNIPPETS,
        _REPLACEMENTS,
        strict=True,
    ):
        if patched.count(expected) != 1:
            raise PatchError("reviewed CPython patch snippet must occur exactly once")
        patched = patched.replace(expected, replacement)
    return patched


def patch_file(path: Path) -> str:
    try:
        status_result = path.lstat()
        if not stat.S_ISREG(status_result.st_mode) or path.is_symlink():
            raise PatchError("CPython stdlib target must be a regular file")
        source_bytes = path.read_bytes()
    except OSError as error:
        raise PatchError("CPython stdlib target cannot be read") from error
    if hashlib.sha256(source_bytes).hexdigest() != BASE_COOKIES_SHA256:
        raise PatchError("CPython stdlib base hash drifted")
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeError as error:
        raise PatchError("CPython stdlib target is not UTF-8") from error
    patched_bytes = patch_cookies(source).encode()
    digest = hashlib.sha256(patched_bytes).hexdigest()
    if digest != PATCHED_COOKIES_SHA256:
        raise PatchError("CPython patched stdlib hash drifted")
    temporary = path.with_name(f".{path.name}.patched")
    try:
        temporary.write_bytes(patched_bytes)
        os.replace(temporary, path)
    except OSError as error:
        raise PatchError("CPython patched stdlib cannot be installed") from error
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    digest = patch_file(args.path)
    print(f"patched {args.path.name} sha256:{digest} upstream:{UPSTREAM_COMMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
