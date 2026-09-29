# Assumptions

Routine, reversible decisions made while building. Each can be changed later.

## Project and runtime

- The repository root is the project root (the spec's "create a directory named `datawarden`"
  applies only when no project exists; this repository *is* the project).
- Python 3.11, managed with `uv` (`uv.lock` is the Python lockfile). Node 22 + npm
  (`package-lock.json`) for the dashboard.
- One Python distribution (`src/datawarden`) instead of several packages. It keeps imports and
  lockfiles simple; the sub-packages follow the spec's component split.
- Local development can run against a host PostgreSQL 16 cluster or Docker Compose. Both use
  two logical databases: `datawarden_app` (application state + LangGraph checkpoints) and
  `datawarden_wh` (warehouse). In Compose they are two separate services.

## Data and business rules

- Currency: INR stored as integer **paise** (`BIGINT`). No floating point anywhere in money.
- Business timezone: `Asia/Kolkata` (IST, UTC+05:30, no DST). Timestamps are stored in UTC
  (`timestamptz`). `business_date = (event_ts AT TIME ZONE 'Asia/Kolkata')::date`.
- Demo period: 30 business days starting `2026-08-01`, seed `42` by default.
- Event identity is the source `event_id`. A re-delivered event with the same `event_id` is a
  duplicate, not a new fact. Distinct `payment_id`s are distinct payments even when order and
  amount match (retries after a failure).
- Revenue: gross collected = captured payments by payment business date; refunds = succeeded
  refunds by refund business date; net = gross - refunds. Failed/pending payments and failed
  refunds count zero.
- Late data: the scheduled incremental mart build recomputes the latest
  `DW_MART_LOOKBACK_DAYS` (default 2) business dates relative to the newest business date in
  the data. Older partitions are only recomputed by an explicit, approved replay. Raw events are
  retained for the whole demo period, so any demo partition can be recomputed.
- Source watermark: the highest `delivery_seq` of ingested source batches. A proposal records
  the watermark it was validated against; a newer watermark invalidates approval.
- Synthetic clock: freshness is measured against the newest *delivered* batch in the source
  manifest, not wall-clock time, so the demo is reproducible on any date.

## Safety

- The reconciliation oracle is Python code reading the immutable source fixtures directly. It
  never reads dbt outputs. Agents have no tool that reaches it or the `protected` schema.
- Fault labels live under `evals/labels/` and are read only by the evaluation harness.
- Fixture model mode is a deterministic, rule-based stand-in for an LLM. It sees only the same
  tool observations a live model would. It is clearly labelled and is not evidence of LLM
  reasoning quality.
