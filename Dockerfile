# syntax=docker/dockerfile:1
FROM golang:1.25.6-bookworm@sha256:f4490d7b261d73af4543c46ac6597d7d101b6e1755bcdd8c5159fda7046b6b3e AS auth-builder
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY bridge/ ./bridge/
COPY dependencies/ipatool.json ./dependencies/ipatool.json
RUN python3 bridge/setup_ipatool.py

FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS base
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-cryptography curl ca-certificates libstdc++6 libgcc-s1 zlib1g && rm -rf /var/lib/apt/lists/*
WORKDIR /app

FROM base AS engine-builder
COPY ios_download.py ./
COPY bridge/ ./bridge/
COPY dependencies/pastel.json ./dependencies/pastel.json
RUN python3 -c 'import ios_download as c; c.setup(c.runtime_node())' && rm -rf .runtime/npm-cache __pycache__

FROM base AS runtime
COPY --from=engine-builder --chown=1000:1000 /app/ /app/
COPY --from=auth-builder --chown=1000:1000 /app/.runtime/ipatool-auth /app/.runtime/ipatool-auth
COPY --from=auth-builder --chown=1000:1000 /app/.runtime/IPATOOL-BRIDGE-SHA256 /app/.runtime/IPATOOL-BRIDGE-SHA256
COPY --from=auth-builder --chown=1000:1000 /app/.runtime/IPATOOL-REVISION /app/.runtime/IPATOOL-REVISION
COPY --chown=1000:1000 licenses/ ./licenses/
COPY --chown=1000:1000 LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chown=1000:1000 app/ ./app/
COPY --chown=1000:1000 web/ ./web/
COPY --chown=1000:1000 tests/ ./tests/
RUN mkdir -p /data /app/.runtime/device /app/.runtime/sap-cache /app/.runtime/jobs /app/downloads/ipa && chown -R 1000:1000 /app /data
USER 1000:1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health',timeout=3)"
ENTRYPOINT ["python3","-m","app.service"]
