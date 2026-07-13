# Isolated Kronos worker

This optional worker contains PyTorch and the MIT-licensed Kronos runtime. It is
not part of the core lock and has no database, queue, provider, broker, ledger,
portfolio, risk, or execution credentials.

## Model preparation

Runtime downloads are forbidden. Prepare this exact read-only structure outside
the repository and verify it against `model-manifest.json`:

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
`/v1/preflight` verifies an exact runtime identity. Canonical forecast I/O and
immutable sampled-path artifacts are added by P3.7; this environment grants no
signal promotion or trading authority.

Run the CPU container with a verified host model directory:

```powershell
$env:STONKS_KRONOS_MODEL_ROOT='D:\models\kronos'
docker compose -f infra/compose.kronos.yaml up --build kronos-cpu
```
