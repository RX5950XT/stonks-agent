# Performance 與 resource capacity

本文件定義 Stonks Agent 的paper-only capacity gate。唯一machine source of truth是
`config/capacity.yaml`；報告固定標示`single_host_ci_baseline`與
`production_sla_claim=false`。ResourceObservation固定為`probe_process`，只與獨立的
`probe_runtime_budget`比較。本地或shared CI量測只證明該次受控fixture在明列資源內通過，
不能推論production SLA、跨主機吞吐或real-money能力。

## 量測範圍

| Workload | 實際量測 | 不代表 |
|---|---|---|
| API | ASGI security/auth與archived request contract | 已部署production business API容量 |
| Queue | Actual PostgreSQL enqueue/claim與unique lease | 常駐dispatcher throughput |
| Snapshot | Actual PostgreSQL durable scheduling transaction | 外部provider下載速度 |
| Research | Actual PostgreSQL run/job/snapshot binding | LLM或TradingAgents端到端速度 |
| Forecast | Authenticated/core forecast contract與warm fake runtime | 真實Kronos CPU/CUDA模型latency |
| Paper cycle | Deterministic target→risk→reservation→fill→balanced journal→replay | live execution或市場撮合容量 |

每個workload只接受policy固定的sample count、concurrency與measurement scope。Timing使用
bounded integer microseconds，p95採nearest-rank；verifier會從raw samples重新計算p95、
wall time、success count與workload outcome，caller不能自行宣稱passed。

## 執行與證據

```powershell
$env:STONKS_CAPACITY_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/stonks_capacity"
uv run python scripts/run_capacity_probe.py --output .data/capacity-report.json
```

DSN只接受loopback上的exact `stonks_capacity` test database，且不得包含password、query、
fragment或額外authority。Report只保存policy ID、bounded samples/counts、opaque identities、
resource evidence與重算後結果；不保存DSN、credential、raw payload、owner identity或高基數
telemetry label。資料庫必須是fresh、專用且disposable；canonical evidence不做DELETE，隔離與
清理由整個database/service lifetime負責。CI只上傳bounded `capacity-report.json`。

## Resource 與 saturation 邊界

Runtime raw CPU、RAM、PID、process與in-flight只量目前Python `probe_process`，並以獨立
`probe_runtime_budget`（4000 millicores、2048 MiB、1 PID、1 process、16 in-flight）判定。
Core、PostgreSQL、TradingAgents、Kronos CPU/CUDA與Quant lab六組budget只屬
`static_manifest_only`契約；本probe未實測這些process的runtime資源，不能用probe RSS或CPU
替它們背書。Core HTTP concurrency、DB pool與runtime role connection limit是不同邊界，
不能用HTTP上限推論DB容量。

TradingAgents、Kronos與Quant lab同步重工作固定offload出event loop，execution gate飽和時立即
回`429 worker_busy`；不得無界等待、自動retry或追單，也不得飢餓risk/execution資源。
`CUDA CI未量測GPU/VRAM`；`gpu_vram_enforced=false`明示Compose沒有VRAM hard limit。真實
Kronos模型、GPU型號、driver/runtime與peak GPU/VRAM必須另做hardware-scoped calibration，
不能混入shared CI baseline。

## 停止條件

- Policy/report unknown、duplicate、missing或reordered workload，sample數量或concurrency不符。
- Timing為負值、非integer、overflow，p95/wall time超限，或caller outcome與重算結果不同。
- Queue lease重複，run/job/snapshot binding漂移，forecast contract失真，paper journal不平衡、
  duplicate fill或replay hash不一致。
- DB不是fresh、disposable的exact loopback `stonks_capacity`，canonical graph含foreign row，
  或credential可能進入argv/log/report。
- Heavy worker未立即回bounded 429、event loop被同步推論阻塞，或resource manifest缺界限。

任何停止條件都使整份report fail closed；不得刪樣本、放寬threshold、改標degraded或用平均值
掩蓋p95失敗。

## 已知限制

- Default core container目前只提供health/readiness surface；五組business API尚未組成單一
  production deployment，因此API probe只驗ASGI security/contract boundary。
- `stonks-worker claim-once`不是常駐dispatcher；queue數據只代表repository primitive。
- Shared runner上的CPU、RSS與throughput只能作單次evidence，不能當跨機器regression ratio。
- 真實Kronos模型、TradingAgents外部LLM/provider、Qlib dataset與GPU/VRAM需在各自pinned
  hardware/runtime另做calibration，不能以fake/contract probe替代。
