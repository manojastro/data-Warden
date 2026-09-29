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
| 5 Integrations and UX | done+verified (Airflow, dashboard, MCP); GitHub adapter not verified |
| 6 Evaluation and handoff | done (fixture-mode evaluation; see limitations) |

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
| Playwright journey | done+tested against the Compose stack | `apps/web/e2e/journey.spec.ts` |
| Docker Compose core profile | done+verified | `docker compose … --profile full up --wait` all healthy |
| Airflow full profile (DAG, failure callback, adapter) | done+verified | see "Airflow verification" below |
| GitHub adapter | implemented, not verified (no token) | `integrations/github.py` |
| Read-only MCP server | done+tested (in-process calls) | `test_mcp_server_is_read_only_and_audited` |
| Webhooks | disabled by default; SSRF guard implemented, not verified live | `services/notify.py` |
| Langfuse | disabled (not integrated beyond status reporting) | |
| OpenTelemetry | spans created in-process; OTLP export only when configured; collector profile provided, not verified | `observability.py` |
| Evaluation benchmark + report | done (fixture mode, 60 runs) | `docs/EVALUATION.md`, `evals/results/` |
| OIDC / Entra ID | todo (extension seam only) | |

## Tests run (2026-09-29, fixture model mode)

| Suite | Result |
| --- | --- |
| `uv run pytest` (33 tests: generator, isolation, API/security, M1 pipeline, 17 end-to-end scenarios) | 32 passed, 1 failed on the full run. The failure was a test-only defect (a fixed record id left over from an earlier run in the same database). Fixed; the test was re-run alone and passed. The full suite was **not** re-run end-to-end after that one-line test fix. Duration ≈ 20 min. |
| Playwright journey (sign in → inject → investigation → validated patch → approve as approver → recovery → healthy check) against Docker Compose | 2 consecutive passes (≈1.4 min each) after fixing a real bug it found (`/assets/*` deep links collided with static files). One earlier attempt stopped at the sign-in page right after a container restart; not reproduced. |
| `ruff check`, `ruff format --check`, `tsc -b`, `vite build` | clean |
| Evaluation (`dw eval --seeds 42,1337`: 10 scenarios × 3 modes × 2 seeds = 60 runs) | completed; see `docs/EVALUATION.md` |

Scenario coverage (spec §14, 1-18): 1 healthy reconcile, 2 duplicate repaired, 3 distinct legit payments survive,
4 schema drift both variants, 5 late replay, 6 join fan-out, 7 transient failure, 8 genuine decline, 9 bad proposal,
10 prompt injection, 11 worker restart (new process), 12 duplicate approvals/events, 13 modified/stale input,
14 conflicting partitions, 15 verified rollback, 16 viewer restrictions, 17 budget/tool failure escalation,
18 oracle isolation — each has a dedicated automated test.

## Airflow verification (full profile, Docker)

`docker compose --profile full up --wait` → all services healthy (Airflow 3.1.0 standalone). With the
`transient_failure` fault injected, a DAG run triggered through the Airflow REST adapter failed; the
incident received both the pipeline's own check events and the Airflow failure-callback event
(`airflow.datawarden_retail_revenue.run_pipeline`, source `airflow`) on the same incident, which was
investigated (root cause: transient pipeline failure), validated, approved through the API, replayed
and resolved ("repair applied and canonical outputs verified").

## Evaluation summary (fixture mode; not evidence of LLM quality)

Root-cause accuracy 1.00 and valid-repair rate 1.00 for both agent modes (0.00 for detection-only);
unsafe-proposal rejection 4/4; false repairs on benign runs 0; idempotency probe 12/12 at most once.
Single- and multi-agent reached identical outcomes; multi-agent used ≈35% fewer estimated tokens.
Sample sizes are small (Wilson intervals in the report).

## Known limitations and blockers

- Live LLM provider not exercised (no endpoint/key): adapters implemented, fixture mode only.
- GitHub adapter and outbound webhooks implemented but not verified against live services.
- OpenTelemetry collector profile provided but not verified; Langfuse only reported as disabled.
- OIDC/Entra ID not implemented.
- The manual-intervention path (rollback itself failing) is implemented but not triggered by a test.
- In this sandbox, Docker builds need `DW_BUILD_EXTRA_CA` because outbound TLS is intercepted.

## `.env` fields to fill locally

Generated automatically by `scripts/gen_env.py`. Fill only if used: `DW_PG_ADMIN_PASSWORD` (host
mode: your PostgreSQL superuser password), `DW_MODEL_PROVIDER`/`DW_MODEL_ENDPOINT`/`DW_MODEL_NAME`/
`DW_MODEL_API_KEY`/`DW_MODEL_API_VERSION`, optional `DW_MODEL_PRICE_*`, `DW_GITHUB_TOKEN`/`DW_GITHUB_REPO`,
`DW_WEBHOOK_URL`, `DW_LANGFUSE_HOST`, `DW_OTEL_EXPORTER_OTLP_ENDPOINT`.

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
