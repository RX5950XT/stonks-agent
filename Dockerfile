# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.9.27@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a AS uv

FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY packages ./packages
COPY src ./src
RUN /sbin/apk add --no-cache \
        build-base=0.5-r3 \
        postgresql18-dev=18.4-r0
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d AS runtime

ARG VCS_REF
ARG SOURCE_URL=https://github.com/stonks-agent/stonks-agent
ARG RELEASE_VERSION=0.1.2
LABEL org.opencontainers.image.title="Stonks Agent core" \
      org.opencontainers.image.description="Paper-only deployment health and migration runtime" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV HOME=/tmp/stonks \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    STONKS_DEPLOYMENT_ROOT=/opt/stonks
WORKDIR /opt/stonks
COPY scripts/patch_cpython_stdlib.py /tmp/patch_cpython_stdlib.py
RUN test -n "${VCS_REF}" \
    && /sbin/apk add --no-cache libpq=18.4-r0 \
    && printf '%s\n' \
        gdbm keyutils-libs krb5-conf krb5-libs libbz2 libcom_err \
        libcrypto3 libffi libintl libncursesw libnsl libpanelw libssl3 \
        libtirpc libtirpc-conf libuuid libverto ncurses-terminfo-base \
        readline xz-libs zlib >> /etc/apk/world \
    && sort -u /etc/apk/world -o /etc/apk/world \
    && /sbin/apk del --no-network .python-rundeps sqlite-libs \
    && python /tmp/patch_cpython_stdlib.py \
        /usr/local/lib/python3.12/http/cookies.py \
    && rm /tmp/patch_cpython_stdlib.py \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* \
              /usr/local/lib/python3.12/ensurepip \
              /usr/local/bin/pip* \
              /usr/local/lib/python3.12/asyncio/windows_events.py \
              /usr/local/lib/python3.12/asyncio/windows_utils.py \
              /usr/local/lib/python3.12/bz2.py \
              /usr/local/lib/python3.12/html/parser.py \
              /usr/local/lib/python3.12/lib-dynload/_bz2*.so \
              /usr/local/lib/python3.12/lib-dynload/_lzma*.so \
              /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so \
              /usr/local/lib/python3.12/lzma.py \
              /usr/local/lib/python3.12/sqlite3 \
              /usr/local/lib/python3.12/tarfile.py \
              /usr/local/lib/python3.12/webbrowser.py
COPY --from=builder /opt/venv /opt/venv
COPY --chown=65532:65532 alembic.ini /opt/stonks/alembic.ini
COPY --chown=65532:65532 migrations /opt/stonks/migrations
COPY --chown=65532:65532 config /opt/stonks/config
COPY --chown=65532:65532 templates /opt/stonks/templates
COPY --chown=65532:65532 strategies /opt/stonks/strategies
COPY --chown=65532:65532 LICENSE THIRD_PARTY_NOTICES.md /usr/share/licenses/stonks-agent/
COPY --chown=65532:65532 --chmod=0444 docs/legal/notices/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md /usr/share/licenses/stonks-agent/AI-HEDGE-FUND-MIT-PEAD-EVENT-STUDY.md
USER 65532:65532
ENTRYPOINT ["stonks-deploy"]
CMD ["serve"]
