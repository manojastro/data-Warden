"""Immutable source fixture store.

Source batches are JSONL files plus a manifest. Batches are append-only: an existing batch file
is never rewritten. Each manifest entry carries a SHA-256 checksum that ingestion and the
reconciliation oracle verify before trusting the contents.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from datawarden.config import get_settings
from datawarden.generator.synthetic import Batch


class SourceIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchEntry:
    batch_id: str
    entity: str
    schema_version: str
    delivery_date: str
    delivery_seq: int
    file: str
    sha256: str
    record_count: int


def sources_dir() -> Path:
    return get_settings().sources_dir


def manifest_path() -> Path:
    return sources_dir() / "manifest.json"


def _serialize(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records).encode()


def load_manifest() -> dict:
    path = manifest_path()
    if not path.exists():
        return {"batches": []}
    return json.loads(path.read_text())


def entries() -> list[BatchEntry]:
    return [BatchEntry(**b) for b in load_manifest()["batches"]]


def _write_manifest(manifest: dict) -> None:
    tmp = manifest_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    tmp.replace(manifest_path())


def init_store(seed: int, generator_version: str) -> None:
    """Start an empty synthetic source store (full demo re-seed only)."""
    d = sources_dir()
    d.mkdir(parents=True, exist_ok=True)
    if (d / "batches").exists():
        shutil.rmtree(d / "batches")
    (d / "batches").mkdir()
    _write_manifest({"seed": seed, "generator_version": generator_version, "batches": []})


def deliver(batch: Batch, batch_id: str | None = None) -> BatchEntry:
    """Append a batch to the store. Refuses to overwrite an existing batch id."""
    manifest = load_manifest()
    bid = batch_id or batch.batch_id
    if any(b["batch_id"] == bid for b in manifest["batches"]):
        raise SourceIntegrityError(f"batch {bid} already delivered; batches are immutable")
    payload = _serialize(batch.records)
    rel = f"batches/{batch.entity}/{bid}.jsonl"
    path = sources_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SourceIntegrityError(f"batch file {rel} already exists")
    path.write_bytes(payload)
    seq = max((b["delivery_seq"] for b in manifest["batches"]), default=0) + 1
    entry = BatchEntry(
        batch_id=bid,
        entity=batch.entity,
        schema_version=batch.schema_version,
        delivery_date=batch.delivery_date.isoformat(),
        delivery_seq=seq,
        file=rel,
        sha256=hashlib.sha256(payload).hexdigest(),
        record_count=len(batch.records),
    )
    manifest["batches"].append(asdict(entry))
    _write_manifest(manifest)
    return entry


def read_batch(entry: BatchEntry) -> list[dict]:
    payload = (sources_dir() / entry.file).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != entry.sha256:
        raise SourceIntegrityError(f"checksum mismatch for {entry.batch_id}")
    return [json.loads(line) for line in payload.decode().splitlines() if line]


def remove_batches(batch_ids: set[str]) -> None:
    """Remove fault-injected batches during a scoped demo reset (never baseline batches)."""
    manifest = load_manifest()
    keep = []
    for b in manifest["batches"]:
        if b["batch_id"] in batch_ids:
            (sources_dir() / b["file"]).unlink(missing_ok=True)
        else:
            keep.append(b)
    manifest["batches"] = keep
    _write_manifest(manifest)


def source_watermark() -> int:
    return max((b.delivery_seq for b in entries()), default=0)
