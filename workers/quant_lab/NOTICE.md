# Qlib worker third-party notice

This optional isolated worker uses source from:

- Qlib, https://github.com/microsoft/qlib
- Commit: `d5379c520f66a39953bad76234a7019a72796fd0`
- License: MIT License
- Copyright (c) Microsoft Corporation

The source archive is verified as SHA-256
`3aaefc2f1711376ef6e603ffcf953e6f377eed90d6367fe2eb0cbcd4cfcb2276`
during the image build. The complete upstream MIT License is copied into the
image at `/usr/share/licenses/stonks-quant-lab-worker/QLIB-LICENSE`.

The worker only exposes the pinned Qlib `LinearModel` OLS path over a closed
typed contract. It does not accept arbitrary Python modules, serialized models,
expressions, dataset paths, provider credentials, or generated code.
