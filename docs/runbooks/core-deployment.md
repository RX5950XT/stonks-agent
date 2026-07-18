# Core deployment 操作與限制

## 已驗證範圍

`infra/compose.yaml` 的 default surface 只有 `core` 與 PostgreSQL；`migrate`
是 explicit `migration` profile。Core image 使用 frozen production lock、
digest-pinned Python 3.12 multi-stage build，runtime 為 UID/GID 65532，
Compose 套用 read-only root filesystem、cap-drop ALL、no-new-privileges、
tmpfs、PID/CPU/RAM limits 與 bounded restart。

目前 `core` 只提供 deployment liveness/readiness control surface，不宣稱已組合
五組 business API，也不把 `stonks-worker claim-once` 偽裝成常駐 dispatcher。
所有 execution 維持 `paper`。

## 自動驗證

完整 clean-volume build、migration、restart、outage 與 durable replay：

```powershell
uv run python scripts/smoke_core_deployment.py
```

Smoke 會產生獨立隨機 DB credentials，只透過 temporary secret files 掛載；
結束或失敗時都移除 containers、networks、volume 與 temporary files。流程包含：

1. 建置同一個 core image，啟動 non-root PostgreSQL。
2. 以 owner credential 執行 migration 兩次，建立獨立 `stonks_app` runtime login。
3. 啟動 core，以 runtime credential 驗證 exact Alembic head。
4. 比較兩次 deterministic paper fake-cycle。
5. 封存固定 workflow record，重啟 core 與 PostgreSQL，再由 persisted record
   replay；不宣稱 fresh stochastic inference bit-identical。
6. DB outage 期間驗證 `/healthz` 為 200、`/readyz` 為 503；DB 恢復後
   readiness 自動恢復。
7. 驗證 container user、read-only root、capabilities、security options，
   並掃描 Compose config、logs、image history 是否出現 generated secrets。

## Health contract

- `GET /healthz`：只證明 process/event loop 存活，不查 DB 或 optional service。
- `GET /readyz`：以 bounded query 驗 DB，且 `alembic_version` 必須 exact 等於
  image 內唯一 migration head。
- 缺 secret、migration 未執行、schema drift、DB outage 或非 `paper` 設定都
  fail closed。錯誤只回 structured public-safe envelope。
- Optional integration 不在 core `depends_on` 或 readiness 判定內。

## Credential 與 migration 邊界

- Raw DSN、raw password、ambient libpq credential 與未知 DB settings 會被拒絕。
- Runtime 與 owner password 必須是 absolute、non-symlink、bounded secret file。
- `core` 只取得 runtime secret；`migrate` 才取得 owner 與 runtime secret。
- Runtime login 只有 `stonks_app` membership，無 superuser、DDL、role switching、
  database creation 或 replication authority。
- Migration 使用 PostgreSQL advisory lock，必須明確執行；API startup 不會自動
  migration。

## Optional profiles

`infra/compose.optional.yaml` 維持 zero-default。與 default Compose 合併後，
10 個 explicit profiles 都可獨立 render，且移除任一 optional service 不影響
core readiness。OpenBB 仍遵循 AGPL corresponding-source policy；heavy upstream
不進 core lock/image。

## 尚未宣稱

- Core business API 的 production dependency composition。
- 常駐 job dispatcher、multi-replica/distributed rate limit。
- 對外 TLS termination、trusted reverse proxy、mTLS、external IdP live wiring。
- Kubernetes/nomad 等 orchestrator、跨主機 network policy、egress firewall、
  managed PostgreSQL TLS/backup/restore。
- Optional heavy workers 在這份 default Compose 中完成端到端 production job path。

因此此 manifest 是單一 host、loopback ingress 的 hardened paper deployment
baseline，不是 public internet production topology。
