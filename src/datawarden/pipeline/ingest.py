"""Append-only, contract-checked, idempotent ingestion of source batches.

A batch is ingested exactly once: its rows and its ``ops.ingested_batches`` record are written
in one transaction, and ``batch_id`` is the primary key of that record. A batch whose schema
version has no registered contract or no ingestion mapping is rejected as a whole.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import psycopg
import yaml
from psycopg import sql

from datawarden import sources, workspace
from datawarden.config import get_settings
from datawarden.warehouse.bootstrap import RAW_TABLES

ENTITY_TABLE = {
    "customers": "raw_customer_events",
    "orders": "raw_order_events",
    "payments": "raw_payment_events",
    "refunds": "raw_refund_events",
}
META_COLUMNS = ("_batch_id", "_source_checksum", "_line_no", "_run_id")


def canonical_columns(entity: str) -> list[str]:
    cols = RAW_TABLES[ENTITY_TABLE[entity]]
    return [c.strip().split()[0] for c in cols.split(",") if c.strip()]


class ContractViolation(RuntimeError):
    def __init__(self, status: str, detail: str, observation: Observation):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.observation = observation


@dataclass
class Observation:
    entity: str
    batch_id: str
    schema_version: str
    fields: list[str]
    contract_status: str = "conforms"
    detail: str = ""

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(",".join(self.fields).encode()).hexdigest()[:16]


@dataclass
class IngestResult:
    ingested: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    rows: int = 0


def load_contract(entity: str, version: str, registry: Path | None = None) -> dict | None:
    path = (registry or get_settings().contracts_registry_dir) / f"{entity}.{version}.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def load_mappings(root: Path | None = None) -> dict:
    return yaml.safe_load(workspace.mappings_path(root).read_text()) or {}


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "timestamp": lambda v: isinstance(v, str) and v.endswith("Z"),
}


def prepare_rows(
    entry: sources.BatchEntry, records: list[dict], mappings: dict, registry: Path | None = None
) -> tuple[list[dict], Observation]:
    fields_seen = sorted({k for r in records for k in r})
    obs = Observation(entry.entity, entry.batch_id, entry.schema_version, fields_seen)
    contract = load_contract(entry.entity, entry.schema_version, registry)
    if contract is None:
        raise _fail(
            obs, "unregistered_version", f"no registered contract for {entry.entity} schema {entry.schema_version}"
        )
    spec = contract["fields"]
    unknown = sorted(set(fields_seen) - set(spec))
    if unknown:
        raise _fail(obs, "violates", f"fields not in contract {entry.entity}.{entry.schema_version}: {unknown}")
    for i, rec in enumerate(records):
        for name, rule in spec.items():
            value = rec.get(name)
            if value is None:
                if rule.get("required"):
                    raise _fail(obs, "violates", f"line {i + 1}: required field {name} missing")
                continue
            if not _TYPES[rule["type"]](value):
                raise _fail(obs, "violates", f"line {i + 1}: field {name} is not {rule['type']}")
            if "enum" in rule and value not in rule["enum"]:
                raise _fail(obs, "violates", f"line {i + 1}: field {name} has invalid value")
        if rec.get("schema_version") != entry.schema_version:
            raise _fail(obs, "violates", f"line {i + 1}: record schema_version differs from batch")

    entity_map = mappings.get(entry.entity) or {}
    if entry.schema_version not in entity_map:
        raise _fail(
            obs,
            "unmapped_version",
            f"no ingestion mapping for {entry.entity} schema {entry.schema_version}; "
            f"mapped versions: {sorted(entity_map)}",
        )
    rename = entity_map[entry.schema_version] or {}
    targets = canonical_columns(entry.entity)
    rows = []
    for i, rec in enumerate(records):
        out = {rename.get(k, k): v for k, v in rec.items()}
        missing_required = [c for c in targets if c not in out and _required_canonical(entry.entity, c)]
        extra = sorted(set(out) - set(targets))
        if missing_required or extra:
            raise _fail(
                obs,
                "mapping_incomplete",
                f"mapped record does not match canonical columns: missing={missing_required} unexpected={extra}",
            )
        row = {c: out.get(c) for c in targets}
        row["event_ts"] = datetime.fromisoformat(row["event_ts"].replace("Z", "+00:00"))
        row.update({"_batch_id": entry.batch_id, "_source_checksum": entry.sha256, "_line_no": i + 1})
        rows.append(row)
    obs.contract_status = "mapped" if rename else "conforms"
    return rows, obs


def _required_canonical(entity: str, column: str) -> bool:
    cols = RAW_TABLES[ENTITY_TABLE[entity]]
    for part in cols.split(","):
        bits = part.strip().split()
        if bits and bits[0] == column:
            return "NOT NULL" in part.upper()
    return False


def _fail(obs: Observation, status: str, detail: str) -> ContractViolation:
    obs.contract_status = status
    obs.detail = detail
    return ContractViolation(status, detail, obs)


def insert_rows(cur: psycopg.Cursor, schema: str, table: str, rows: list[dict], run_id: str) -> None:
    if not rows:
        return
    cols = list(rows[0].keys()) + ["_run_id"]
    stmt = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
    )
    cur.executemany(stmt, [tuple(r[c] for c in cols[:-1]) + (run_id,) for r in rows])


def record_observation(cur: psycopg.Cursor, obs: Observation, run_id: str) -> None:
    cur.execute(
        """INSERT INTO ops.schema_observations
           (entity, batch_id, schema_version, fingerprint, fields, contract_status, detail, run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            obs.entity,
            obs.batch_id,
            obs.schema_version,
            obs.fingerprint,
            json.dumps(obs.fields),
            obs.contract_status,
            obs.detail or None,
            run_id,
        ),
    )


def pending_entries(conn: psycopg.Connection) -> list[sources.BatchEntry]:
    done = {
        r["batch_id"] if isinstance(r, dict) else r[0]
        for r in conn.execute("SELECT batch_id FROM ops.ingested_batches").fetchall()
    }
    return sorted((e for e in sources.entries() if e.batch_id not in done), key=lambda e: e.delivery_seq)


def ingest_pending(conn: psycopg.Connection, run_id: str, log) -> IngestResult:
    """Ingest every delivered batch not yet ingested, in delivery order.

    A failing batch blocks later batches of the same entity (to keep per-entity ordering) but
    not other entities. ``log(level, message)`` records run logs.
    """
    result = IngestResult()
    mappings = load_mappings()
    blocked: set[str] = set()
    for entry in pending_entries(conn):
        if entry.entity in blocked:
            result.skipped.append(entry.batch_id)
            log("warning", f"batch {entry.batch_id} held back: earlier {entry.entity} batch failed")
            continue
        try:
            records = sources.read_batch(entry)
            rows, obs = prepare_rows(entry, records, mappings)
        except (ContractViolation, sources.SourceIntegrityError) as exc:
            if isinstance(exc, ContractViolation):
                obs = exc.observation
            else:
                obs = Observation(entry.entity, entry.batch_id, entry.schema_version, [], "integrity_error", str(exc))
            with conn.transaction():
                record_observation(conn.cursor(), obs, run_id)
            result.observations.append(obs)
            result.failed[entry.batch_id] = str(exc)
            blocked.add(entry.entity)
            log("error", f"batch {entry.batch_id} rejected ({obs.contract_status}): {exc}")
            continue
        with conn.transaction():
            cur = conn.cursor()
            # Claim the batch first: the primary key makes a concurrent or repeated ingestion of
            # the same batch a no-op instead of a duplicate append.
            cur.execute(
                """INSERT INTO ops.ingested_batches
                   (batch_id, entity, schema_version, delivery_seq, delivery_date, sha256, record_count, run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (batch_id) DO NOTHING RETURNING batch_id""",
                (
                    entry.batch_id,
                    entry.entity,
                    entry.schema_version,
                    entry.delivery_seq,
                    entry.delivery_date,
                    entry.sha256,
                    entry.record_count,
                    run_id,
                ),
            )
            if cur.fetchone() is None:
                result.skipped.append(entry.batch_id)
                continue
            insert_rows(cur, "raw", ENTITY_TABLE[entry.entity], rows, run_id)
            record_observation(cur, obs, run_id)
        result.observations.append(obs)
        result.ingested.append(entry.batch_id)
        result.rows += len(rows)
    log(
        "info",
        f"ingestion: {len(result.ingested)} batches ({result.rows} rows) ingested, "
        f"{len(result.failed)} rejected, {len(result.skipped)} held back",
    )
    return result
