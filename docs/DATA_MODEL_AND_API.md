# Data model and API

## Warehouse (`datawarden_wh`)

| Schema | Contents | Written by |
| --- | --- | --- |
| `raw` | `raw_order_events`, `raw_payment_events`, `raw_refund_events`, `raw_customer_events` — one row per delivered source record with `_batch_id`, `_source_checksum`, `_line_no`, `_run_id`, `_ingested_at`. Append-only (trigger). | `dw_pipeline` |
| `staging` | `stg_orders` (per order), `stg_payments` (per payment event), `stg_refunds` (per refund event) | `dw_transformer` (dbt) |
| `marts` | `dim_customers`, `fct_orders`, `fct_payments` (per payment attempt), `fct_refunds`, `mart_daily_revenue` (per Asia/Kolkata business date, incremental delete+insert) | `dw_transformer` (dbt) |
| `ops` | `ingested_batches`, `pipeline_runs`, `task_runs`, `run_logs`, `schema_observations`, `quality_results`, `demo_settings` | `dw_pipeline` |
| `recovery` | `snap_<operation>` pre-change partition snapshots | `dw_executor` |
| `shadow_<incident>_r<rev>` | pending-batch tables, source views, full shadow build | `dw_shadow` |

Money is `bigint` paise everywhere. Revenue definition: gross = captured payments by payment
business date; refunds = succeeded refunds by refund business date; net = gross − refunds.
Payments and refunds are aggregated independently before joining.

## Application database (`datawarden_app`, Alembic migrations in `migrations/`)

`roles`, `users`, `sessions`, `assets`, `lineage_edges`, `pipeline_runs`, `quality_checks`,
`quality_results`, `incident_events`, `incidents`, `agent_runs`, `hypotheses`, `evidence`,
`tool_calls`, `repair_proposals`, `validations`, `approvals`, `recovery_operations`,
`partition_locks`, `audit_events` (append-only trigger), `stream_events` (SSE ids), `jobs`,
`outbox_events`, `worker_heartbeats`, `artifacts`, `model_usage`, `evaluation_runs`, plus
LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, …).

Key constraints: `incident_events.idempotency_key` unique; `tool_calls.operation_id` unique;
`repair_proposals (incident_id, revision)` unique; `approvals.proposal_id` unique;
`recovery_operations.operation_key` unique (`proposal_hash:approval_id`); `partition_locks.lock_key`
primary key; `jobs.dedup_key` unique.

## REST API (`/api/v1`, OpenAPI at `/api/openapi.json`, docs at `/api/docs`)

| Area | Endpoints | Auth |
| --- | --- | --- |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | session cookie; CSRF header on mutations |
| Catalog | `GET /assets`, `GET /assets/{id}`, `GET /lineage`, `GET /checks`, `GET /checks/{id}/results` | read |
| Runs | `GET /pipeline-runs`, `GET /pipeline-runs/{id}` (tasks + logs), `POST /pipeline-runs` | read / `pipeline.run` |
| Ingestion | `POST /events` | HMAC signature (`X-DW-Timestamp`, `X-DW-Signature`) + `Idempotency-Key` |
| Incidents | `GET /incidents` (paginated, `status`, `severity`), `GET /incidents/{id}`, `POST /incidents/{id}/start`, `POST /incidents/{id}/cancel` | read / `incident.*` |
| Investigation | `GET /incidents/{id}/evidence`, `/tool-calls`, `/timeline?after_id=`, `/graph-status`, `GET /evidence/{id}`, `GET /graph/definition` | read |
| Live | `GET /stream?incident_id=` (SSE; resumes from `Last-Event-ID`) | read |
| Repair | `GET /proposals/{id}`, `GET /proposals/{id}/comparison`, `GET /approvals`, `POST /approvals/{id}/decision` (`version` for optimistic concurrency, `Idempotency-Key` required) | read / `approval.decide` |
| Recovery | `GET /incidents/{id}/recovery`, `GET /recovery-operations` | read |
| Demo | `GET /demo/scenarios`, `POST /demo/faults`, `POST /demo/reset` (demo mode only) | `demo.control` |
| Evaluation | `POST /evaluations`, `GET /evaluations`, `GET /evaluations/{id}`, `GET /usage` | `eval.run` / read |
| System | `GET /healthz`, `GET /readyz`, `GET /integrations`, `GET /jobs`, `GET /jobs/{id}`, `GET /audit` | none / read |

Errors are JSON `{"error", "request_id"}`; every response carries `X-Request-ID`. Request bodies are
limited to 1 MB. CORS origins are explicit (`DW_CORS_ORIGINS`).

## Event contract (`contracts/events.py`)

```json
{
  "source": "pipeline | airflow | check_runner | api",
  "event_type": "pipeline_run | check_result | pipeline_task_failed",
  "occurred_at": "2026-09-29T18:00:00Z",
  "run": {"run_id": "...", "status": "...", "trigger": "...", "code_commit": "...",
          "source_watermark": 120, "tasks": {...}, "checks": {...}, "results": [...]},
  "check_id": "reconciliation.mart_daily_revenue", "check_type": "reconciliation",
  "asset": "mart_daily_revenue", "status": "fail", "severity": "critical",
  "message": "...", "observed": {...}, "partition_date": null
}
```

Correlation: events from the same pipeline run join one incident (`run:<run_id>`); otherwise an
active incident on the same asset absorbs the event; otherwise a new incident opens and an
investigation job is enqueued through the outbox (3 s debounce so a run's events arrive together).
