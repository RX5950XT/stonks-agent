# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /workspace/sidecars/nautilus
COPY sidecars/nautilus/pyproject.toml sidecars/nautilus/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
RUN uv sync --frozen --no-dev --no-install-project
RUN python - <<'PY'
import hashlib
from pathlib import Path
from urllib.request import urlopen

url = "https://files.pythonhosted.org/packages/f7/33/af01e6a9d26b9594228a903eb00b7175d82777955cc331aab0faffbc614e/nautilus_trader-1.230.0.tar.gz"
expected = "cc7eaa247e640e46588094fcca44f34f4b6330eeec41e9247068f2500921757d"
payload = urlopen(url, timeout=120).read()
if hashlib.sha256(payload).hexdigest() != expected:
    raise SystemExit("NautilusTrader source sdist hash mismatch")
target = Path("/workspace/nautilus-source/nautilus_trader-1.230.0.tar.gz")
target.parent.mkdir(parents=True)
target.write_bytes(payload)
PY

FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS runtime

ENV HOME=/tmp/sidecar \
    PATH=/workspace/sidecars/nautilus/.venv/bin:$PATH \
    PYTHONPATH=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY --from=build /workspace/sidecars/nautilus/.venv /workspace/sidecars/nautilus/.venv
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
COPY sidecars/__init__.py /workspace/sidecars/__init__.py
COPY sidecars/nautilus /workspace/sidecars/nautilus
COPY scripts/smoke_nautilus.py /workspace/scripts/smoke_nautilus.py
COPY LICENSE /usr/share/licenses/stonks-nautilus-sidecar/APACHE-2.0-LICENSE
COPY --from=build /workspace/nautilus-source /usr/share/source/nautilus-trader
RUN set -eu; \
    license="$(find /workspace/sidecars/nautilus/.venv -path '*nautilus_trader-1.230.0.dist-info/licenses/LICENSE*' -type f -print -quit)"; \
    test -n "$license"; \
    cp "$license" /usr/share/licenses/stonks-nautilus-sidecar/NAUTILUS-LGPL-3.0; \
    cp /usr/share/common-licenses/GPL-3 \
      /usr/share/licenses/stonks-nautilus-sidecar/GNU-GPL-3.0; \
    echo '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986  /usr/share/licenses/stonks-nautilus-sidecar/GNU-GPL-3.0' \
      | sha256sum --check --strict; \
    mkdir -p /tmp/sidecar; \
    chown -R 65532:65532 /tmp/sidecar
USER 65532:65532
EXPOSE 7400
CMD ["uvicorn", "sidecars.nautilus.runtime_app:app", "--host", "0.0.0.0", "--port", "7400", "--no-proxy-headers", "--no-access-log"]
