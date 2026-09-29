# Deployment guide

No cloud resources are provisioned by this repository. This guide describes a private Linux VM
deployment (tested locally with Docker Compose) and an Azure proposal (not executed).

## A. Private Linux VM (Docker Compose)

Tested resources: 4 vCPU, 15 GB RAM, ~6 GB disk for images and data. Observed usage with the full
profile: under 3 GB RAM; a scenario investigation plus shadow validation takes 20–35 s; a full
seed takes ~30–40 s.

1. Install Docker Engine + Compose plugin, `git`, `make`, Python 3.11 (only for `scripts/gen_env.py`).
2. `git clone … && cd data-Warden && python3 scripts/gen_env.py` — creates `.env` (mode 600) with
   generated secrets. Never commit it.
3. Choose a profile:
   - `make up` (core: databases, migrations, API + dashboard, worker)
   - `make up PROFILE=full` (adds Airflow on `127.0.0.1:8080`)
   - `make up PROFILE=observability` (adds an OpenTelemetry collector)
4. `make seed` (inside Compose: `docker compose -f infra/docker-compose.yml exec worker dw seed && … dw app-seed`).
5. Put a TLS reverse proxy (Caddy/nginx) in front of `127.0.0.1:8000`. Set `DW_ENV=vm` so session
   cookies are `Secure`, and set `DW_CORS_ORIGINS` to your hostname.
6. Back up the `app-db` and `warehouse` volumes (pg_dump nightly) and the `artifacts` volume
   (source fixtures, runtime workspace, recovery metadata).
7. Behind a TLS-intercepting proxy, build with `DW_BUILD_EXTRA_CA=/path/to/ca.pem make up` (the CA
   is passed as a BuildKit secret, not baked into images).

Hardening checklist: keep ports bound to 127.0.0.1; rotate `.env` secrets; set `DW_DEMO_MODE=false`
for anything that is not synthetic (disables fault injection, reset, and chaos hooks); restrict SSH;
ship JSON logs to your log system.

## B. Azure proposal (not provisioned)

| Component | Azure service | Notes |
| --- | --- | --- |
| API + dashboard | Azure Container Apps (external ingress, min 1 replica) | Managed identity; secrets from Key Vault |
| Worker | Container Apps (internal, 1–3 replicas) | Leases/heartbeats make horizontal scale safe |
| App DB + checkpoints | Azure Database for PostgreSQL Flexible Server | Private endpoint; PITR backups |
| Warehouse | Separate PostgreSQL Flexible Server (or Fabric/Synapse later behind the same tools) | Roles as in `warehouse/bootstrap.py` |
| Artifacts | Azure Files (mounted) initially; Blob via the artifact-store seam later | Runtime git workspace needs a POSIX mount |
| Scheduling | Managed Airflow in Azure Data Factory, or Container Apps jobs calling `dw pipeline --emit` | Same pipeline CLI |
| Models | Azure OpenAI (`DW_MODEL_PROVIDER=azure_openai`, deployment name, API version) | Private endpoint; configure pricing for cost estimates |
| Identity | Entra ID OIDC | **Not implemented** — the session layer has an extension seam only |
| Observability | Application Insights via OTLP | `DW_OTEL_EXPORTER_OTLP_ENDPOINT` |
| Secrets | Key Vault references | Replace `.env` |

Estimated monthly cost is not stated here; size it with the Azure pricing calculator for your region.
