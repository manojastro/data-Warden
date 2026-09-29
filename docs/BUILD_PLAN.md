# DataWarden build plan

Source specification: [`docs/SPEC.md`](SPEC.md) (extracted from the uploaded build prompt).
Live progress and evidence: [`docs/BUILD_STATUS.md`](BUILD_STATUS.md).

## Starting point

The repository contained only a one-line `README.md`. No `AGENTS.md`, no configuration,
no prior code. The whole platform is built from scratch in this repository root
(the repository itself plays the role of the `datawarden` directory).

## Layout

| Path | Purpose |
| --- | --- |
| `src/datawarden/` | Single Python distribution (API, worker, agents, tools, contracts, pipeline, oracle, checks, recovery). Sub-packages mirror the spec's `apps/api`, `services/worker`, `packages/*` split. |
| `apps/web/` | React + TypeScript + Vite + Tailwind + React Flow dashboard. |
| `pipelines/dbt/` | Baseline dbt project (copied into a git-versioned runtime workspace). |
| `pipelines/contracts/` | Versioned source schema contracts (producer-owned, not patchable). |
| `pipelines/ingestion/` | Ingestion column mappings (patchable by approved repairs). |
| `pipelines/airflow/` | Airflow DAG for the `full` profile. |
| `data/scenarios/` | Public fault scenario catalog (no answers). |
| `evals/` | Hidden ground-truth labels, benchmark runner output. Never exposed to agents. |
| `infra/` | Dockerfiles, compose, warehouse bootstrap SQL. |
| `tests/` | pytest suites (unit, integration, scenario). |
| `artifacts/` | Runtime artifacts (git-ignored): source fixtures, dbt artifacts, reports, snapshots. |

## Milestones

1. **Data foundation** – deterministic generator (30 days, >50k events), PostgreSQL warehouse
   with scoped roles, append-only ingestion with schema contracts, dbt staging/facts/mart,
   independent Python reconciliation oracle, deterministic checks, fault injector, CLI.
   Acceptance: healthy data reconciles, injected duplicate fault fails reconciliation.
2. **Incident platform** – Alembic schema, FastAPI (auth, roles, assets, runs, signed event
   ingestion, incidents, SSE), Postgres job queue with leases + worker, read-only typed tools,
   basic UI. Acceptance: failed check creates incident, UI shows real evidence.
3. **Multi-agent investigation** – typed contracts, LangGraph graph (fan-out, join, loops,
   budgets, Postgres checkpoints), fixture + live model adapters.
   Acceptance: duplicate fault investigated correctly; legitimate decline closes without repair.
4. **Verified recovery** – shadow schemas, proposal policy, protected validation, approval
   interrupt, executor saga with journal, snapshots, rollback.
   Acceptance: good repair succeeds, bad repair rejected, crash/retry is idempotent.
5. **Integrations and UX** – Airflow full profile, lineage from dbt manifest, full dashboard,
   optional GitHub adapter, read-only MCP server.
6. **Evaluation and handoff** – all fault scenarios, security/resilience tests, baselines,
   benchmark report, runbooks, threat model, demo script, deployment guides.
