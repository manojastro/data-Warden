#!/usr/bin/env python3
"""Create a local `.env` from `.env.example`, generating random secrets for empty secret fields.

Existing values in `.env` are kept. Secrets are never printed.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {
    "DW_PG_ADMIN_PASSWORD",
    "DW_APP_DB_PASSWORD",
    "DW_WH_PIPELINE_PASSWORD",
    "DW_WH_AGENT_RO_PASSWORD",
    "DW_WH_SHADOW_PASSWORD",
    "DW_WH_EXECUTOR_PASSWORD",
    "DW_WH_VALIDATOR_PASSWORD",
    "DW_INGEST_HMAC_SECRET",
    "DW_SESSION_SECRET",
}


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def main() -> int:
    example = (ROOT / ".env.example").read_text().splitlines()
    existing = parse(ROOT / ".env")
    lines, generated = [], []
    for line in example:
        if "=" in line and not line.lstrip().startswith("#"):
            key, default = (p.strip() for p in line.split("=", 1))
            value = existing.get(key, default)
            if key in GENERATED and not value:
                value = secrets.token_urlsafe(24)
                generated.append(key)
            lines.append(f"{key}={value}")
        else:
            lines.append(line)
    path = ROOT / ".env"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    print(f"wrote {path} ({len(generated)} secrets generated: {', '.join(generated) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
