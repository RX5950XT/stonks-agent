# Kronos worker third-party notice

This optional isolated worker uses source from:

- Kronos, https://github.com/shiyu-coder/Kronos
- Commit: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`
- License: MIT
- Copyright (c) 2025 ShiYu

The source archive is verified as SHA-256
`969719e47b2134d8a56533784508b6d859bd1c9aacb1b62e4a504cb4fc096021`
during the image build. The complete upstream MIT License is copied into the
image at `/usr/share/licenses/stonks-kronos-worker/KRONOS-LICENSE`.

Model and tokenizer weights are separate Hugging Face artifacts. Their pinned
repositories, revisions, sizes, and SHA-256 identities are recorded in
`model-manifest.json`; operators remain responsible for confirming the model
and input-data terms that apply to their deployment.
