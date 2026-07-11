# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15

LABEL org.opencontainers.image.title="Stonks Agent OpenBB sidecar" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.vendor="Stonks Agent contributors"

ENV HOME=/tmp/openbb \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && uv run --frozen openbb-build

COPY app.py ./
COPY Dockerfile README.md NOTICE.md SOURCE_OFFER.md provider-manifest.yaml \
    sbom.cdx.json \
    /srv/source-tree/
COPY pyproject.toml uv.lock app.py /srv/source-tree/

ADD --checksum=sha256:c26cfc2ae37c1700e01db9ca0fd2cd02118715cec2097ea870f47a70186402bb \
    https://files.pythonhosted.org/packages/42/9e/0cbc623a38f7c72cebce47c7c71968dd3276259422f9eb2eb49fca73a219/openbb_core-1.6.13.tar.gz \
    /srv/source-tree/upstream/
ADD --checksum=sha256:4a23a6ca542a1bbe309a1b9c1246d9e905ec03ab28c3da8e9a8f9a0db6ff8659 \
    https://files.pythonhosted.org/packages/8e/64/7c5b2e09ac7f9d338c3f84712b2d76b2411a36c0607e8847dbf6de97b9bc/openbb_platform_api-1.3.6.tar.gz \
    /srv/source-tree/upstream/
ADD --checksum=sha256:143bfcc2766227af14e804312645e3e77762b2954d0750ead0a1c7f8ec2e64dc \
    https://files.pythonhosted.org/packages/17/71/93a5980580b3eda4f1566ff4346a71385f68efe12538b46c53c0264b1cf0/openbb_equity-1.6.2.tar.gz \
    /srv/source-tree/upstream/
ADD --checksum=sha256:b25bc0fe17552f2331c771f9f91ab66c138e7bb54b07af282381dec2c273bd5b \
    https://files.pythonhosted.org/packages/10/62/810ce26ce3bdda302104780d9fa70a727ce9dceb50e41968c27c049787e7/openbb_yfinance-1.6.3.tar.gz \
    /srv/source-tree/upstream/

ADD --checksum=sha256:442baa7865ddf53ba04808a29e4810ac1fd9c83383d4ac76297cec4569e19222 \
    https://raw.githubusercontent.com/OpenBB-finance/OpenBB/1c74893140292944e71ff5cdd9536edf12f05483/LICENSE \
    /srv/source-tree/OPENBB_LICENSE.txt

RUN mkdir -p /usr/share/licenses/openbb \
    && cp /srv/source-tree/OPENBB_LICENSE.txt \
        /usr/share/licenses/openbb/AGPL-3.0.txt \
    && tar -czf /srv/stonks-openbb-sidecar-source.tar.gz -C /srv/source-tree . \
    && chmod -R a-w /app /srv /usr/share/licenses/openbb \
    && mkdir -p /tmp/openbb \
    && chown -R 65532:65532 /tmp/openbb

USER 65532:65532
EXPOSE 6900

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "6900", "--no-access-log"]
