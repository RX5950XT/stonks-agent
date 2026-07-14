# NautilusTrader sidecar notice

This optional image dynamically uses the unmodified `nautilus_trader==1.230.0`
wheel, licensed `LGPL-3.0-or-later` by Nautech Systems Pty Ltd and contributors.
Copyright (C) 2015-2026 Nautech Systems Pty Ltd. The reviewed source tag is
`v1.230.0`, commit `8160730c7c550480b0a439fb11086a4c4de15f0b`:

https://github.com/nautechsystems/nautilus_trader/tree/8160730c7c550480b0a439fb11086a4c4de15f0b

The exact wheel, source sdist, URLs and SHA-256 values are pinned in `uv.lock`
and `distribution-manifest.yaml`. The image includes the verified source sdist
at `/usr/share/source/nautilus-trader/nautilus_trader-1.230.0.tar.gz`, the
wheel's LGPL license at
`/usr/share/licenses/stonks-nautilus-sidecar/NAUTILUS-LGPL-3.0` and does not
modify or statically link NautilusTrader. The accompanying GPLv3 text is at
`/usr/share/licenses/stonks-nautilus-sidecar/GNU-GPL-3.0`.

Users may replace the dynamic wheel with an interface-compatible modified
build. Build a derived image as root, use `uv pip install --python
/workspace/sidecars/nautilus/.venv/bin/python --reinstall --no-deps
/tmp/nautilus_trader-1.230.0+modified-*.whl`, then restore `USER 65532:65532`.
The modified wheel should use a PEP 440 local version beginning
`1.230.0+`; its OCI image digest and reported engine version become a new
runtime identity. Run the canonical replay tests before deployment. Users may
reverse engineer the combined work for debugging those changes as permitted
by LGPLv3 section 4.

The Stonks adapter files remain Apache-2.0. The process boundary is for
dependency, authority, and deployment isolation; it does not remove LGPL
obligations.
