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
    report = run_pipeline(trigger=args.trigger, full_refresh=args.full_refresh, replay_window=window)
    _print(report.summary())
    if args.emit:
        from datawarden.events.emitter import emit_run_events

        _print(emit_run_events(report))
    return 0 if report.status == "success" else 1


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
    sp.add_argument("--emit", action="store_true", help="send failing checks to the API ingestion endpoint")
    sp.set_defaults(fn=cmd_pipeline)
    sp = sub.add_parser("checks", help="run protected quality checks")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_checks)
    sp = sub.add_parser("fault", help="demo fault injection (synthetic only)")
    sp.add_argument("action", choices=["list", "inject", "reset"])
    sp.add_argument("scenario", nargs="?")
    sp.add_argument("--no-rebuild", action="store_true")
    sp.set_defaults(fn=cmd_fault)
    sp = sub.add_parser("oracle", help="summarize independent source truth")
    sp.set_defaults(fn=cmd_oracle)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
