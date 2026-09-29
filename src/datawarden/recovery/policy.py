"""Deterministic proposal policy. Runs before any shadow execution; model confidence is ignored.

A proposal may only change existing dbt model SQL files or (for schema drift) add an ingestion
mapping that a registered contract justifies. It may never touch tests, validators, schema.yml,
macros, project/profiles config, contracts, authentication, dependencies, or anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from datawarden import workspace
from datawarden.config import get_settings
from datawarden.contracts.agents import RepairProposalDraft
from datawarden.pipeline.ingest import load_contract
from datawarden.services.catalog import ASSET_INFO

MODEL_FILE = re.compile(r"^dbt/models/(staging|marts)/[a-z_]+\.sql$")
MAPPINGS_FILE = "ingestion/mappings.yaml"
FORBIDDEN_SQL = re.compile(
    r"\b(delete|drop|truncate|insert|update|alter|grant|revoke|create|copy|vacuum|"
    r"pg_\w+|dblink|lo_\w+|set\s+role)\b",
    re.I,
)
ROW_HIDING = re.compile(r"\bwhere\b[^;]*\bamount_paise\s*(<|<=|>|>=|between)\s*\d", re.I | re.S)
MAX_FILES = 2
MAX_FILE_BYTES = 20_000


@dataclass
class PolicyResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


def _config_block(sql: str) -> str:
    m = re.search(r"\{\{\s*config\((.*?)\)\s*\}\}", sql, re.S)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def evaluate(draft: RepairProposalDraft, *, affected_partitions: list[str], base_commit: str) -> PolicyResult:
    v: list[str] = []
    checks = [
        "kind",
        "file_allowlist",
        "sql_safety",
        "row_hiding",
        "config_unchanged",
        "mapping_justified",
        "scope_bounds",
        "base_commit_current",
    ]
    if draft.kind not in ("dbt_patch", "mapping_patch", "replay"):
        v.append(f"kind {draft.kind} cannot be executed")
    if workspace.head_commit() != base_commit:
        v.append("pipeline code changed since the proposal was drafted")
    if draft.kind == "replay" and draft.files:
        v.append("a replay may not change files")
    if draft.kind in ("dbt_patch", "mapping_patch") and not draft.files:
        v.append("patch proposals must change at least one file")
    if len(draft.files) > MAX_FILES:
        v.append(f"at most {MAX_FILES} files may change")
    for rel, content in draft.files.items():
        if len(content.encode()) > MAX_FILE_BYTES:
            v.append(f"{rel}: file too large")
        if rel == MAPPINGS_FILE:
            if draft.kind != "mapping_patch":
                v.append("ingestion mappings may only change in a mapping_patch")
            else:
                v.extend(_check_mapping(content, base_commit))
            continue
        if not MODEL_FILE.match(rel):
            v.append(f"{rel}: path is protected or outside the repair allowlist")
            continue
        if draft.kind != "dbt_patch":
            v.append(f"{rel}: model changes require kind dbt_patch")
        try:
            old = workspace.read_file(rel, commit=base_commit)
        except workspace.WorkspaceError:
            v.append(f"{rel}: new models cannot be introduced by a repair")
            continue
        if FORBIDDEN_SQL.search(_strip_comments(content)):
            v.append(f"{rel}: contains a forbidden statement or function")
        if ROW_HIDING.search(_strip_comments(content)) and not ROW_HIDING.search(_strip_comments(old)):
            v.append(f"{rel}: adds a filter on amounts that would hide rows instead of fixing them")
        if re.search(r"\blimit\s+\d", content, re.I) and not re.search(r"\blimit\s+\d", old, re.I):
            v.append(f"{rel}: adds a LIMIT that would drop rows")
        if _config_block(content) != _config_block(old):
            v.append(f"{rel}: changes the model's materialization config")
    unknown = sorted(set(draft.asset_scope) - set(ASSET_INFO))
    if unknown:
        v.append(f"unknown assets in scope: {unknown}")
    if draft.kind in ("dbt_patch", "mapping_patch", "replay"):
        if not draft.partition_scope:
            v.append("partition scope is empty")
        try:
            parsed = sorted(date.fromisoformat(d) for d in draft.partition_scope)
        except ValueError:
            v.append("partition scope contains invalid dates")
            parsed = []
        if len(parsed) > 31:
            v.append("partition scope exceeds 31 business dates")
        evidence = set(affected_partitions)
        outside = sorted(d for d in draft.partition_scope if d not in evidence)
        if evidence and outside:
            v.append(f"partition scope includes dates without evidence of impact: {outside}")
    return PolicyResult(not v, v, checks)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"\{#.*?#\}", "", sql, flags=re.S)


def _check_mapping(content: str, base_commit: str) -> list[str]:
    v = []
    try:
        new = yaml.safe_load(content) or {}
        old = yaml.safe_load(workspace.read_file(MAPPINGS_FILE, commit=base_commit)) or {}
    except yaml.YAMLError:
        return ["mappings file is not valid YAML"]
    registry = get_settings().contracts_registry_dir
    for entity, versions in old.items():
        for ver, mapping in (versions or {}).items():
            if (new.get(entity) or {}).get(ver) != mapping:
                v.append(f"existing mapping {entity}.{ver} may not be changed or removed")
    for entity, versions in new.items():
        for ver, mapping in (versions or {}).items():
            if ver in (old.get(entity) or {}):
                continue
            contract = load_contract(entity, ver, registry)
            if contract is None:
                v.append(f"mapping for {entity}.{ver} has no registered contract")
                continue
            renames = {
                (c["from"], c["to"])
                for c in contract.get("changes", [])
                if c.get("kind") == "rename" and c.get("semantics_unchanged")
            }
            declared = {(f["canonical"], name) for name, f in contract["fields"].items() if "canonical" in f}
            for src, dst in (mapping or {}).items():
                if (dst, src) not in renames and (dst, src) not in declared:
                    v.append(f"mapping {entity}.{ver} {src}->{dst} is not declared by the contract")
    return v
