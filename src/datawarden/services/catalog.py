"""Sync the asset catalog, lineage (from the dbt manifest), and protected check registry."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from datawarden import workspace
from datawarden.checks.registry import CHECKS
from datawarden.config import get_settings
from datawarden.db.models import Asset, LineageEdge, QualityCheck
from datawarden.pipeline import dbt
from datawarden.warehouse.conn import connect

ASSET_INFO = {
    "raw_order_events": ("raw", "raw", "commerce-core", False, "Append-only order events (created, cancelled)"),
    "raw_payment_events": ("raw", "raw", "payments-gateway", True, "Append-only payment attempt events"),
    "raw_refund_events": ("raw", "raw", "payments-gateway", True, "Append-only refund events"),
    "raw_customer_events": (
        "raw",
        "raw",
        "crm-platform",
        False,
        "Append-only customer events (PII columns restricted)",
    ),
    "stg_orders": ("staging", "staging", "data-platform", False, "One row per order"),
    "stg_payments": ("staging", "staging", "data-platform", True, "One row per payment event"),
    "stg_refunds": ("staging", "staging", "data-platform", True, "One row per refund event"),
    "dim_customers": ("dimension", "marts", "data-platform", False, "One row per customer (no personal fields)"),
    "fct_orders": ("fact", "marts", "data-platform", False, "One row per order"),
    "fct_payments": ("fact", "marts", "finance-data", True, "One row per payment attempt"),
    "fct_refunds": ("fact", "marts", "finance-data", True, "One row per refund"),
    "mart_daily_revenue": (
        "mart",
        "marts",
        "finance-data",
        True,
        "Daily gross collected, refunds and net revenue (INR paise, Asia/Kolkata days)",
    ),
}
DOWNSTREAM_REPORTS = {"mart_daily_revenue": ["Finance daily revenue dashboard", "Month-end revenue close"]}


def parse_manifest() -> dict:
    art = get_settings().artifact_dir / "dbt" / "catalog"
    res = dbt.run_dbt("parse", target="pipeline", project_dir=workspace.dbt_project_dir(), artifacts_dir=art)
    if not res.ok:
        raise RuntimeError(f"dbt parse failed: {res.log_tail[-500:]}")
    return json.loads((art / "target" / "manifest.json").read_text())


def lineage_from_manifest(manifest: dict) -> list[tuple[str, str]]:
    edges = []
    for node in manifest["nodes"].values():
        if node["resource_type"] != "model":
            continue
        for dep in node["depends_on"]["nodes"]:
            upstream = dep.split(".")[-1]
            edges.append((upstream, node["name"]))
    return sorted(set(edges))


def manifest_path_latest() -> Path:
    return get_settings().artifact_dir / "dbt" / "catalog" / "target" / "manifest.json"


def sync_catalog(db: Session) -> dict:
    manifest = parse_manifest()
    with connect("validator") as conn:
        cols = conn.execute("""SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns
                               WHERE table_schema IN ('raw','staging','marts') ORDER BY ordinal_position""").fetchall()
    by_table: dict[str, list] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], []).append({"name": c["column_name"], "type": c["data_type"]})
    for asset_id, (kind, schema, owner, critical, desc) in ASSET_INFO.items():
        stmt = insert(Asset).values(
            id=asset_id,
            kind=kind,
            schema_name=schema,
            owner=owner,
            business_critical=critical,
            description=desc,
            columns=by_table.get(asset_id, []),
            meta={"downstream_reports": DOWNSTREAM_REPORTS.get(asset_id, [])},
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "kind": stmt.excluded.kind,
                    "schema_name": stmt.excluded.schema_name,
                    "owner": stmt.excluded.owner,
                    "business_critical": stmt.excluded.business_critical,
                    "description": stmt.excluded.description,
                    "columns": stmt.excluded.columns,
                    "meta": stmt.excluded.meta,
                },
            )
        )
    edges = lineage_from_manifest(manifest)
    db.execute(delete(LineageEdge))
    for up, down in edges:
        if up in ASSET_INFO and down in ASSET_INFO:
            db.add(LineageEdge(upstream_asset_id=up, downstream_asset_id=down, source="dbt_manifest"))
    for c in CHECKS:
        stmt = insert(QualityCheck).values(
            id=c.check_id,
            check_type=c.check_type,
            asset_id=c.asset if c.asset in ASSET_INFO else None,
            severity=c.severity,
            description=c.description,
            threshold=c.threshold,
            protected=True,
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "severity": stmt.excluded.severity,
                    "description": stmt.excluded.description,
                    "threshold": stmt.excluded.threshold,
                },
            )
        )
    return {"assets": len(ASSET_INFO), "lineage_edges": len(edges), "checks": len(CHECKS)}


def ensure_check(db: Session, check_id: str, check_type: str, severity: str, description: str) -> None:
    """Register an operational check (pipeline task / dbt test) the first time it is reported."""
    stmt = insert(QualityCheck).values(
        id=check_id,
        check_type=check_type,
        asset_id=None,
        severity=severity,
        description=description,
        threshold={},
        protected=True,
    )
    db.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
