# DataWarden application image (API, worker, pipeline runner, CLI) with the built dashboard.
FROM node:22-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
# Optional build secret `extra_ca`: an extra CA bundle for TLS-intercepting proxies (empty by default).
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY apps/web ./
RUN npm run build

# The full bookworm image already ships git (runtime workspace) and curl (health checks), so no
# apt step is needed.
FROM python:3.11-bookworm AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DW_REPO_ROOT=/app DW_ARTIFACT_DIR=/artifacts
RUN useradd -m -u 1000 -g 0 dw
WORKDIR /app
COPY infra/requirements.lock.txt ./infra/requirements.lock.txt
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca REQUESTS_CA_BUNDLE=/run/secrets/extra_ca; fi; \
    python -m venv /opt/venv && /opt/venv/bin/pip install -r infra/requirements.lock.txt
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY pipelines ./pipelines
COPY data ./data
COPY scripts ./scripts
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca REQUESTS_CA_BUNDLE=/run/secrets/extra_ca; fi; \
    pip install --no-deps -e . && dw --help >/dev/null
COPY --from=web /web/dist ./apps/web/dist
RUN mkdir -p /artifacts && chown -R 1000:0 /artifacts /app && chmod -R g+w /artifacts
USER 1000:0
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["uvicorn", "datawarden.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
