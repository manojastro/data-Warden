# Build status

Legend: **done+tested** (automated test exercises it), **done** (implemented, manually verified),
**fixture** (runs with deterministic model fixtures), **disabled** (needs configuration),
**blocked** (external dependency missing), **todo**.

## Milestone 1 – Data foundation: done+tested

| Item | Status | Evidence |
| --- | --- | --- |
| Deterministic generator (30 days, 57,414 events, seed 42) | done+tested | `tests/test_generator.py` |
| Immutable, checksummed source store | done+tested | `src/datawarden/sources.py` |
| Warehouse roles/schemas (pipeline, transformer, executor, agent_ro, shadow, validator) | done+tested | `src/datawarden/warehouse/bootstrap.py`; append-only test |
| Contract-checked, idempotent ingestion | done+tested | `test_ingestion_is_idempotent_and_raw_is_append_only`, `test_schema_drift_rejects_whole_batch` |
| dbt staging/facts/mart (incremental, bounded replay) | done+tested | `pipelines/dbt/`; `test_late_events_need_bounded_replay` |
| Independent reconciliation oracle | done+tested | `src/datawarden/oracle/`; `test_healthy_pipeline_reconciles_to_source_truth` |
| 21 protected checks + dbt tests + pipeline task results | done+tested | `src/datawarden/checks/registry.py` |
| Fault injector (9 scenarios incl. 2 schema-drift variants) + scoped reset | done (all 9 manually verified, 3 automated) | `dw fault inject <scenario>` |

Acceptance evidence (2026-09-29, host PostgreSQL 16, seed 42):
- Healthy: `dw checks --all` → 21/21 pass, 30 mart partitions reconcile.
- `duplicate_payments`: reconciliation fails on exactly the 3 ground-truth dates; uniqueness fails (81 duplicate rows).
- Each other scenario produces its expected symptom (see `docs/FAULT_CATALOG.md` once written).
- `uv run pytest` → 9 passed (≈3 min, dominated by dbt full-refresh rebuilds).

## Commands verified
```
python3 scripts/gen_env.py      # .env with generated secrets
uv run dw setup-db              # roles, databases, schemas (admin credential)
uv run dw seed                  # generate sources, full pipeline build
uv run dw checks --all
uv run dw fault inject duplicate_payments && uv run dw pipeline
uv run dw fault reset
uv run pytest
```

## Next
Milestone 2: application DB migrations, FastAPI, auth/roles, job queue + worker, signed event
ingestion, read-only tools, basic UI.
