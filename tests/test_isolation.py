"""Scenario 18: ground truth and the oracle are not reachable by agents (static + runtime checks)."""

import ast
from pathlib import Path

import pytest

import datawarden.tools  # noqa: F401 - registers tools
from datawarden.tools.base import REGISTRY, ToolContext, invoke

SRC = Path(__file__).resolve().parents[1] / "src" / "datawarden"
AGENT_SIDE = [SRC / "agents", SRC / "tools"]
FORBIDDEN_IMPORTS = ("datawarden.oracle", "datawarden.faults", "datawarden.evals")


def _imports(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_agent_and_tool_code_never_imports_oracle_faults_or_evals():
    offenders = []
    for folder in AGENT_SIDE:
        for f in folder.rglob("*.py"):
            for m in _imports(f):
                if m.startswith(FORBIDDEN_IMPORTS):
                    offenders.append(f"{f.relative_to(SRC)} imports {m}")
    # tools/recovery.py may import the *validation* package, which is trusted controller code; the
    # validation module itself imports the oracle but is never exposed to agent callers.
    assert offenders == []


def test_no_agent_tool_reads_files_or_protected_state():
    agent_names = {
        "quality_investigator",
        "lineage_investigator",
        "root_cause_investigator",
        "repair_planner",
        "verification_analyst",
        "single_agent",
    }
    reachable = {name for name, spec in REGISTRY.items() if spec.allowed_callers & agent_names}
    assert not reachable & {
        "execute_approved_repair",
        "rollback_repair",
        "verify_canonical_outputs",
        "request_approval",
        "create_shadow_environment",
        "run_shadow_pipeline",
    }
    assert not any("file" in n or "oracle" in n or "label" in n for n in reachable)


@pytest.mark.integration
def test_readonly_sql_cannot_reach_protected_data(seeded):
    ctx = ToolContext("root_cause_investigator")
    for sql in (
        "select * from recovery.pg_class",
        "select pg_read_file('/etc/passwd')",
        "select full_name from raw.raw_customer_events",
        "select * from ops.demo_settings",
        "select 1; delete from raw.raw_order_events",
        "delete from raw.raw_order_events",
    ):
        res = invoke(ctx, "run_readonly_sql", {"sql": sql})
        assert res.status == "error", sql
    ok = invoke(ctx, "run_readonly_sql", {"sql": "select count(*) as n from marts.mart_daily_revenue"})
    assert ok.status == "ok" and ok.output["rows"][0]["n"] >= 30
