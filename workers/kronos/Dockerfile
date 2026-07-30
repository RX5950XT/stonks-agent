# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS source

ADD --checksum=sha256:969719e47b2134d8a56533784508b6d859bd1c9aacb1b62e4a504cb4fc096021 https://github.com/shiyu-coder/Kronos/archive/67b630e67f6a18c9e9be918d9b4337c960db1e9a.tar.gz /tmp/kronos.tar.gz
RUN mkdir -p /opt/kronos \
    && tar -xzf /tmp/kronos.tar.gz --strip-components=1 -C /opt/kronos \
    && rm /tmp/kronos.tar.gz \
    && test -f /opt/kronos/model/kronos.py \
    && test -f /opt/kronos/LICENSE

ENV HOME=/tmp/worker \
    PYTHONPATH=/workspace:/opt/kronos \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

FROM source AS cpu
ENV PATH=/workspace/workers/kronos/.venv/bin:$PATH
WORKDIR /workspace/workers/kronos
COPY workers/kronos/pyproject.toml workers/kronos/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
RUN uv sync --frozen --no-dev --no-install-project

FROM source AS cuda
ENV PATH=/workspace/workers/kronos/profiles/cuda/.venv/bin:$PATH
WORKDIR /workspace/workers/kronos/profiles/cuda
COPY workers/kronos/profiles/cuda/pyproject.toml workers/kronos/profiles/cuda/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
RUN uv sync --frozen --no-dev --no-install-project

FROM cuda AS runtime-cuda
WORKDIR /workspace
COPY workers/__init__.py /workspace/workers/__init__.py
COPY workers/kronos /workspace/workers/kronos
COPY LICENSE /usr/share/licenses/stonks-kronos-worker/LICENSE
RUN cp /opt/kronos/LICENSE /usr/share/licenses/stonks-kronos-worker/KRONOS-LICENSE \
    && mkdir -p /tmp/worker \
    && chown -R 65532:65532 /tmp/worker
USER 65532:65532
EXPOSE 7200
CMD ["uvicorn", "workers.kronos.runtime_app:app", "--host", "0.0.0.0", "--port", "7200", "--no-proxy-headers", "--no-access-log"]

FROM cpu AS runtime-cpu
WORKDIR /workspace
COPY workers/__init__.py /workspace/workers/__init__.py
COPY workers/kronos /workspace/workers/kronos
COPY LICENSE /usr/share/licenses/stonks-kronos-worker/LICENSE
RUN cp /opt/kronos/LICENSE /usr/share/licenses/stonks-kronos-worker/KRONOS-LICENSE \
    && mkdir -p /tmp/worker \
    && chown -R 65532:65532 /tmp/worker
USER 65532:65532
EXPOSE 7200
CMD ["uvicorn", "workers.kronos.runtime_app:app", "--host", "0.0.0.0", "--port", "7200", "--no-proxy-headers", "--no-access-log"]
