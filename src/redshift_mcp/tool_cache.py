"""TTL cache for MCP tool results with optional stale fallback."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

# TTLs tuned for catalog vs data-heavy tools (PDF: match data freshness).
TOOL_CACHE_TTL_SEC: dict[str, int] = {
    "list_schemas": 300,
    "list_tables": 120,
    "describe_table": 300,
    "sample_table": 60,
    "run_select_query": 60,
    "profile_column": 300,
    "get_table_size": 120,
}


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


def cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = f"{tool_name}\n{_canonical_arguments(arguments)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    value: Any
    stored_at: float
    ttl_sec: int


class ToolResultCache:
    """In-memory TTL cache with stale retention for upstream failures."""

    def __init__(self, *, max_entries: int) -> None:
        self._lock = threading.Lock()
        self._fresh: dict[str, CacheEntry] = {}
        self._stale: dict[str, CacheEntry] = {}
        self._max_entries = max(16, max_entries)

    def _gc_fresh(self) -> None:
        if len(self._fresh) <= self._max_entries:
            return
        # Drop oldest ~half by stored_at
        items = sorted(self._fresh.items(), key=lambda kv: kv[1].stored_at)
        for k, _ in items[: max(1, len(items) // 2)]:
            self._fresh.pop(k, None)

    def get_fresh(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            entry = self._fresh.get(key)
            if entry is None:
                return None
            if now - entry.stored_at > entry.ttl_sec:
                return None
            return entry.value

    def get_stale(self, key: str) -> Any | None:
        with self._lock:
            entry = self._stale.get(key)
            return None if entry is None else entry.value

    def set_success(self, key: str, value: Any, *, ttl_sec: int) -> None:
        entry = CacheEntry(value=value, stored_at=time.time(), ttl_sec=ttl_sec)
        with self._lock:
            self._fresh[key] = entry
            self._stale[key] = entry
            self._gc_fresh()
            if len(self._stale) > self._max_entries * 4:
                items = sorted(self._stale.items(), key=lambda kv: kv[1].stored_at)
                for k, _ in items[: len(items) // 2]:
                    self._stale.pop(k, None)
