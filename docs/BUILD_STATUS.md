# Build status

Legend: **done+tested** — covered by an automated test that ran and passed; **done+verified** —
implemented and exercised manually (evidence noted); **fixture** — exercised with deterministic
model fixtures only; **implemented, not verified** — code exists but was not run against the real
external system; **disabled** — needs configuration; **todo**.

Environment used: Linux container, 4 vCPU / 15 GB RAM, PostgreSQL 16 (host) and Docker Compose
(PostgreSQL 16-alpine, Airflow 3.1.0), Python 3.11, Node 22. Model mode: **fixture** throughout.

## Milestones

| Milestone | Status |
| --- | --- |
| 1 Data foundation | done+tested |
| 2 Incident platform | done+tested |
| 3 Multi-agent investigation | done+tested (fixture) |
| 4 Verified recovery | done+tested |
| 5 Integrations and UX | see table below |
| 6 Evaluation and handoff | see table below |

## Feature status

| Feature | Status | Evidence |
| --- | --- | --- |
| Generator (seeded, 30 days, 57.5k events, split-tender payments) | done+tested | `tests/test_generator.py` |
| Warehouse roles & schemas, append-only raw | done+tested | `test_ingestion_is_idempotent_and_raw_is_append_only`, `test_readonly_sql_cannot_reach_protected_data` |
| Contract-checked idempotent ingestion | done+tested | M1 tests, schema-drift scenarios |
| dbt models, incremental mart, `replay_dates` | done+tested | late-events scenario (only day 20 changes) |
| Independent oracle + 21 checks | done+tested | `test_healthy_pipeline_reconciles_to_source_truth` |
| Fault injector (9 scenarios) + scoped reset | done+tested | every scenario test injects and resets |
| App DB (Alembic), append-only audit trigger | done+tested | migrations applied in tests and Compose |
| Auth (argon2, sessions, CSRF), roles, permission matrix | done+tested | `tests/test_api.py`, viewer/operator scenario test |
| Signed + idempotent event ingestion | done+tested | `tests/test_api.py` |
| Job queue (SKIP LOCKED, leases, heartbeat, backoff), outbox, worker | done+tested | all scenario tests run through it; worker-restart test uses a new process |
| SSE with persisted ids / timeline resume | done+tested (resume via `after_id`); browser EventSource verified manually | `test_timeline_resume_after_event_id`, screenshots |
| Typed tools, allowlists, budgets, timeouts, bounded output, evidence | done+tested | isolation, budget, injection tests |
| LangGraph graph (fan-out/join, loops, budgets, checkpoints, interrupt) | done+tested (fixture) | all scenario tests |
| Single-agent baseline graph variant | done+verified (fixture) | eval runner |
| Model adapters: fixture | done+tested | |
| Model adapters: OpenAI-compatible / Azure OpenAI | implemented, not verified (no endpoint/key) | `agents/models.py` |
| Proposal policy | done+tested | faulty-proposal scenario |
| Shadow schemas + protected validation (11 checks) | done+tested | every repair scenario |
| Approval (hash-bound, expiry, optimistic concurrency, invalidation → revision) | done+tested | stale/modified test, duplicate-approval test |
| Executor saga, partition locks, snapshots, verification | done+tested | repair scenarios, conflict test |
| Verified rollback; failed rollback → manual_intervention | rollback done+tested (chaos hook); manual-intervention path implemented, not triggered by a test | rollback test |
| Dashboard (8 page groups) | done+verified (Playwright screenshots) | see e2e row |
| Playwright journey | see "Tests run" | `apps/web/e2e/journey.spec.ts` |
| Docker Compose core profile | done+verified | `docker compose … --profile full up --wait` all healthy |
| Airflow full profile (DAG, failure callback, adapter) | see "Airflow" below | |
| GitHub adapter | implemented, not verified (no token) | `integrations/github.py` |
| Read-only MCP server | done+tested (in-process calls) | `test_mcp_server_is_read_only_and_audited` |
| Webhooks | disabled by default; SSRF guard implemented, not verified live | `services/notify.py` |
| Langfuse | disabled (not integrated beyond status reporting) | |
| OpenTelemetry | spans created in-process; OTLP export only when configured; collector profile provided, not verified | `observability.py` |
| Evaluation benchmark + report | see `docs/EVALUATION.md` | `evals/runner.py` |
| OIDC / Entra ID | todo (extension seam only) | |

## Tests run

(Updated at the end of the build; see the final section.)

## Commands verified

```
python3 scripts/gen_env.py
uv run dw setup-db && uv run alembic upgrade head
uv run dw seed && uv run dw app-seed
uv run dw fault inject duplicate_payments && uv run dw pipeline --emit && uv run dw worker --drain --wait 6
uv run pytest
DW_BUILD_EXTRA_CA=… docker compose -f infra/docker-compose.yml --env-file .env --profile full build
docker compose -f infra/docker-compose.yml --env-file .env --profile full up -d --wait
```
