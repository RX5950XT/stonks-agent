# RD factor sandbox CVE review

Review date: 2026-07-15. Runtime: Linux, Python 3.12.13, factor-expression v1.

The project requires Python 3.12. Python 3.12.13 is the current 3.12 security
release, but Grype's binary CPE matcher reports vulnerabilities in optional
stdlib capabilities that this one-shot worker does not need. The runtime image
physically removes those source modules and native extensions. The smoke gate
then proves that they cannot be imported. The OpenVEX statements apply only to
the listed CVE and `pkg:generic/python@3.12.13`; Grype has no manual ignore
rules, so every new High or Critical finding still fails the build.

| CVE | Removed vulnerable capability |
| --- | --- |
| CVE-2026-11940, CVE-2026-11972 | `tarfile` |
| CVE-2026-15308 | `html.parser` and the `html` package |
| CVE-2026-3298 | Windows-only asyncio proactor modules |
| CVE-2026-3644 | `http.cookies` |
| CVE-2026-4224, CVE-2026-7210 | `xml`, `pyexpat`, `_elementtree` |
| CVE-2026-4786 | `webbrowser` |
| CVE-2026-6100 | `bz2`, `gzip`, `lzma` and native extensions |
| CVE-2026-9669 | `bz2` and `_bz2` |

SQLite is not VEX-suppressed. The image deletes `_sqlite3`, the `sqlite3`
package, `sqlite-libs`, and the Python runtime meta-package that retained it.
This removes the FTS5 implementation reported under CVE-2026-11822 and
CVE-2026-11824 instead of ignoring those findings. System `pip` and
`ensurepip` are also removed; the frozen worker virtual environment contains
only its seven runtime packages.

Evidence:

- Python 3.12.13 release: <https://www.python.org/downloads/release/python-31213/>
- Grype exact ignore/VEX fields: <https://oss.anchore.com/docs/guides/vulnerability/filter-results/>
- SQLite CVE applicability and fixes: <https://sqlite.org/cves.html>

Any Python base version change requires deleting or re-reviewing every VEX
entry. Do not widen rules to a package-only, wildcard, severity, namespace, or
fix-state ignore.
