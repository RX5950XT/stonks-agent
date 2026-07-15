# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS uvbin

FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d AS base

ENV HOME=/tmp/worker \
    PATH=/workspace/workers/quant_lab/rd_agent/.venv/bin:$PATH \
    PYTHONPATH=/workspace:/workspace/packages/contracts/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

FROM base AS source
COPY --from=uvbin /usr/local/bin/uv /usr/local/bin/uv

ADD --checksum=sha256:837010068449c5e874dee83f91240b281bed529acd214fdd1bdb6678f620caa4 https://codeload.github.com/microsoft/RD-Agent/tar.gz/4f9ecb005881cddc08df0124a2e894c018007679 /tmp/rd-agent.tar.gz
RUN mkdir -p /opt/rd-agent-source /usr/share/sources/rd-agent \
    && tar -xzf /tmp/rd-agent.tar.gz --strip-components=1 -C /opt/rd-agent-source \
    && cp /tmp/rd-agent.tar.gz /usr/share/sources/rd-agent/rd-agent-4f9ecb00.tar.gz \
    && test -f /opt/rd-agent-source/LICENSE \
    && test "$(sha256sum /opt/rd-agent-source/LICENSE | cut -d' ' -f1)" = "c2cfccb812fe482101a8f04597dfc5a9991a6b2748266c47ac91b6a5aae15383" \
    && chmod 0444 /usr/share/sources/rd-agent/rd-agent-4f9ecb00.tar.gz

FROM source AS build
WORKDIR /workspace/workers/quant_lab/rd_agent
COPY workers/quant_lab/rd_agent/pyproject.toml workers/quant_lab/rd_agent/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS runtime
WORKDIR /workspace
COPY --from=build /workspace/workers/quant_lab/rd_agent/.venv /workspace/workers/quant_lab/rd_agent/.venv
COPY --from=source /usr/share/sources/rd-agent/rd-agent-4f9ecb00.tar.gz /usr/share/sources/rd-agent/rd-agent-4f9ecb00.tar.gz
COPY --from=source /opt/rd-agent-source/LICENSE /usr/share/licenses/stonks-rd-agent-factor-sandbox/RD-AGENT-LICENSE
COPY packages/contracts /workspace/packages/contracts
COPY workers/__init__.py /workspace/workers/__init__.py
COPY workers/quant_lab/__init__.py /workspace/workers/quant_lab/__init__.py
COPY workers/quant_lab/rd_agent /workspace/workers/quant_lab/rd_agent
COPY LICENSE /usr/share/licenses/stonks-rd-agent-factor-sandbox/LICENSE
RUN set -eux; \
    cd /usr/local/lib/python3.12; \
    rm -rf \
        asyncio/windows_events.py \
        asyncio/windows_utils.py \
        bz2.py \
        gzip.py \
        html \
        http/cookies.py \
        lib-dynload/_bz2*.so \
        lib-dynload/_elementtree*.so \
        lib-dynload/_lzma*.so \
        lib-dynload/_sqlite3*.so \
        lib-dynload/pyexpat*.so \
        lzma.py \
        sqlite3 \
        tarfile.py \
        webbrowser.py \
        xml \
        ensurepip \
        site-packages/pip \
        site-packages/pip-*.dist-info \
        /usr/local/bin/pip*; \
    apk del --no-network .python-rundeps sqlite-libs; \
    mkdir -p /tmp/worker; \
    chown 65532:65532 /tmp/worker; \
    chmod 0444 \
        /usr/share/sources/rd-agent/rd-agent-4f9ecb00.tar.gz \
        /usr/share/licenses/stonks-rd-agent-factor-sandbox/LICENSE \
        /usr/share/licenses/stonks-rd-agent-factor-sandbox/RD-AGENT-LICENSE; \
    python -c "import pydantic, yaml; from workers.quant_lab.rd_agent.adapter import RDAgentSandboxWorker"
USER 65532:65532
CMD ["python", "-m", "workers.quant_lab.rd_agent.cli"]
