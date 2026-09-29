# Runbooks

Plain operating procedures for the recovery path. Every step below is visible in the dashboard
(incident workspace → Repair review → Recovery journal, and Audit & recovery).

## 1. Approving a repair

1. Open the incident (status `awaiting approval`). Read the root-cause summary and click evidence
   ids to see the exact queries and results.
2. Repair review → check:
   - **Exact change** (diff) and **patch hash**. The approval is bound to this hash.
   - **Protected validation**: all checks must pass. The analyst cannot overrule a failed check.
   - **Shadow vs canonical**: only in-scope partitions may change.
   - Scope, preconditions, risks, rollback plan. Model confidence is uncalibrated and never used.
3. Approve (approver role only) or reject. Rejection ends that proposal and escalates the incident.
4. The worker resumes the paused graph. Watch the journal: `recheck → lock → snapshot → promote →
   apply → verify → complete`.

Approval becomes **invalidated** automatically if, before execution, the patch/scope changes, the
code base moves, new source batches arrive (stale snapshot), or validation evidence is missing. The
graph then drafts a new revision (within the repair-attempt budget) or escalates.

## 2. Verified recovery

Execution is a saga (`src/datawarden/recovery/executor.py`). Each step writes a journal entry:

| Step | What happens | Idempotency |
| --- | --- | --- |
| recheck | approval, approver role, expiry, hash, watermark, base commit, validation bound to hash | skipped once passed |
| lock | `partition_locks` rows for every mart partition in scope and every changed model | unique key → conflict |
| snapshot | `recovery.snap_<op>` copy of scoped partitions + checksum + per-date fingerprints of all partitions; artifact row with 30-day retention | reused if present |
| promote | commit approved files to the runtime workspace, tagged `[op:<id>]` | detects its own commit |
| apply | ingest pending batches, rebuild upstream models, recompute exactly the scoped partitions (`replay_dates`) | delete+insert per partition |
| verify | scoped partitions reconcile with the oracle; no partition outside scope changed; all critical/high checks pass | read-only |

A duplicate approval, duplicate job, or retried worker hits the unique `operation_key` and replays
the stored outcome; it never commits a second logical repair.

## 3. Rollback (automatic)

Triggered by any failure after the snapshot (apply error, verification failure, crash on retry).

1. Revert code with a new, history-preserving commit `rollback [op:<id>]` to `pre_commit`.
2. Rebuild upstream models from immutable raw with the restored code.
3. In one transaction: delete scoped partitions, insert them back from the snapshot.
4. Verify: restored checksum equals the snapshot checksum; every partition's fingerprint equals its
   pre-change fingerprint; the oracle sees exactly the pre-change discrepancies again.
5. Status `rolled_back`; incident `escalated` with the reason. Locks released.

Raw rows ingested during apply (e.g. a batch accepted by a new mapping) stay, because raw is
append-only and those rows are correct source data. Canonical partitions are restored exactly.

To exercise it: `DW_CHAOS=postcheck_corrupt` (demo mode only) corrupts one partition after apply.
`tests/test_scenarios.py::test_canonical_postcheck_failure_triggers_verified_rollback` does this.

## 4. Failed rollback → manual intervention

If rollback verification fails (or the recovering job exhausts its retries), the incident becomes
`manual_intervention` and the operation `manual_intervention`. The platform stops. Do this:

1. Freeze changes: pause the Airflow DAG (full profile) and stop the worker (`docker compose stop worker`).
2. Inspect the operation journal (Audit & recovery → Recovery journal) for `pre_commit`,
   `post_commit`, `snapshot_ref`, `snapshot_checksum`, and the failing verification detail.
3. Restore code: in `artifacts/runtime/workspace`, `git log` then create a revert commit to
   `pre_commit` (never rewrite history).
4. Restore data for the scoped dates as the executor role:
   ```sql
   BEGIN;
   DELETE FROM marts.mart_daily_revenue WHERE business_date = ANY('{<dates>}');
   INSERT INTO marts.mart_daily_revenue SELECT * FROM recovery.snap_<op_id>;
   SELECT md5(string_agg(t::text, '|' ORDER BY business_date))
     FROM marts.mart_daily_revenue t WHERE business_date = ANY('{<dates>}');  -- must equal snapshot_checksum
   COMMIT;
   ```
5. Rebuild upstream models: `dw pipeline --replay-dates <dates>` (then re-apply step 4 for the mart
   if needed) and run `dw checks --all`.
6. Record what you did (the audit log is append-only; add a note via the incident's cancel reason
   or your ticketing system) and release stale locks: `DELETE FROM partition_locks WHERE operation_id = '<op_id>';`
7. Restart the worker; re-run the pipeline.

## 5. Stuck jobs / worker crash

Jobs have 60 s leases renewed by a heartbeat thread. If a worker dies, another worker re-claims the
job after the lease expires; graph execution continues from the last checkpoint and tool calls
with the same operation id return stored results. A job that exhausts its retries escalates the
incident (or marks it `manual_intervention` if it was recovering).

## 6. Demo reset

`make reset` (or Demo controls → Reset) purges only rows from fault-delivered batches (through a
vetted function that refuses to run unless the warehouse is marked synthetic), removes those batch
files, restores baseline code/contracts, clears model fault profiles, drops shadow schemas, and
rebuilds canonical models. Incidents and audit history are kept.
