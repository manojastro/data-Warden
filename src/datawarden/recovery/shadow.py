"""Incident-scoped shadow environments.

Each proposal revision gets its own schema ``shadow_<incident>_r<rev>`` owned by ``dw_shadow``
(created through a vetted SECURITY DEFINER function) and its own detached copy of the pipeline
code with the patch applied. ``dw_shadow`` can read raw but has no privileges on canonical
``staging``/``marts``. Pending (not yet ingested) source batches are ingested into shadow-local
tables with the patched mappings, and shadow source views union them with raw, so the shadow run
mirrors exactly what the approved canonical execution will do.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from psycopg import sql

from datawarden import sources, workspace
from datawarden.config import get_settings
from datawarden.pipeline import dbt
from datawarden.pipeline.ingest import ENTITY_TABLE, ContractViolation, insert_rows, load_mappings, prepare_rows
from datawarden.warehouse.conn import connect


@dataclass
class ShadowEnv:
    schema: str
    code_dir: Path
    artifacts_dir: Path
    pending_ingested: list[str]
    pending_rejected: dict[str, str]


def schema_name(incident_id: str, revision: int) -> str:
    return f"shadow_{re.sub(r'[^a-z0-9]', '', incident_id.lower())[-10:]}_r{revision}"


def create(incident_id: str, revision: int, files: dict[str, str], base_commit: str) -> ShadowEnv:
    s = get_settings()
    schema = schema_name(incident_id, revision)
    code_dir = s.artifact_dir / "shadow" / schema / "code"
    art = s.artifact_dir / "shadow" / schema / "dbt"
    if art.exists():
        shutil.rmtree(art)
    workspace.clone_to(code_dir, base_commit)
    for rel, content in files.items():
        path = (code_dir / rel).resolve()
        if code_dir.resolve() not in path.parents:
            raise ValueError("patch path escapes shadow workspace")
        path.write_text(content)
    with connect("shadow", autocommit=True) as conn:
        conn.execute("SELECT ops.drop_shadow_schema(%s)", (schema,))
        conn.execute("SELECT ops.create_shadow_schema(%s)", (schema,))
        ident = sql.Identifier(schema)
        for table in ENTITY_TABLE.values():
            conn.execute(
                sql.SQL("CREATE TABLE {}.{} AS SELECT * FROM raw.{} WITH NO DATA").format(
                    ident, sql.Identifier(f"pending_{table}"), sql.Identifier(table)
                )
            )
            conn.execute(
                sql.SQL("CREATE VIEW {s}.{t} AS SELECT * FROM raw.{t} UNION ALL SELECT * FROM {s}.{p}").format(
                    s=ident, t=sql.Identifier(table), p=sql.Identifier(f"pending_{table}")
                )
            )
    ingested, rejected = _ingest_pending(schema, code_dir)
    return ShadowEnv(schema, code_dir, art, ingested, rejected)


def _ingest_pending(schema: str, code_dir: Path) -> tuple[list[str], dict[str, str]]:
    mappings = load_mappings(code_dir)
    ingested, rejected = [], {}
    with connect("shadow") as conn:
        done = {r["batch_id"] for r in conn.execute("SELECT batch_id FROM ops.ingested_batches").fetchall()}
    with connect("shadow") as conn:
        offset = 10**12
        for entry in sorted(sources.entries(), key=lambda e: e.delivery_seq):
            if entry.batch_id in done:
                continue
            try:
                rows, _ = prepare_rows(entry, sources.read_batch(entry), mappings)
            except (ContractViolation, sources.SourceIntegrityError) as exc:
                rejected[entry.batch_id] = str(exc)
                continue
            for i, r in enumerate(rows):
                r["_load_id"] = offset + entry.delivery_seq * 100_000 + i
            insert_rows(conn.cursor(), schema, f"pending_{ENTITY_TABLE[entry.entity]}", rows, "shadow")
            ingested.append(entry.batch_id)
        conn.commit()
    return ingested, rejected


def run(env: ShadowEnv, code_version: str) -> dbt.DbtResult:
    """Full build of every model inside the shadow schema, reading shadow source views."""
    return dbt.run_dbt(
        "run",
        target="shadow",
        project_dir=env.code_dir / "dbt",
        artifacts_dir=env.artifacts_dir / "run",
        vars_={"raw_schema": env.schema, "code_version": code_version},
        full_refresh=True,
        shadow_schema=env.schema,
    )


def run_tests(env: ShadowEnv) -> dbt.DbtResult:
    return dbt.run_dbt(
        "test",
        target="shadow",
        project_dir=env.code_dir / "dbt",
        artifacts_dir=env.artifacts_dir / "test",
        vars_={"raw_schema": env.schema},
        shadow_schema=env.schema,
    )


def drop(schema: str) -> None:
    with connect("shadow", autocommit=True) as conn:
        conn.execute("SELECT ops.drop_shadow_schema(%s)", (schema,))
