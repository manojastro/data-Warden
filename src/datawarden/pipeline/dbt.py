"""Trusted dbt runner: fixed command templates, vetted arguments, minimal environment.

Nothing here accepts free-form command text. Model selectors must name known models, vars are
validated per key, and each target receives only the one database password it needs.
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from datawarden.config import get_settings

MODELS = {
    "stg_orders",
    "stg_payments",
    "stg_refunds",
    "dim_customers",
    "fct_orders",
    "fct_payments",
    "fct_refunds",
    "mart_daily_revenue",
}
TARGETS = {
    "pipeline": ("dw_pipeline", "wh_pipeline_password"),
    "executor": ("dw_executor", "wh_executor_password"),
    "shadow": ("dw_shadow", "wh_shadow_password"),
}
_SELECT_RE = re.compile(r"^(\+?)([a-z_]+)(\+?)$")
_SHADOW_RE = re.compile(r"^shadow_[a-z0-9_]{1,50}$")
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")

PROFILES = """
datawarden:
  target: pipeline
  outputs:
    pipeline:
      type: postgres
      host: "{{ env_var('DW_WH_HOST') }}"
      port: "{{ env_var('DW_WH_PORT') | as_number }}"
      dbname: "{{ env_var('DW_WH_DB_NAME') }}"
      user: dw_pipeline
      password: "{{ env_var('DW_DBT_PASSWORD') }}"
      role: dw_transformer
      schema: marts
      threads: 4
      connect_timeout: 10
    executor:
      type: postgres
      host: "{{ env_var('DW_WH_HOST') }}"
      port: "{{ env_var('DW_WH_PORT') | as_number }}"
      dbname: "{{ env_var('DW_WH_DB_NAME') }}"
      user: dw_executor
      password: "{{ env_var('DW_DBT_PASSWORD') }}"
      role: dw_transformer
      schema: marts
      threads: 4
      connect_timeout: 10
    shadow:
      type: postgres
      host: "{{ env_var('DW_WH_HOST') }}"
      port: "{{ env_var('DW_WH_PORT') | as_number }}"
      dbname: "{{ env_var('DW_WH_DB_NAME') }}"
      user: dw_shadow
      password: "{{ env_var('DW_DBT_PASSWORD') }}"
      schema: "{{ env_var('DW_SHADOW_SCHEMA') }}"
      threads: 4
      connect_timeout: 10
"""


class DbtCommandError(ValueError):
    pass


@dataclass
class DbtResult:
    command: str
    target: str
    returncode: int
    artifacts_dir: Path
    results: list[dict] = field(default_factory=list)
    log_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def failures(self) -> list[dict]:
        return [r for r in self.results if r["status"] in ("error", "fail", "runtime error")]


def profiles_dir() -> Path:
    d = get_settings().runtime_dir / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "profiles.yml").write_text(PROFILES)
    return d


def _validate_select(select: list[str]) -> list[str]:
    out = []
    for item in select:
        m = _SELECT_RE.match(item)
        if not m or m.group(2) not in MODELS:
            raise DbtCommandError(f"selector not allowed: {item!r}")
        out.append(item)
    return out


def _validate_vars(vars_: dict) -> dict:
    allowed = {}
    for key, value in vars_.items():
        if key in ("replay_start", "replay_end"):
            allowed[key] = date.fromisoformat(str(value)).isoformat()
        elif key == "code_version":
            if not _HEX_RE.match(str(value)):
                raise DbtCommandError("code_version must be a hex hash")
            allowed[key] = str(value)
        elif key == "raw_schema":
            if value != "raw" and not _SHADOW_RE.match(str(value)):
                raise DbtCommandError("raw_schema must be raw or a shadow schema")
            allowed[key] = str(value)
        elif key == "lookback_days":
            allowed[key] = int(value)
            if not 0 <= allowed[key] <= 30:
                raise DbtCommandError("lookback_days out of range")
        else:
            raise DbtCommandError(f"var not allowed: {key}")
    if ("replay_start" in allowed) != ("replay_end" in allowed):
        raise DbtCommandError("replay window needs both replay_start and replay_end")
    if "replay_start" in allowed and allowed["replay_start"] > allowed["replay_end"]:
        raise DbtCommandError("replay_start after replay_end")
    return allowed


def _limits() -> None:  # pragma: no cover - runs in child process
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))


def run_dbt(
    command: str,
    *,
    target: str,
    project_dir: Path,
    artifacts_dir: Path,
    select: list[str] | None = None,
    exclude: list[str] | None = None,
    vars_: dict | None = None,
    full_refresh: bool = False,
    shadow_schema: str | None = None,
    timeout: int = 600,
) -> DbtResult:
    if command not in ("run", "test", "parse"):
        raise DbtCommandError(f"dbt command not allowed: {command}")
    if target not in TARGETS:
        raise DbtCommandError(f"dbt target not allowed: {target}")
    if (target == "shadow") != bool(shadow_schema):
        raise DbtCommandError("shadow target requires a shadow schema (and only the shadow target)")
    if shadow_schema and not _SHADOW_RE.match(shadow_schema):
        raise DbtCommandError("invalid shadow schema")
    s = get_settings()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dbt_bin = shutil.which("dbt", path=str(Path(sys.executable).parent)) or shutil.which("dbt")
    args = [
        dbt_bin,
        command,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles_dir()),
        "--target",
        target,
        "--target-path",
        str(artifacts_dir / "target"),
        "--log-path",
        str(artifacts_dir / "logs"),
        "--no-use-colors",
        "--log-format",
        "text",
    ]
    if select:
        args += ["--select", *_validate_select(select)]
    if exclude:
        args += ["--exclude", *_validate_select(exclude)]
    if vars_:
        args += ["--vars", json.dumps(_validate_vars(vars_))]
    if full_refresh:
        if command != "run":
            raise DbtCommandError("--full-refresh only valid for run")
        args.append("--full-refresh")
    _, pw_attr = TARGETS[target]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(s.runtime_dir),
        "DW_WH_HOST": s.wh_host,
        "DW_WH_PORT": str(s.wh_port),
        "DW_WH_DB_NAME": s.wh_db_name,
        "DW_DBT_PASSWORD": getattr(s, pw_attr).get_secret_value(),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
    }
    if shadow_schema:
        env["DW_SHADOW_SCHEMA"] = shadow_schema
    (artifacts_dir / "target" / "run_results.json").unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            args, cwd=project_dir, env=env, capture_output=True, text=True, timeout=timeout, preexec_fn=_limits
        )
        returncode, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, output = 124, f"dbt {command} timed out after {timeout}s\n{exc.stdout or ''}"
    results = []
    rr = artifacts_dir / "target" / "run_results.json"
    if rr.exists():
        data = json.loads(rr.read_text())
        for r in data.get("results", []):
            results.append(
                {
                    "unique_id": r["unique_id"],
                    "status": r["status"],
                    "message": (r.get("message") or "")[:500],
                    "failures": r.get("failures"),
                    "execution_time": r.get("execution_time"),
                }
            )
    tail = "\n".join(output.strip().splitlines()[-60:])
    return DbtResult(command, target, returncode, artifacts_dir, results, tail)
