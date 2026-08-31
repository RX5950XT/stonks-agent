# ALPINE-3.23-CORE-RUNTIME

Core runtime 的 base 是
`python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d`
（Linux/amd64）。正式 image identity 由每次 release 的 signed manifest 綁定；
本文件只固定 base 與 runtime legal contract，不固定 content-dependent final digest。

Final runtime 的 `/lib/apk/db/installed` 有 37 個 APK records。以
`name`、`version`、`license`、source `origin` 與 package `repo-commit` 排序後的
canonical inventory SHA-256 是
`f61429eb093628277b4ba7f4c87ab7b1c61925e496d2b99dc56fd92aeddadab8`。
Exact package、version、license expression、origin 與 40 字元 build commit 記錄
於 `config/release/core-runtime-legal.json`；release SBOM/inventory 仍必須由實際
image digest 重新產生，不能只信任這份靜態快照。

這些 APK 不是 Apache-2.0 core application 的一部分授權聲明。Runtime 同時包含
GPL、LGPL、MPL、MIT、BSD、Apache、X11、Zlib、bzip2 與 Public-Domain metadata；
例如 BusyBox/APK tools、GDBM、Readline、musl-utils、gettext/libintl 與
ca-certificates。Alpine 官方 package format 也明示 `license` 欄位是資訊性
metadata，APK 本身不驗證授權義務，因此 SBOM 有 license expression 不等於散布
義務已完成。

## 已驗證的 corresponding source

Release tooling 已從實際 image package database 重驗 37 筆 inventory，封存
27 個 source origins 的 exact aports recipe、patch、build script 與經
`abuild fetch verify` 驗證的 distfiles。Deterministic archive 共 244 個 source
files、133,200,204 bytes；兩次獨立產生的 raw bytes 相同，archive SHA-256 為
`304e4f09643b6b81f3de2e0c12bcfedc113031095fe60487a3b65fc8f1bed7b9`，manifest
SHA-256 為
`caf862681e934e57df25a13c38ecc24d039f69fe83f1f19d15e0eb1d946e6384`。

正式 release 必須把 exact
`payload/release/alpine-corresponding-source.tar.gz` 納入同一 signed manifest，
且 verifier 必須重驗 canonical metadata、manifest、member hash/size/path 與 legal
policy；任何 drift 都 fail closed。本文件是工程治理與 provenance 記錄，不是法律
意見。
