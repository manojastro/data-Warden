"""`dw` command-line interface for setup, seeding, pipeline runs, checks, and demo faults."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from datawarden.config import get_settings
from datawarden.logs import configure_logging


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_setup_db(args) -> int:
    from datawarden.warehouse.bootstrap import bootstrap_app_database, bootstrap_warehouse

    bootstrap_app_database()
    bootstrap_warehouse(recreate=args.recreate)
    print("databases, roles and schemas ready")
    return 0


def cmd_seed(args) -> int:
    from datawarden import sources, workspace
    from datawarden.faults import injector
    from datawarden.generator.synthetic import GENERATOR_VERSION, generate_baseline
    from datawarden.pipeline.runner import run_pipeline
    from datawarden.warehouse.bootstrap import bootstrap_warehouse

    s = get_settings()
    if not s.demo_mode:
        print("seed is only available in demo mode", file=sys.stderr)
        return 2
    bootstrap_warehouse(recreate=True)
    sources.init_store(s.seed, GENERATOR_VERSION)
    batches = generate_baseline(s.seed, date.fromisoformat(s.demo_start_date), s.demo_days)
    for b in batches:
        sources.deliver(b)
    events = sum(len(b.records) for b in batches)
    commit = workspace.init_workspace()
    injector._save_state({"active": [], "history": []})
    injector.model_profile_path().unlink(missing_ok=True)
    (s.runtime_dir / "chaos" / "next_run_failure.json").unlink(missing_ok=True)
    print(f"generated {len(batches)} batches / {events} events (seed {s.seed}); workspace {commit[:12]}")
    report = run_pipeline(trigger="seed", full_refresh=True)
    _print(report.summary())
    return 0 if report.status == "success" else 1


def cmd_pipeline(args) -> int:
    from datawarden.pipeline.runner import run_pipeline

    window = (date.fromisoformat(args.replay_start), date.fromisoformat(args.replay_end)) if args.replay_start else None
    dates = [date.fromisoformat(d) for d in args.replay_dates.split(",") if d.strip()] if args.replay_dates else None
    report = run_pipeline(
        trigger=args.trigger, full_refresh=args.full_refresh, replay_window=window, replay_dates=dates
    )
    summary = report.summary()
    if args.airflow_run_id:
        _remember_airflow_run(args.airflow_run_id, report)
    _print(summary)
    if args.emit:
        from datawarden.events.emitter import emit_run_events

        _print(emit_run_events(report))
    return 0 if report.status == "success" else 1


def _airflow_run_file(airflow_run_id: str):
    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", airflow_run_id)[:150]
    return get_settings().artifact_dir / "airflow_runs" / f"{safe}.json"


def _remember_airflow_run(airflow_run_id: str, report) -> None:
    from datawarden.events.emitter import build_events

    run = build_events(report)[0][1].run
    path = _airflow_run_file(airflow_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.model_copy(update={"results": []}).model_dump_json())


def cmd_airflow_callback(args) -> int:
    """Report an Airflow task failure through the signed ingestion endpoint (same path as checks)."""
    from datetime import UTC, datetime

    import httpx

    from datawarden.contracts.events import IncidentEventIn, RunSummary
    from datawarden.events.emitter import post_event

    path = _airflow_run_file(args.run_id)
    run = RunSummary.model_validate_json(path.read_text()) if path.exists() else None
    event = IncidentEventIn(
        source="airflow",
        event_type="pipeline_task_failed",
        occurred_at=datetime.now(UTC),
        run=run,
        check_id=f"airflow.{args.dag_id}.{args.task_id}".lower(),
        check_type="pipeline_failure",
        asset=None,
        status="fail",
        severity="high",
        message=f"Airflow task {args.dag_id}.{args.task_id} failed (try {args.try_number}, dag run {args.run_id})",
        observed={
            "dag_id": args.dag_id,
            "task_id": args.task_id,
            "airflow_run_id": args.run_id,
            "try_number": args.try_number,
            "datawarden_run_id": run.run_id if run else None,
        },
    )
    key = f"airflow:{args.dag_id}:{args.run_id}:{args.task_id}:{args.try_number}"[:200]
    with httpx.Client(timeout=20) as client:
        _print(post_event(client, key, event))
    return 0


def cmd_airflow(args) -> int:
    from datawarden.integrations import airflow

    if args.action == "runs":
        _print(airflow.list_runs())
    elif args.action == "trigger":
        _print(airflow.trigger([d for d in (args.replay_dates or "").split(",") if d], note="triggered via dw cli"))
    elif args.action == "log":
        print(airflow.task_log(args.run_id))
    return 0


def cmd_checks(args) -> int:
    from datawarden.checks.registry import run_checks, summarize
    from datawarden.warehouse.conn import connect

    with connect("validator") as conn:
        results = run_checks(conn)
    for r in results:
        if args.all or r.status != "pass":
            print(f"{r.status.upper():5} {r.check_id:42} {r.message}")
    _print(summarize(results))
    return 0 if all(r.status == "pass" for r in results) else 1


def cmd_fault(args) -> int:
    from datawarden.faults import injector

    if args.action == "list":
        for k, v in injector.SCENARIOS.items():
            print(f"{k:28} {v}")
        return 0
    if args.action == "reset":
        _print(injector.reset(rebuild=not args.no_rebuild))
        return 0
    if args.action == "inject":
        _print(injector.inject(args.scenario))
        return 0
    return 2


def cmd_app_seed(args) -> int:
    from datawarden.db.session import session_scope
    from datawarden.services.auth import ensure_demo_users
    from datawarden.services.catalog import sync_catalog

    with session_scope() as db:
        cat = sync_catalog(db)
        users = ensure_demo_users(db)
    print(
        f"catalog: {cat}; demo users created: {users['created'] or 'none (already exist)'}; "
        f"credentials file: {users['credentials_file']}"
    )
    return 0


def cmd_worker(args) -> int:
    from datawarden.worker.main import drain, main

    if args.drain:
        for job in drain(wait_for_delayed=args.wait):
            print(f"{job['id']} {job['kind']} {job['status']}")
        return 0
    main()
    return 0


def cmd_eval(args) -> int:
    from datawarden.evals.runner import run_evaluation

    seeds = [int(x) for x in args.seeds.split(",")] if args.seeds else None
    scenarios = args.scenarios.split(",") if args.scenarios else None
    modes = args.modes.split(",") if args.modes else None
    _print(run_evaluation(scenarios=scenarios, seeds=seeds, modes=modes))
    return 0


def cmd_demo(args) -> int:
    """Scripted demo: wrong-fix rejection, then a successful verified recovery."""
    from datawarden.evals.scenario import run_scenario

    for scenario in (["faulty_proposal"] if not args.skip_bad else []) + [args.scenario]:
        print(f"\n=== {scenario} ===")
        out = run_scenario(scenario, approve=not args.no_approve)
        for p in out.proposals:
            print(
                f"  proposal {p['id']} ({p['kind']}): {p['status']}"
                + (f"; policy: {p['policy_violations']}" if p["policy_violations"] else "")
                + (f"; failed checks: {p['failed_validations']}" if p["failed_validations"] else "")
            )
        print(
            f"  incident {out.incident_ids[0] if out.incident_ids else '-'}: {out.status} "
            f"(root cause {out.root_cause}); recovery {out.recovery}; failing checks after: "
            f"{out.failing_checks_after or 'none'}"
        )
        print(f"  reason: {out.terminal_reason}")
    print("\nOpen the dashboard (http://localhost:8000 or :5173) to review evidence, diffs, and the recovery journal.")
    return 0


def cmd_mcp(args) -> int:
    from datawarden.mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_oracle(args) -> int:
    from datawarden.oracle.reconciliation import source_truth

    t = source_truth()
    total = sum(d.net_revenue_paise for d in t.daily.values())
    print(
        f"{len(t.daily)} business dates, net revenue {total / 100:,.2f} INR, {t.distinct_payment_ids} distinct payments"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING")
    p = argparse.ArgumentParser(prog="dw", description="DataWarden command line")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setup-db", help="create databases, roles, schemas (admin credential)")
    sp.add_argument("--recreate", action="store_true")
    sp.set_defaults(fn=cmd_setup_db)
    sp = sub.add_parser("seed", help="regenerate synthetic sources and rebuild the warehouse")
    sp.set_defaults(fn=cmd_seed)
    sp = sub.add_parser("pipeline", help="run the pipeline (ingest, dbt, tests, checks)")
    sp.add_argument("--trigger", default="manual")
    sp.add_argument("--full-refresh", action="store_true")
    sp.add_argument("--replay-start")
    sp.add_argument("--replay-end")
    sp.add_argument("--replay-dates", help="comma-separated business dates to recompute exactly")
    sp.add_argument("--emit", action="store_true", help="send failing checks to the API ingestion endpoint")
    sp.add_argument("--airflow-run-id", help="(Airflow) remember this run for the failure callback")
    sp.set_defaults(fn=cmd_pipeline)
    sp = sub.add_parser("airflow-callback", help="(Airflow) report a task failure to DataWarden")
    for a in ("--dag-id", "--task-id", "--run-id"):
        sp.add_argument(a, required=True)
    sp.add_argument("--try-number", type=int, default=1)
    sp.set_defaults(fn=cmd_airflow_callback)
    sp = sub.add_parser("airflow", help="Airflow adapter: list runs, fetch a log, trigger a scoped replay")
    sp.add_argument("action", choices=["runs", "log", "trigger"])
    sp.add_argument("--run-id")
    sp.add_argument("--replay-dates")
    sp.set_defaults(fn=cmd_airflow)
    sp = sub.add_parser("checks", help="run protected quality checks")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_checks)
    sp = sub.add_parser("fault", help="demo fault injection (synthetic only)")
    sp.add_argument("action", choices=["list", "inject", "reset"])
    sp.add_argument("scenario", nargs="?")
    sp.add_argument("--no-rebuild", action="store_true")
    sp.set_defaults(fn=cmd_fault)
    sp = sub.add_parser("app-seed", help="sync catalog/lineage/checks and create local demo users")
    sp.set_defaults(fn=cmd_app_seed)
    sp = sub.add_parser("worker", help="run the job worker")
    sp.add_argument("--drain", action="store_true", help="process ready jobs then exit")
    sp.add_argument("--wait", type=float, default=0, help="with --drain: keep polling this many seconds")
    sp.set_defaults(fn=cmd_worker)
    sp = sub.add_parser("eval", help="benchmark fixed seeds x scenarios x modes; writes docs/EVALUATION.md")
    sp.add_argument("--seeds", help="comma-separated seeds (default 42,1337)")
    sp.add_argument("--scenarios", help="comma-separated scenarios (default all)")
    sp.add_argument("--modes", help="detection_only,single_agent,multi_agent")
    sp.set_defaults(fn=cmd_eval)
    sp = sub.add_parser("demo", help="scripted demo: faulty proposal rejected, then a verified recovery")
    sp.add_argument("--scenario", default="duplicate_payments")
    sp.add_argument("--skip-bad", action="store_true")
    sp.add_argument("--no-approve", action="store_true", help="stop at the approval (approve in the dashboard)")
    sp.set_defaults(fn=cmd_demo)
    sp = sub.add_parser("mcp", help="run the read-only MCP server (stdio)")
    sp.set_defaults(fn=cmd_mcp)
    sp = sub.add_parser("oracle", help="summarize independent source truth")
    sp.set_defaults(fn=cmd_oracle)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
