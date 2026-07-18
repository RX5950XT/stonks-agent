# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.9.27@sha256:143b40f4ab56a780f43377604702107b5a35f83a4453daf1e4be691358718a6a AS uv

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY packages ./packages
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ARG VCS_REF
LABEL org.opencontainers.image.title="Stonks Agent core" \
      org.opencontainers.image.description="Paper-only deployment health and migration runtime" \
      org.opencontainers.image.source="https://github.com/stonks-agent/stonks-agent" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV HOME=/tmp/stonks \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    STONKS_DEPLOYMENT_ROOT=/opt/stonks
WORKDIR /opt/stonks
RUN test -n "${VCS_REF}" \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* \
              /usr/local/lib/python3.12/ensurepip \
              /usr/local/bin/pip*
COPY --from=builder /opt/venv /opt/venv
COPY --chown=65532:65532 alembic.ini /opt/stonks/alembic.ini
COPY --chown=65532:65532 migrations /opt/stonks/migrations
COPY --chown=65532:65532 config /opt/stonks/config
COPY --chown=65532:65532 templates /opt/stonks/templates
COPY --chown=65532:65532 strategies /opt/stonks/strategies
COPY --chown=65532:65532 LICENSE THIRD_PARTY_NOTICES.md /usr/share/licenses/stonks-agent/
USER 65532:65532
ENTRYPOINT ["stonks-deploy"]
CMD ["serve"]
