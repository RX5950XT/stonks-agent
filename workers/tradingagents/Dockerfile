# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15

LABEL org.opencontainers.image.title="Stonks TradingAgents worker" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/TauricResearch/TradingAgents/tree/01477f9afb7a47b849ed4c9259d3a9a4738d9fda"

ENV HOME=/tmp/worker \
    PATH=/workspace/workers/tradingagents/.venv/bin:$PATH \
    PYTHONPATH=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /workspace/workers/tradingagents
COPY workers/tradingagents/pyproject.toml workers/tradingagents/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
RUN uv sync --frozen --no-dev --no-install-project

COPY workers/__init__.py /workspace/workers/__init__.py
COPY workers/tradingagents/__init__.py workers/tradingagents/app.py \
     workers/tradingagents/adapter.py workers/tradingagents/artifacts.py \
     workers/tradingagents/runtime.py \
     workers/tradingagents/runtime_app.py ./
COPY workers/tradingagents/NOTICE.md /usr/share/doc/stonks-tradingagents-worker/NOTICE.md
COPY LICENSE /usr/share/licenses/stonks-tradingagents-worker/LICENSE

RUN mkdir -p /tmp/worker \
    && chown -R 65532:65532 /tmp/worker \
    && chmod -R a-w /workspace /usr/share/doc/stonks-tradingagents-worker \
       /usr/share/licenses/stonks-tradingagents-worker

USER 65532:65532
EXPOSE 7100
WORKDIR /workspace
CMD ["uvicorn", "workers.tradingagents.runtime_app:app", "--host", "0.0.0.0", "--port", "7100", "--no-access-log"]
