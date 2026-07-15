# Optional integrations 操作手冊

## 安全預設

所有 optional integrations 預設關閉。`config/features.yaml` 是 typed deployment
catalog，不會自行啟動 container；缺少 catalog 時，core loader 會回傳全關閉 flags。
格式錯誤、未知 integration、`execution_mode` 非 `paper`、readiness coupling 或
execution authority 會 fail closed。

`infra/compose.optional.yaml` 沒有 default-active service，也沒有 core、database、
broker 或 `depends_on`。因此 optional integration 缺失、啟動失敗或停用都不會改變
core readiness。任何 profile 都只能增加 research、forecast 或 evaluation 能力，
不能建立 `PortfolioTarget`、override risk、提交 order 或寫 ledger。

## Profile 與邊界

| Integration | Explicit profile | Network | 允許輸出 | Supply-chain 邊界 |
|---|---|---|---|---|
| AI-Trader | 無；external HTTPS adapter | 固定 HTTPS origin | untrusted evidence | 授權不完整，不部署、不複製 source/prompt/assets |
| OpenBB | `openbb` | provider egress | canonical observation | AGPL-3.0-only；獨立 lock、SBOM、NOTICE 與 corresponding source |
| TradingAgents | `tradingagents-paper`、`tradingagents-backtest`、`tradingagents-production` | internal | research artifact | Apache-2.0；獨立 lock/image/NOTICE |
| Kronos | `kronos-cpu`、`kronos-cuda` | internal | forecast only | MIT；CPU/CUDA locks 與 model manifest 分離 |
| Qlib | `qlib` | internal | evaluation only | MIT；獨立 quant-lab lock/image/NOTICE |
| NautilusTrader | `nautilus` | internal | evaluation only | LGPL-3.0-or-later；獨立 lock/source/license/SBOM |
| QuantConnect LEAN | `lean` | internal | evaluation only | Apache-2.0；獨立 Python/NuGet locks/source/license/SBOM |
| RD-Agent factor sandbox | `rd-agent` | none | evaluation only | MIT provenance archive；one-shot clean-room sandbox |
| Freqtrade、FinRL、vectorbt | 無；future RFC only | none | none | 本階段沒有 image、profile 或 dependency |

完整 image、source identity、license、lock、NOTICE、SBOM 與 CVE policy 以
`config/features.yaml` 為準。Core `pyproject.toml` 與 `uv.lock` 不得加入 OpenBB、
PyTorch、TradingAgents、Qlib、RD-Agent、NautilusTrader 或 LEAN runtime。

## 驗證設定

以下命令不會啟動 service：

```powershell
uv run pytest -q --no-cov tests/config/test_optional_features.py tests/security/test_optional_integrations.py
docker compose -f infra/compose.optional.yaml config --quiet
docker compose -f infra/compose.optional.yaml config --profiles
docker compose -f infra/compose.optional.yaml --profile nautilus config --quiet
```

不指定 profile 時，`config --services` 必須沒有輸出：

```powershell
docker compose -f infra/compose.optional.yaml config --services
```

## 顯式啟動與停止

先依 integration 自身 README 建置 pinned image、驗證 lock/license/SBOM/CVE，並以
secret provider 或 process environment 提供 catalog 列出的環境變數。不得把 token、
model path 或 credentials 寫入 YAML 或版控。

```powershell
docker compose -f infra/compose.optional.yaml --profile openbb up -d --wait openbb
docker compose -f infra/compose.optional.yaml --profile openbb down --volumes --remove-orphans
```

啟動 NautilusTrader、LEAN 前必須提供實際 runtime hash、image digest 與至少 32 字元
service token；manifest 中的 `disabled` 只讓 zero-default render 成功，runtime identity
驗證仍會拒絕以該值啟動。Kronos 必須掛載已驗 hash 的 model root。RD-Agent 是每個 job
建立兩個 fresh container 的 one-shot sandbox，應使用受信任的 launcher/smoke 流程，
不得把 profile 當常駐服務，也不得提供 Docker socket 給 generated code。

OpenBB 的 process boundary 不會自動免除 AGPL 義務。部署、修改或透過網路提供服務前，
必須同步發布對應 source offer，並保留 provider、資料與 API 使用條款的獨立審查。

## 故障與回復

Optional service 不健康時，停用該 profile 並將能力標記為 disabled/degraded；不得用空
result、舊 artifact 或 fallback order 偽裝成功。Core paper cycle、risk、reservation、
execution、ledger 與 readiness 應繼續依 canonical policy 運作。若 runtime identity、
license、SBOM、CVE、credential scope 或 output contract drift，立即停止該 profile，
保留 structured error/audit evidence，修正後重新跑該 integration 的完整 CI gate。
