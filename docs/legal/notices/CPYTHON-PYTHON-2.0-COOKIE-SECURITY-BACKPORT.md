# CPYTHON-PYTHON-2.0-COOKIE-SECURITY-BACKPORT

Core runtime 使用 CPython 3.12.13。Python 整體授權的 SPDX expression 為
`Python-2.0`；其中主要授權條款是 `PSF-2.0`，完整 Python license 亦包含歷史
授權條款。Runtime 保留上游完整檔案
`/usr/local/lib/python3.12/LICENSE.txt`，SHA-256 為
`3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf`。

來源：

- CPython 3.12.13 source：
  <https://github.com/python/cpython/tree/v3.12.13>
- CPython 3.12.13 complete license：
  <https://github.com/python/cpython/blob/v3.12.13/LICENSE>
- Copyright (c) 2001 Python Software Foundation；完整 copyright 與歷史
  notices 以 runtime 內的完整 license 為準。

## CVE-2026-3644 修改摘要

`scripts/patch_cpython_stdlib.py` 對 CPython 3.12.13 的
`Lib/http/cookies.py` 做 selective backport，來源是 CPython upstream commit
[`57e88c1cf95e1481b94ae57abe1010469d47a6b4`](https://github.com/python/cpython/commit/57e88c1cf95e1481b94ae57abe1010469d47a6b4)。
該 commit 的主旨是拒絕 `http.cookies.Morsel.update()` 與
`BaseCookie.js_output()` 中的 control characters，並處理 validated in-place
update 與 unpickle state。該 commit 的
[complete LICENSE](https://github.com/python/cpython/blob/57e88c1cf95e1481b94ae57abe1010469d47a6b4/LICENSE)
SHA-256 為
`b0e25a78cffb43f4d92de8b61ccfa1f1f98ecbc22330b54b5251e7b6ba010231`。

Exact provenance：

- CPython v3.12.13 原始 `cookies.py` SHA-256：
  `e79e3858e22266a709c3cac3b0c0b14b9a3f074621145d67e1abc01fb6613ae3`
- 上述 upstream commit 完整 `cookies.py` SHA-256：
  `407579d026cb4ba7bba7952c97e52e8d3a270a92679e896d601cd13a9a06260e`
- Core runtime 安裝後 `cookies.py` SHA-256：
  `6387f676095ae5374943eff99fbcd2d9c681172c00209fadf54c311cf7228149`

本專案只移植 `Morsel.update`、`Morsel.__ior__`、`Morsel.__setstate__` 與
`BaseCookie.js_output` 的相關防護；沒有複製 upstream commit 的完整檔案或測試
suite。安裝後 hash 與 upstream commit 完整檔案不同是預期結果。

這是 Stonks Agent 維護者套用到固定 Python 3.12.13 base 的修改版，
不是 CPython 官方 3.12.x release，也不表示其他 Python runtime、其他標準函式庫路徑或未來 CVE
已修正。修改部分沿用 CPython 的 `Python-2.0` 完整授權條款；本文件是 PSF License
Version 2 要求的修改摘要，不取代 runtime 內的完整 license。
