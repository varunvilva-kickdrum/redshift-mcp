"""Non-blocking JSON audit logs (stdout) for CloudWatch Logs / agents."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass

_audit_queue: queue.Queue[str | None] | None = None
_listener_thread: threading.Thread | None = None


@dataclass(frozen=True)
class AuditEvent:
    user_id: str
    tier: str
    tool: str
    cache_hit: bool
    stale_fallback: bool
    duration_ms: float
    ok: bool
    error: str | None = None


def _ensure_listener() -> queue.Queue[str | None]:
    global _audit_queue, _listener_thread
    if _audit_queue is not None:
        return _audit_queue

    _audit_queue = queue.Queue(maxsize=50_000)

    def _run() -> None:
        logger = logging.getLogger("redshift_mcp_audit_sink")
        while True:
            item = _audit_queue.get()
            if item is None:
                return
            try:
                logger.info(item)
            except Exception:
                # Logging must never crash the server.
                pass

    _listener_thread = threading.Thread(target=_run, name="redshift-mcp-audit", daemon=True)
    _listener_thread.start()
    return _audit_queue


def configure_audit_logging() -> None:
    """Attach a dedicated JSON logger to stdout (idempotent)."""
    log = logging.getLogger("redshift_mcp_audit_sink")
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.propagate = False
    _ensure_listener()


def emit_audit(event: AuditEvent) -> None:
    """Enqueue a single JSON audit line (drops if queue is full)."""
    configure_audit_logging()
    q = _ensure_listener()
    payload = {
        "event": "mcp_tool_call",
        "ts": time.time(),
        "user_id": event.user_id,
        "tier": event.tier,
        "tool": event.tool,
        "cache_hit": event.cache_hit,
        "stale_fallback": event.stale_fallback,
        "duration_ms": round(event.duration_ms, 3),
        "ok": event.ok,
        "error": event.error,
    }
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        q.put_nowait(line)
    except queue.Full:
        pass


def shutdown_audit(wait: bool = False) -> None:
    """Optional graceful shutdown for tests."""
    global _audit_queue, _listener_thread
    if _audit_queue is None:
        return
    try:
        _audit_queue.put_nowait(None)
    except queue.Full:
        pass
    if wait and _listener_thread is not None:
        _listener_thread.join(timeout=2.0)
    _audit_queue = None
    _listener_thread = None
