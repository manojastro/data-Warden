"""Pipeline run orchestration shared by the CLI, the core-profile runner, and Airflow.

A run is: ingest pending batches -> dbt run (upstream models) -> dbt run (mart, incremental or a
bounded replay window) -> dbt test -> protected quality checks. Every task and log line is
recorded in ``ops`` so agents can later inspect runs through read-only tools.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg

from datawarden import sources, workspace
from datawarden.checks.registry import CheckResult, run_checks, store_results, summarize
from datawarden.config import get_settings
from datawarden.pipeline import dbt
from datawarden.pipeline.ingest import IngestResult, ingest_pending
from datawarden.warehouse.conn import connect

log = logging.getLogger(__name__)


_ERROR_LINE = re.compile(r"\b(ERROR|FAIL|Error)\b(?!=)")


class TransientTaskError(RuntimeError):
    pass


@dataclass
class RunReport:
    run_id: str
    status: str
    trigger: str
    code_commit: str
    source_watermark: int
    tasks: dict[str, str] = field(default_factory=dict)
    ingest: IngestResult | None = None
    dbt_failures: list[dict] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None

    def failing_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status != "pass"]

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "trigger": self.trigger,
            "code_commit": self.code_commit,
            "source_watermark": self.source_watermark,
            "tasks": self.tasks,
            "error": self.error,
            "ingested_batches": len(self.ingest.ingested) if self.ingest else 0,
            "rejected_batches": sorted(self.ingest.failed) if self.ingest else [],
            "dbt_failures": [f["unique_id"] for f in self.dbt_failures],
            "checks": summarize(self.checks),
            "failing_checks": [c.check_id for c in self.failing_checks()],
        }


def _chaos_file() -> Path:
    return get_settings().runtime_dir / "chaos" / "next_run_failure.json"


def arm_transient_failure(task: str, message: str) -> None:
    """Demo-only: make the next pipeline run fail ``task`` once with ``message``."""
    path = _chaos_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task": task, "message": message}))


def _consume_chaos(task: str) -> str | None:
    path = _chaos_file()
    if not get_settings().demo_mode or not path.exists():
        return None
    spec = json.loads(path.read_text())
    if spec.get("task") != task:
        return None
    path.unlink()
    return spec["message"]


class _Recorder:
    def __init__(self, conn: psycopg.Connection, run_id: str):
        self.conn, self.run_id = conn, run_id

    def log(self, task: str, level: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO ops.run_logs (run_id, task, level, message) VALUES (%s,%s,%s,%s)",
            (self.run_id, task, level, message[:4000]),
        )
        getattr(log, "warning" if level == "warning" else level if level in ("info", "error") else "info")(
            message, extra={"run_id": self.run_id}
        )

    def task_start(self, task: str) -> int:
        return self.conn.execute(
            "INSERT INTO ops.task_runs (run_id, task, status) VALUES (%s,%s,'running') RETURNING id",
            (self.run_id, task),
        ).fetchone()["id"]

    def task_end(self, task_id: int, status: str, detail: dict) -> None:
        self.conn.execute(
            "UPDATE ops.task_runs SET status=%s, finished_at=now(), detail=%s WHERE id=%s",
            (status, json.dumps(detail, default=str), task_id),
        )


def run_pipeline(
    *,
    trigger: str = "manual",
    full_refresh: bool = False,
    replay_window: tuple[date, date] | None = None,
    dbt_target: str = "pipeline",
    run_id: str | None = None,
    run_checks_after: bool = True,
    select_upstream: list[str] | None = None,
) -> RunReport:
    """Execute one pipeline run. ``dbt_target='executor'`` is used by approved repairs only."""
    s = get_settings()
    run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    commit = workspace.head_commit()
    watermark = sources.source_watermark()
    report = RunReport(run_id, "running", trigger, commit, watermark)
    conn = connect("pipeline", autocommit=True)
    rec = _Recorder(conn, run_id)
    params = {
        "full_refresh": full_refresh,
        "dbt_target": dbt_target,
        "replay_window": [d.isoformat() for d in replay_window] if replay_window else None,
    }
    conn.execute(
        """INSERT INTO ops.pipeline_runs (run_id, trigger, status, source_watermark, code_commit, params)
                    VALUES (%s,%s,'running',%s,%s,%s)""",
        (run_id, trigger, watermark, commit, json.dumps(params)),
    )
    art = s.artifact_dir / "dbt" / run_id
    project = workspace.dbt_project_dir()
    vars_: dict = {"code_version": commit[:12]}
    failed = False
    try:
        # 1. ingestion
        tid = rec.task_start("ingest")
        report.ingest = ingest_pending(conn, run_id, lambda lvl, msg: rec.log("ingest", lvl, msg))
        ing_ok = not report.ingest.failed
        rec.task_end(
            tid,
            "success" if ing_ok else "failed",
            {
                "ingested": report.ingest.ingested,
                "rejected": report.ingest.failed,
                "held_back": report.ingest.skipped,
                "rows": report.ingest.rows,
            },
        )
        report.tasks["ingest"] = "success" if ing_ok else "failed"
        failed |= not ing_ok

        # 2. upstream models (staging, dimensions, facts)
        tid = rec.task_start("dbt_run_upstream")
        res = dbt.run_dbt(
            "run",
            target=dbt_target,
            project_dir=project,
            artifacts_dir=art / "run_upstream",
            select=select_upstream,
            exclude=["mart_daily_revenue"],
            vars_=vars_,
            full_refresh=full_refresh,
        )
        _record_dbt(rec, tid, "dbt_run_upstream", res, report)
        if not res.ok:
            raise (
                TransientTaskError("dbt_run_upstream failed")
                if res.returncode == 124
                else RuntimeError("dbt_run_upstream failed: " + "; ".join(f["message"] for f in res.failures())[:500])
            )

        # 3. mart (incremental window or bounded replay)
        tid = rec.task_start("dbt_run_mart")
        chaos = _consume_chaos("dbt_run_mart")
        if chaos:
            rec.log("dbt_run_mart", "error", chaos)
            rec.task_end(tid, "failed", {"error": chaos})
            report.tasks["dbt_run_mart"] = "failed"
            raise TransientTaskError(chaos)
        mart_vars = dict(vars_)
        if replay_window:
            mart_vars.update(replay_start=replay_window[0].isoformat(), replay_end=replay_window[1].isoformat())
            rec.log("dbt_run_mart", "info", f"bounded replay of mart partitions {replay_window[0]}..{replay_window[1]}")
        res = dbt.run_dbt(
            "run",
            target=dbt_target,
            project_dir=project,
            artifacts_dir=art / "run_mart",
            select=["mart_daily_revenue"],
            vars_=mart_vars,
            full_refresh=full_refresh,
        )
        _record_dbt(rec, tid, "dbt_run_mart", res, report)
        if not res.ok:
            raise RuntimeError("dbt_run_mart failed: " + "; ".join(f["message"] for f in res.failures())[:500])

        # 4. dbt tests (results feed checks; failures do not abort the run)
        tid = rec.task_start("dbt_test")
        res = dbt.run_dbt("test", target=dbt_target, project_dir=project, artifacts_dir=art / "test")
        _record_dbt(rec, tid, "dbt_test", res, report, failures_fail_task=False)
    except Exception as exc:  # noqa: BLE001 - recorded, surfaced as a failed run
        failed = True
        report.error = str(exc)[:1000]
        rec.log("run", "error", f"run aborted: {report.error}")

    # 5. protected checks (read against canonical state even when the run failed)
    if run_checks_after:
        tid = rec.task_start("quality_checks")
        try:
            report.checks = run_checks(conn) + _operational_results(report)
            store_results(conn, report.checks, run_id)
            rec.task_end(tid, "success", summarize(report.checks))
            report.tasks["quality_checks"] = "success"
        except Exception as exc:  # noqa: BLE001
            rec.task_end(tid, "failed", {"error": str(exc)[:500]})
            report.tasks["quality_checks"] = "failed"
            rec.log("quality_checks", "error", f"checks failed to run: {exc}")
    report.status = "failed" if failed else "success"
    conn.execute(
        "UPDATE ops.pipeline_runs SET status=%s, finished_at=now(), error=%s WHERE run_id=%s",
        (report.status, report.error, run_id),
    )
    conn.close()
    return report


def _operational_results(report: RunReport) -> list[CheckResult]:
    """Pipeline task failures and dbt test outcomes, expressed as check results."""
    out = []
    for task, status in report.tasks.items():
        if task == "quality_checks":
            continue
        ok = status in ("success", "tests_failed")
        out.append(
            CheckResult(
                f"pipeline.{task}",
                "pipeline_failure",
                "pipeline",
                "pass" if ok else "fail",
                "high",
                f"task {task} {status}" + (f": {report.error}" if not ok and report.error else ""),
                {"run_id": report.run_id, "task_status": status},
            )
        )
    for f in report.dbt_failures:
        if f["unique_id"].startswith("test."):
            name = f["unique_id"].split(".")[2]
            out.append(
                CheckResult(
                    f"dbt.{name}",
                    "dbt_test",
                    "dbt",
                    "fail",
                    "high",
                    f"dbt test {name}: {f['message']}",
                    {"failures": f.get("failures")},
                )
            )
    return out


def _record_dbt(
    rec: _Recorder, tid: int, task: str, res: dbt.DbtResult, report: RunReport, failures_fail_task: bool = True
) -> None:
    fails = res.failures()
    report.dbt_failures.extend(fails)
    for line in res.log_tail.splitlines()[-25:]:
        if line.strip():
            rec.log(task, "error" if _ERROR_LINE.search(line) else "info", line)
    status = "success" if res.ok or (not failures_fail_task and res.returncode in (0, 1)) else "failed"
    rec.task_end(
        tid, status, {"returncode": res.returncode, "results": res.results, "artifacts": str(res.artifacts_dir)}
    )
    report.tasks[task] = status if res.ok or failures_fail_task else ("tests_failed" if fails else status)
