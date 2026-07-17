# Observability 操作與限制

## 已驗證邊界

- `config/observability/default.toml` 預設 `enabled=false`，使用no-op runtime。
- 啟用時只接受typed config中的exact OTLP/HTTP origin；禁止ambient `OTEL_*` credential/config、proxy、`.netrc`與redirect。
- Metrics固定為四個canonical名稱，labels只允許`component/operation/status/environment`；span不接受raw account、symbol、user、URL、prompt或exception text。
- `infra/compose.observability.yaml` 使用internal backend network；只有Collector OTLP/health與Grafana經獨立ingress bridge綁定host loopback，Prometheus不發布host port。
- Grafana要求external file secrets，anonymous/signup、ambient plugin install/update、news、analytics與Live均停用。

Docker bridge以`host_binding_ipv4`限制published port的預設host位址；本manifest仍在每個port顯式寫入`127.0.0.1`。[Docker bridge options](https://docs.docker.com/engine/network/drivers/bridge/)

Grafana的`preinstall_disabled`與`preinstall_auto_update`用來阻止預設suggested plugins背景下載。[Grafana configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/)

## 驗證

```powershell
uv run pytest -q --no-cov tests/policy/test_observability_infra.py
```

Runtime smoke會使用三個pinned images與臨時Grafana secrets，啟動完整stack、驗證host health，從core OTLP exporter送出trace/metrics，再由internal network讀回canonical metric與labels；測試結束會移除containers與network。

## 尚未宣稱

- Trace目前經本機OTLP pipeline送至nop sink，沒有持久化backend。
- Prometheus、Grafana狀態使用tmpfs，重啟即清除。
- Ingress bridge不是production egress network policy；跨host部署仍需P6.7的TLS、firewall/network policy與正式secret distribution。
- Response與durable carrier保存synthetic span ID；SDK實際span是其child，因此trace ID可關聯，但backend會看到缺席parent，且SDK child span ID尚未回綁ContextVar。
