# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS source

ADD --checksum=sha256:3aaefc2f1711376ef6e603ffcf953e6f377eed90d6367fe2eb0cbcd4cfcb2276 https://github.com/microsoft/qlib/archive/d5379c520f66a39953bad76234a7019a72796fd0.tar.gz /tmp/qlib.tar.gz
RUN mkdir -p /opt/qlib \
    && tar -xzf /tmp/qlib.tar.gz --strip-components=1 -C /opt/qlib \
    && rm /tmp/qlib.tar.gz \
    && test -f /opt/qlib/qlib/contrib/model/linear.py \
    && test -f /opt/qlib/LICENSE

ENV HOME=/tmp/worker \
    PATH=/workspace/workers/quant_lab/.venv/bin:$PATH \
    PYTHONPATH=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYQLIB=0.9.8.dev0+d5379c52 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

FROM source AS build
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace/workers/quant_lab
COPY workers/quant_lab/pyproject.toml workers/quant_lab/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
RUN uv sync --frozen --no-dev --no-install-project \
    && uv pip install --python .venv/bin/python --no-deps --no-build-isolation /opt/qlib

FROM source AS runtime
WORKDIR /workspace
COPY --from=build /workspace/workers/quant_lab/.venv /workspace/workers/quant_lab/.venv
COPY packages/contracts /workspace/packages/contracts
COPY workers/__init__.py /workspace/workers/__init__.py
COPY workers/quant_lab /workspace/workers/quant_lab
COPY config/workers/quant_lab.yaml /workspace/config/workers/quant_lab.yaml
COPY LICENSE /usr/share/licenses/stonks-quant-lab-worker/LICENSE
RUN cp /opt/qlib/LICENSE /usr/share/licenses/stonks-quant-lab-worker/QLIB-LICENSE \
    && mkdir -p /tmp/worker \
    && chown -R 65532:65532 /tmp/worker
USER 65532:65532
EXPOSE 7300
CMD ["uvicorn", "workers.quant_lab.runtime_app:app", "--host", "0.0.0.0", "--port", "7300", "--no-access-log"]
