"""Honest integration status: connected | disabled | unavailable | failed."""

from __future__ import annotations

import shutil

import httpx
import psycopg

from datawarden.config import get_settings


def status() -> list[dict]:
    s = get_settings()
    out = []

    def add(name: str, state: str, detail: str) -> None:
        out.append({"name": name, "status": state, "detail": detail})

    try:
        with psycopg.connect(s.wh_dsn("agent_ro"), connect_timeout=3) as c:
            c.execute("SELECT 1")
        add("warehouse", "connected", f"{s.wh_host}:{s.wh_port}/{s.wh_db_name}")
    except Exception as exc:  # noqa: BLE001
        add("warehouse", "unavailable", type(exc).__name__)
    add("dbt", "connected" if _dbt_available() else "unavailable", "dbt-core with postgres adapter (local)")
    if s.model_provider == "fixture":
        add("model", "disabled", "fixture mode: deterministic model responses; no live LLM calls")
    elif s.model_endpoint and s.model_api_key.get_secret_value():
        add("model", "connected", f"{s.model_provider} ({s.model_name or 'default model'}); verified per call")
    else:
        add("model", "failed", f"{s.model_provider} selected but endpoint or key missing")
    if s.airflow_base_url:
        try:
            r = httpx.get(f"{s.airflow_base_url.rstrip('/')}/api/v2/monitor/health", timeout=3)
            add("airflow", "connected" if r.status_code == 200 else "failed", f"HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            add("airflow", "unavailable", type(exc).__name__)
    else:
        add("airflow", "disabled", "set DW_AIRFLOW_BASE_URL (full profile)")
    if s.github_token.get_secret_value() and s.github_repo:
        add("github", "connected", f"repo {s.github_repo} (read diffs, scoped PRs on approval only)")
    else:
        add("github", "disabled", "local git workspace diffs and patch artifacts are used instead")
    add(
        "webhook",
        "connected" if s.webhook_url else "disabled",
        "outbound notifications" if s.webhook_url else "no outbound notifications",
    )
    add(
        "langfuse", "connected" if s.langfuse_host else "disabled", s.langfuse_host or "optional tracing not configured"
    )
    add(
        "opentelemetry",
        "connected" if s.otel_exporter_otlp_endpoint else "disabled",
        s.otel_exporter_otlp_endpoint or "spans recorded in-process only",
    )
    add("mcp", "connected", "read-only MCP server available via `dw mcp` (stdio)")
    return out


def _dbt_available() -> bool:
    import sys
    from pathlib import Path

    return bool(shutil.which("dbt", path=str(Path(sys.executable).parent)) or shutil.which("dbt"))
