"""Outbound alerts: Slack incoming webhooks and generic signed JSON webhooks.

Both senders are best-effort: they return False on any failure and never raise,
so an unreachable chat service can't break assessment or review flows. Webhook
URLs are treated as secrets and are never logged in full.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger("dra.notify")
SIGNATURE_HEADER = "X-DRA-Signature"


def _host(url) -> str:
    try:
        return urlsplit(str(url)).hostname or "?"
    except Exception:
        return "?"


def _valid_url(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(("https://", "http://"))


def sign(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of the exact request body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(url, body: bytes, headers: dict, timeout) -> bool:
    if not _valid_url(url):
        log.warning("notify skipped: invalid URL")
        return False
    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=timeout)
    except Exception as exc:  # network errors, timeouts, bad URLs
        log.warning("notify to %s failed: %s", _host(url), type(exc).__name__)
        return False
    if not 200 <= resp.status_code < 300:
        log.warning("notify to %s returned HTTP %s", _host(url), resp.status_code)
        return False
    return True


def send_slack(webhook_url, text, blocks=None, timeout=10) -> bool:
    """POST a message to a Slack incoming webhook. True on 2xx, False otherwise."""
    try:
        payload = {"text": str(text)}
        if blocks:
            payload["blocks"] = blocks
        body = json.dumps(payload).encode("utf-8")
    except Exception as exc:
        log.warning("slack payload not serialisable: %s", type(exc).__name__)
        return False
    return _post(webhook_url, body, {"Content-Type": "application/json"}, timeout)


def send_webhook(url, payload: dict, secret: str | None = None, timeout=10) -> bool:
    """POST JSON; when ``secret`` is set adds X-DRA-Signature = hex HMAC-SHA256(body)."""
    try:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                          default=str).encode("utf-8")
    except Exception as exc:
        log.warning("webhook payload not serialisable: %s", type(exc).__name__)
        return False
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[SIGNATURE_HEADER] = sign(body, secret)
    return _post(url, body, headers, timeout)


def _clean(value) -> str:
    """Untrusted text: drop control chars and chat-markup brackets (no @channel tricks)."""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ord(ch) >= 32)
    return text.replace("<", "(").replace(">", ")").strip()


def format_alert(observation, incident_name, base_url=None) -> str:
    """One-line human alert, e.g. 'DESTROYED (89%) at 37.77901, -122.42685 — file · incident: X'."""
    obs = observation or {}
    tags = []
    if obs.get("is_demo"):
        tags.append("[DEMO - SYNTHETIC]")
    status = obs.get("status") or "not_reviewed"
    if status == "not_reviewed":
        tags.append("[UNVERIFIED MODEL OUTPUT]")
    elif status == "rejected":
        tags.append("[REJECTED BY REVIEWER]")
    elif status in ("confirmed", "corrected"):
        tags.append(f"[{status.upper()}]")

    label = _clean(obs.get("damage_class") or "Unknown").upper()
    try:
        score = float(obs.get("confidence"))
        label += f" ({round(score * 100)}%)" if math.isfinite(score) else ""
    except (TypeError, ValueError):
        pass

    try:
        lat, lon = float(obs.get("latitude")), float(obs.get("longitude"))
        where = f"at {lat:.5f}, {lon:.5f}" if math.isfinite(lat) and math.isfinite(lon) \
            else "location unknown"
    except (TypeError, ValueError):
        where = "location unknown"

    parts = [" ".join(tags + [label, where])]
    if obs.get("source_file"):
        parts[0] += f" — {_clean(obs['source_file'])}"
    parts.append(f"incident: {_clean(incident_name) or 'unnamed'}")
    hazards = obs.get("hazards") or []
    if hazards:
        parts.append("hazards: " + ", ".join(_clean(h) for h in hazards))
    if base_url and obs.get("id") is not None:
        parts.append(f"{str(base_url).rstrip('/')}/?observation={quote(str(obs['id']), safe='')}")
    return " · ".join(parts)
