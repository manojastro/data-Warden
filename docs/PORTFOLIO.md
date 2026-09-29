# Portfolio explanation

## The problem

Data teams lose days to "the revenue number is wrong" incidents: a producer re-sends events, a feed
renames a field, a late batch misses its partition, a join quietly multiplies rows. Detection is the
easy part. The expensive part is the investigation, choosing a safe fix, proving it does not hide
or drop real data, getting sign-off, applying it without collateral damage, and being able to undo it.

## What DataWarden does

- Detects problems with deterministic checks, including reconciliation against an independent
  oracle recomputed from immutable source files.
- Investigates with five specialist agents that must cite evidence from real tools (SQL, dbt
  artifacts, run logs, schema history, git diffs).
- Proposes the smallest safe repair, or explicitly declines (legitimate business change, missing
  contract, unsafe request).
- Proves the repair in an isolated shadow schema against protected checks the agents cannot edit.
- Requires an authorized approver; binds approval to a content hash, code base and data snapshot.
- Applies through a journaled executor with partition locks and snapshots, verifies the canonical
  result, and rolls back (verified) on failure.

## Business value (qualitative — no measured savings are claimed)

- Faster, evidence-backed triage: the reviewer sees the exact query, result hash and diff.
- Fewer bad fixes: policy and oracle validation reject fixes that hide rows, weaken checks, or drop
  legitimate payments — even at 0.99 claimed confidence.
- Controlled blast radius: only approved partitions change; everything else is fingerprinted.
- Auditability: append-only audit of every tool call, denial, approval and recovery step.

## Design trade-offs

| Decision | Why | Cost |
| --- | --- | --- |
| Postgres job queue + outbox instead of Celery/Kafka | One system to operate; leases + SKIP LOCKED are proven | Lower throughput ceiling |
| LangGraph for the controller | Typed state, fan-out/fan-in, durable checkpoints, `interrupt` for approvals | Framework coupling |
| Deterministic routing/permissions, agents only for judgement | Safety properties do not depend on model behaviour | Less "autonomous" |
| Oracle recomputed from source files in Python | Independent of the dbt code under test | Duplicates business logic by design |
| dbt tables + incremental mart with `replay_dates` | Partition-scoped, transactional mart updates; no view renames breaking dependents | Upstream tables are fully rebuilt on promotion |
| Git-versioned runtime workspace | Proposals pin a base commit; promotions and rollbacks are commits | Local only unless the GitHub adapter is configured |
| Fixture model mode | CI and demos without spend; same tools and validation | Says nothing about LLM quality |
| Single Python package | Simple imports and one lockfile | Less separation than the suggested multi-package layout |

## Limitations

- Synthetic single-domain data; nine fault types.
- Evaluation measured in fixture mode; live-model behaviour not measured here.
- No OIDC/Entra ID; demo accounts only. Single organization, three roles.
- Shadow isolation is role-based within one PostgreSQL instance, not a separate sandbox per run.
- Upstream tables are rebuilt on promotion (mart changes are partition-scoped).
- GitHub PR adapter implemented but not exercised against a live repository.

## Resume bullet templates

Replace bracketed placeholders only with numbers you have measured yourself.

- Built a multi-agent data-incident platform (LangGraph, FastAPI, PostgreSQL, dbt, Airflow, React)
  that investigates pipeline faults with five tool-using specialist agents and applies repairs only
  after shadow validation and human approval.
- Designed a verified-recovery saga (partition locks, snapshots, hash-bound approvals, journaled
  rollback) that executed [N] repairs with zero duplicate side effects across [M] replayed
  deliveries in automated tests.
- Implemented an independent reconciliation oracle and protected validation suite that rejected
  [X/Y] deliberately unsafe AI repair proposals, including ones claiming 0.97–0.99 confidence.
- Benchmarked multi-agent vs single-agent vs detection-only workflows on [K] seeded fault scenarios
  (root-cause accuracy [A], valid repair rate [B], false repairs on benign changes [C]) in
  deterministic fixture mode; documented limitations of fixture evaluation.
