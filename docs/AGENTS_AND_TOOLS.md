# Agent and tool contracts

## Structured contracts (`src/datawarden/contracts/agents.py`)

```text
Hypothesis(id, category, description, supporting_evidence_ids, contradicting_evidence_ids, status, next_query)
AgentFinding(agent_id, summary, symptoms, hypotheses, evidence_ids, affected_assets, affected_partitions,
             business_reports, root_cause, conclusive, recommended_next_step, next_query, uncertainty)
RepairProposalDraft(kind, summary, files, asset_scope, partition_scope, preconditions, risks, rollback_plan,
                    claimed_confidence, escalation_reason)
ValidationResult(proposal_hash, input_snapshot, check_id, expected, observed, status, artifact_ref)
VerificationVerdict(decision, reasons, requested_checks, missing_coverage, uncertainty)
```

Persisted forms: `hypotheses`, `evidence` (id, asset_id, query_or_tool_ref, artifact_hash,
collected_at, summary, redaction_status, untrusted_text), `repair_proposals` (id, revision,
patch_hash, base_commit, asset_scope, partition_scope, preconditions, rollback_plan, …),
`validations`.

Evidence ids are content-addressed: `E-<incident>-<sha256(tool, args, output)[:10]>`. The same
query with the same result yields the same id, which is how the controller detects "repeated
identical queries without new evidence".

`uncertainty` and `claimed_confidence` are uncalibrated model estimates. They are displayed and
logged but never used for permission, routing past a check, or success.

## Agent loop (`agents/specialists.py`)

```
for step in range(max_steps):
    decision = model.decide(turn)                # tool call or final
    if final: validate against the output schema; stop
    result = tools.invoke(caller=agent, tool, args, operation_id=f"{incident}:{agent}:{round}:{step}")
    append observation (status, evidence id, bounded output)
    stop on: budget exhausted | 2 repeats without new evidence | active-time deadline
```

A stopped agent returns a well-formed finding with `uncertainty=high` and the stop reason; it never
raises into the graph. At most 3 specialists run concurrently per process (semaphore).

## Tools (`tools/readonly.py`, `tools/recovery.py`)

Every tool has a Pydantic input schema, an allowlist of callers, a timeout, a bounded output size,
explicit error results, and persistence (`tool_calls` row + `evidence` row) before the result is
returned. Tool-call budget (25 per incident) is consumed atomically in SQL across parallel branches.

| Tool | Callers | Timeout | Effect |
| --- | --- | --- | --- |
| get_asset_metadata | investigators, planner, analyst, controller, api, mcp | 20 s | read |
| get_quality_results | same | 20 s | read |
| get_pipeline_run | investigators, controller, api, mcp | 20 s | read |
| get_pipeline_logs | investigators, api | 20 s | read (untrusted text) |
| get_schema_history | investigators, planner, api, mcp | 20 s | read (contracts + mappings) |
| get_lineage_neighbors | readers | 20 s | read (depth ≤ 3) |
| get_recent_code_changes | investigators, planner, api | 20 s | read (git log/diff) |
| run_readonly_sql | investigators | 20 s (5 s statement) | read, ≤ 100 rows |
| sample_redacted_rows | investigators | 20 s | read, ≤ 20 rows, PII hidden, free text untrusted |
| calculate_business_impact | investigators, planner, api | 20 s | read |
| propose_patch | repair_planner | 20 s | writes a proposal record only |
| create_shadow_environment | controller | 180 s | shadow schema + code copy |
| run_shadow_pipeline | controller | 600 s | dbt build + tests in shadow |
| run_protected_validation | controller, verification_analyst | 300 s | read; records validations |
| request_approval | controller | 20 s | pending approval |
| execute_approved_repair | executor | 1200 s | canonical write (saga) |
| verify_canonical_outputs | executor | 300 s | read |
| rollback_repair | executor | 900 s | canonical write (restore) |

There is no shell, file-read, network, or arbitrary-command tool. dbt runs only through fixed
command templates (`pipeline/dbt.py`): allowed commands `run|test|parse`, selectors restricted to
known models, vars validated per key, one database password per target.

## Model adapters (`agents/models.py`)

| Mode | Config | Behaviour |
| --- | --- | --- |
| fixture | `DW_MODEL_PROVIDER=fixture` | Deterministic rules over structured observations; token counts are estimates; cost "unavailable" |
| OpenAI-compatible | `openai_compatible`, `DW_MODEL_ENDPOINT`, `DW_MODEL_NAME`, `DW_MODEL_API_KEY` | Function calling; `submit_final` tool carries the output schema; ≤ 3 attempts on invalid JSON/rate limits |
| Azure OpenAI | `azure_openai` + `DW_MODEL_API_VERSION`; `DW_MODEL_NAME` = deployment | Same |

Pricing: set `DW_MODEL_PRICE_INPUT_PER_1K` and `DW_MODEL_PRICE_OUTPUT_PER_1K` to get cost estimates;
otherwise cost is reported as unavailable, never zero.

Live mode was **not** exercised in this build (no endpoint/key configured).
