"""Optional outbound webhook notifications (disabled unless DW_WEBHOOK_URL is set).

SSRF guard: only http(s) URLs whose host resolves to public addresses are allowed.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

import httpx

from datawarden.config import get_settings
from datawarden.events import signing


def _public_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


def deliver_webhook(payload: dict) -> dict:
    s = get_settings()
    if not s.webhook_url:
        return {"status": "disabled"}
    if not _public_host(s.webhook_url):
        return {"status": "blocked", "reason": "webhook URL must resolve to a public address"}
    body = json.dumps(payload, default=str).encode()
    headers = signing.headers(s.ingest_hmac_secret.get_secret_value(), body, f"webhook-{hash(body)}")
    resp = httpx.post(s.webhook_url, content=body, headers=headers, timeout=10, follow_redirects=False)
    return {"status": "sent" if resp.is_success else "failed", "http_status": resp.status_code}
