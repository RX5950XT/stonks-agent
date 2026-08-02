# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.27-python3.12-bookworm-slim@sha256:a0cb65a2dada255d392fe0426d04ed50911f0b78b3295de0c3286c1c86148b15 AS python-build

ENV UV_LINK_MODE=copy
WORKDIR /workspace/sidecars/lean
COPY sidecars/lean/pyproject.toml sidecars/lean/uv.lock ./
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
RUN uv export --frozen --no-dev --no-emit-project --no-emit-local \
      --format requirements.txt --output-file /tmp/requirements.txt \
    && uv pip install --python /usr/local/bin/python3 --target /opt/python-site \
      --requirement /tmp/requirements.txt

FROM mcr.microsoft.com/dotnet/sdk:10.0-noble@sha256:ed034a8bf0b24ded0cbbac07e17825d8e9ebfe21e308191d0f7421eaf5ad4664 AS lean-build

ARG LEAN_COMMIT=c22774e49ee80ecef5ca84f57616f6b66fad8bc5
ARG LEAN_ARCHIVE_SHA256=258a6db94c942e77488e47cb8e5c873c2cc2cbc7e4bc1bce5d14ea4f31b6628f
ENV NUGET_PACKAGES=/root/.nuget/packages \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1
WORKDIR /src
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      "https://codeload.github.com/QuantConnect/Lean/tar.gz/${LEAN_COMMIT}" \
      --output /tmp/lean-source.tar.gz \
    && echo "${LEAN_ARCHIVE_SHA256}  /tmp/lean-source.tar.gz" \
      | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/lean-source.tar.gz \
      --strip-components=1 --directory /src

COPY sidecars/lean/patches /tmp/stonks-patches
RUN git apply --check /tmp/stonks-patches/0001-remove-vulnerable-runtime-dependencies.patch \
    && git apply /tmp/stonks-patches/0001-remove-vulnerable-runtime-dependencies.patch
COPY sidecars/lean/engine/IonicZipCompat.cs /src/Compression/IonicZipCompat.cs
COPY sidecars/lean/engine/CanonicalBacktestAlgorithm.cs /src/StonksAlgorithm/CanonicalBacktestAlgorithm.cs
COPY sidecars/lean/engine/Stonks.Lean.Algorithm.csproj /src/StonksAlgorithm/Stonks.Lean.Algorithm.csproj
COPY sidecars/lean/dotnet-locks /tmp/dotnet-locks
RUN set -eu; \
    for project in Algorithm Algorithm.CSharp Algorithm.Framework AlgorithmFactory \
      Api Brokerages Common Compression Configuration Engine Indicators Launcher \
      Logging Messaging Queues StonksAlgorithm; do \
        cp "/tmp/dotnet-locks/${project}/packages.lock.json" \
          "/src/${project}/packages.lock.json"; \
    done; \
    dotnet restore Launcher/QuantConnect.Lean.Launcher.csproj --locked-mode; \
    dotnet restore StonksAlgorithm/Stonks.Lean.Algorithm.csproj --locked-mode; \
    dotnet list Launcher/QuantConnect.Lean.Launcher.csproj package \
      --vulnerable --include-transitive --format json \
      > /tmp/launcher-vulnerabilities.json; \
    dotnet list StonksAlgorithm/Stonks.Lean.Algorithm.csproj package \
      --vulnerable --include-transitive --format json \
      > /tmp/algorithm-vulnerabilities.json; \
    ! grep -q '"vulnerabilities"' /tmp/launcher-vulnerabilities.json; \
    ! grep -q '"vulnerabilities"' /tmp/algorithm-vulnerabilities.json; \
    dotnet publish Launcher/QuantConnect.Lean.Launcher.csproj -c Release \
      --no-restore --no-self-contained --output /opt/lean; \
    dotnet build StonksAlgorithm/Stonks.Lean.Algorithm.csproj -c Release \
      --no-restore; \
    cp StonksAlgorithm/bin/Release/net10.0/Stonks.Lean.Algorithm.dll /opt/lean/; \
    mkdir -p /opt/lean/Data/market-hours /opt/lean/Data/symbol-properties; \
    cp Data/market-hours/market-hours-database.json /opt/lean/Data/market-hours/; \
    cp Data/symbol-properties/security-database.csv /opt/lean/Data/symbol-properties/; \
    cp Data/symbol-properties/symbol-properties-database.csv /opt/lean/Data/symbol-properties/

FROM mcr.microsoft.com/dotnet/runtime:10.0-noble@sha256:ed5d539b27842d656a06a5984dbcb5114d3e885fbada612a49a5a7c3c3a44e1c AS runtime

ENV HOME=/tmp/sidecar \
    PYTHONPATH=/opt/python-site:/workspace:/workspace/packages/contracts/src:/workspace/packages/service-auth/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_EnableDiagnostics=0 \
    COMPlus_EnableDiagnostics=0
WORKDIR /workspace
RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/python-site /opt/python-site
COPY --from=lean-build /opt/lean /opt/lean
COPY packages/contracts /workspace/packages/contracts
COPY packages/service-auth /workspace/packages/service-auth
COPY sidecars/__init__.py /workspace/sidecars/__init__.py
COPY sidecars/lean /workspace/sidecars/lean
COPY scripts/smoke_lean.py /workspace/scripts/smoke_lean.py
COPY LICENSE /usr/share/licenses/stonks-lean-sidecar/STONKS-APACHE-2.0
COPY --from=lean-build /src/LICENSE /usr/share/licenses/stonks-lean-sidecar/LEAN-APACHE-2.0
COPY --from=lean-build /tmp/lean-source.tar.gz /usr/share/source/lean/lean-source.tar.gz
COPY sidecars/lean/patches /usr/share/source/lean/stonks-modifications/patches
COPY sidecars/lean/engine /usr/share/source/lean/stonks-modifications/engine
RUN mkdir -p /tmp/sidecar /tmp/lean-jobs \
    && chown -R 65532:65532 /tmp/sidecar /tmp/lean-jobs
USER 65532:65532
EXPOSE 7410
CMD ["python3", "-m", "uvicorn", "sidecars.lean.runtime_app:app", "--host", "0.0.0.0", "--port", "7410", "--no-proxy-headers", "--no-access-log"]
