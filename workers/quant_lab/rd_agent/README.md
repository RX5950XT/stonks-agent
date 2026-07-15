# RD-Agent factor sandbox

此 worker 只執行已封存的 RD-Agent proposal 中、符合 `factor-expression-v1`
的 `compute(rows)`。它不是 RD-Agent 的完整 runtime，也不支援 upstream model
template、DockerEnv、LocalEnv、pickle、GPU、LLM 或網路。

每次 invocation 只跑一個 child process；可信 launcher 必須用相同 archived
source、label-free dataset、runtime 與 policy 啟動兩個不同的全新 container。
Core 比對兩份 canonical prediction hash、重跑 AST scan，再用 P3.4 的 labeled
PIT snapshot 執行完整 evaluation。worker 與 report 都沒有 promotion、target、
order、risk、ledger 或 database authority。

容器以 stdin 接收單一 `RDSandboxInvocation`，stdout 只輸出統一 API envelope。
正式啟動需傳入精確 `STONKS_RD_RUNTIME_HASH` 與 `STONKS_RD_IMAGE_DIGEST`；不得
掛載 Docker socket、host path、provider credentials 或 core secrets。

## Supply-chain gate

- Runtime只保留7個frozen Python packages；RD-Agent commit `4f9ecb0`的MIT
  source archive與license只供provenance，不解壓、不import、不執行。
- Image從pinned Python 3.12.13/Alpine base建立，移除未使用的tar/XML/HTML、
  compression、webbrowser、Windows asyncio、SQLite、system pip capabilities。
- `openvex.json`只針對exact `pkg:generic/python@3.12.13`與已移除code；
  `grype.yaml`沒有manual ignore。SBOM、OpenVEX與scanner policy都納入runtime hash。
- `scripts/smoke_rd_agent.py`必須在actual Docker runtime驗兩個fresh instances、
  deterministic bytes、escape/network/rootfs/socket、CPU/output bounds與removed imports。

本機驗證入口：

```powershell
uv lock --check --project workers/quant_lab/rd_agent
$env:PYTHONPATH='.;src;packages/contracts/src'
uv run --project workers/quant_lab/rd_agent pytest -c workers/quant_lab/rd_agent/pyproject.toml -q --no-cov workers/quant_lab/rd_agent/tests tests/contracts/workers/test_rd_agent.py
docker build -f workers/quant_lab/rd_agent/Dockerfile -t stonks-rd-agent-factor-sandbox:p5.8 .
uv run python scripts/smoke_rd_agent.py
```
