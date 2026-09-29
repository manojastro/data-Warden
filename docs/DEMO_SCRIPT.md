# 5-minute demo script

Shows one **wrong fix being rejected** and one **successful verified recovery**. Everything runs on
synthetic data in fixture model mode (say so out loud: the "model" is a deterministic stand-in;
the pipeline, SQL, dbt, shadow validation, approval and execution are real).

Prep (before the audience): `make up && make seed` (or host mode, see README). Keep two browser
windows: one signed in as **operator**, one as **approver** (passwords in
`artifacts/demo_credentials.json`).

## 0:00 – Frame it (30 s)

Overview page: "A retail revenue pipeline — 57k synthetic events, dbt, a daily revenue mart.
DataWarden watches 21 protected checks. The rule: AI proposes, deterministic systems validate,
authorized humans approve."

## 0:30 – A wrong fix, rejected (90 s)

1. Operator → Demo controls → **Inject `faulty_proposal`**. (Duplicate payment events plus a
   deliberately faulty Repair Planner.)
2. Open the incident from the jobs list. Point at the live timeline and the execution graph:
   Quality and Lineage investigators run in parallel, Root Cause loops for a second round to prove
   the duplicates are re-deliveries, and finds legitimate split-tender payments.
3. Repair review:
   - Revision 1, confidence **0.97**: deletes the uniqueness test and filters large payments →
     **rejected by policy** (protected path, row hiding) before anything runs.
   - Revision 2, confidence **0.99**: dedups on order/amount → runs in a shadow schema →
     **protected validation fails** (`legit_distinct_payments_preserved`, revenue reconciliation).
4. "High confidence changed nothing. The incident is escalated with the evidence."

## 2:00 – A real recovery (2 min)

1. Demo controls → **Reset synthetic data**, then **Inject `duplicate_payments`**.
2. Incident workspace: show hypotheses with supporting/contradicting evidence; click an evidence id
   to show the exact SQL, results and hash.
3. Repair review: the diff (`distinct on (event_id)` in `stg_payments`), the hash, 11/11 protected
   checks, shadow-vs-canonical table (only the three affected dates change).
4. Operator window: "I can see it but I cannot approve." Approver window: **Approve repair**.
5. Watch the recovery journal fill in: recheck → lock → snapshot → promote → apply → verify →
   complete. Incident **resolved**.

## 4:00 – Proof (45 s)

- Assets → `mart_daily_revenue`: `reconciliation.mart_daily_revenue` is **pass** again.
- Audit & recovery: approval by `approver`, the executor steps, and (from the first incident) the
  policy rejection — append-only.
- Optional: `DW_CHAOS=postcheck_corrupt` run shows an automatic, verified rollback.

## 4:45 – Close (15 s)

"Two agents in parallel, a bounded root-cause loop, a planner that cannot write, validation it
cannot edit, and an executor that only acts on a hash a human approved."

CLI alternative: `make demo` prints the same two outcomes in the terminal.
