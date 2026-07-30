# Isolated Kronos worker

This optional worker contains PyTorch and the MIT-licensed Kronos runtime. It is
not part of the core lock and has no database, queue, provider, broker, ledger,
portfolio, risk, or execution credentials.

## Model preparation

Worker runtime downloads are forbidden. Provisioning is a separate one-shot
operator step; from the repository root run:

```powershell
uv run --frozen python scripts/fetch_kronos_model.py
```

It fetches only the exact pinned repository/revision recorded below, verifies
every file against `model-manifest.json`, deletes any mismatch and exits
non-zero, and writes `.data/models/kronos/`. Re-running verifies in place.
To prepare the structure by hand instead, produce exactly this and verify it
against `model-manifest.json`:

```text
/models/
  kronos-small/{config.json,model.safetensors}
  kronos-tokenizer-base/{config.json,model.safetensors}
```

The pinned Hugging Face revisions are Kronos-small
`901c26c1332695a2a8f243eb2f37243a37bea320` and Kronos-Tokenizer-base
`0e0117387f39004a9016484a186a908917e22426`. Startup checks exact files, sizes,
SHA-256 values, symlinks, and untracked entries before loading either component.

## Profiles

CPU and CUDA use independent `pyproject.toml`/`uv.lock` environments and Docker
targets (`runtime-cpu`, `runtime-cuda`). The model is warmed exactly once at
process startup. `/healthz` is liveness, `/readyz` reports load readiness, and
`/v1/preflight` verifies an exact runtime identity. `/v1/forecast` only accepts
lease-fenced PIT bars, core-generated exchange session timestamps, exact pinned
runtime identity, and explicit seeds. Each seed runs sequentially with upstream
`sample_count=1`; the worker returns every raw path without averaging. Core owns
artifact persistence, deterministic signal mapping, promotion, and all trading
authority; this worker has none of those capabilities.

The repository verifier is the supported local CPU smoke. It generates
ephemeral service identity, starts the hardened container, performs one actual
forecast, maps the result to a shadow alpha signal, and removes its temporary
container/network:

```powershell
uv run --frozen python scripts/verify_kronos_runtime.py
```

It expects the verified files under `.data/models/kronos/`. Direct Compose
startup additionally requires the complete service OIDC/JWKS environment and is
not a supported shortcut. This smoke does not connect Kronos to GUI research or
grant paper-trading eligibility.
