# Quant-lab worker

這是 Qlib 的 optional isolated research worker。它只接受 core 產生的
immutable `QuantDatasetArtifact`，並固定執行 pinned Qlib
`DataHandlerLP -> DatasetH -> LinearModel(OLS)`。輸出僅為 research-only
predictions、positions、metrics 與 artifact hashes，沒有 strategy promotion、
portfolio target、risk override、order 或 DB 寫入權限。

安全邊界：

- Qlib source 固定 commit 與 archive SHA-256；worker 使用獨立 `uv.lock`。
- HTTP body 有大小上限，只接受 identity encoding 的 JSON typed contract。
- 不接受任意 class/module/expression、pickle、dataset path 或 provider input。
- runtime 使用 non-root、read-only filesystem、cap-drop 與 internal network；
  compose 不提供 DB、queue、provider 或 execution credentials。
- 同一 process 的 Qlib/BLAS inference 序列化；deadline 在執行前後都會檢查。

從 repository root 建置與執行：

```powershell
docker compose -f infra/compose.quant-lab.yaml build quant-lab
docker compose -f infra/compose.quant-lab.yaml up quant-lab
```

Worker API：`GET /healthz`、`POST /v1/research`。正式 job 仍由 core runner
持有 lease/transaction；worker 回傳 generation/nonce fence，不能自行 commit。
