"""Redshift connection and query execution."""

from __future__ import annotations

import base64
import re
import threading
import time as time_module
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

import redshift_connector

from redshift_mcp.config import Settings

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_identifier(name: str, *, kind: str) -> str:
    """Validate a SQL identifier (schema/table/column name)."""
    if not name or not name.strip():
        msg = f"Invalid {kind}: empty."
        raise ValueError(msg)
    if not _IDENTIFIER_RE.fullmatch(name):
        msg = f"Invalid {kind}: only letters, digits, underscore, and $ are allowed."
        raise ValueError(msg)
    return name


def quote_ident(name: str) -> str:
    """Quote a Redshift identifier (escape embedded double quotes)."""
    return '"' + name.replace('"', '""') + '"'


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dt_time):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, memoryview):
        return base64.b64encode(value.tobytes()).decode("ascii")
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


class RedshiftClient:
    """Small helper around a single lazy Redshift connection.

    One process uses one client and one underlying TCP/TLS connection. The driver
    is not safe for concurrent queries on the same connection, so ``execute`` is
    serialized with an RLock (nested ``connection()`` / ``close()`` use the same lock).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: Any | None = None
        self._lock = threading.RLock()

    def _connect(self) -> Any:
        s = self._settings
        if s.redshift_iam:
            kwargs: dict[str, Any] = {
                "iam": True,
                "database": s.redshift_database,
                "db_user": s.redshift_user,
                "cluster_identifier": s.redshift_cluster_identifier,
                "region": s.redshift_aws_region,
            }
            if s.redshift_host:
                kwargs["host"] = s.redshift_host
            if s.redshift_port:
                kwargs["port"] = s.redshift_port
            conn = redshift_connector.connect(**kwargs)
            conn.autocommit = True
            return conn

        conn = redshift_connector.connect(
            host=s.redshift_host,
            port=s.redshift_port,
            database=s.redshift_database,
            user=s.redshift_user,
            password=s.redshift_password,
        )
        conn.autocommit = True
        return conn

    @staticmethod
    def _is_retryable_session_error(exc: Exception) -> bool:
        message = str(exc).lower()
        # 25P02 means the backend session is in failed-transaction state.
        if "25p02" in message or "current transaction is aborted" in message:
            return True
        # Recover transient session breakages by reconnecting.
        connection_markers = (
            "ssl",
            "connection reset",
            "connection refused",
            "connection closed",
            "server closed the connection",
            "broken pipe",
            "timeout",
            "timed out",
        )
        return any(marker in message for marker in connection_markers)

    @staticmethod
    def _needs_new_socket_before_retry(exc: Exception) -> bool:
        """Whether to drop the TCP/TLS session before a retry (not for 25P02 rollback recovery)."""
        message = str(exc).lower()
        if "25p02" in message or "current transaction is aborted" in message:
            return False
        connection_markers = (
            "ssl",
            "eof occurred",
            "connection reset",
            "connection refused",
            "connection closed",
            "server closed the connection",
            "broken pipe",
            "timeout",
            "timed out",
        )
        return any(marker in message for marker in connection_markers)

    def connection(self) -> Any:
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        *,
        include_column_meta: bool = True,
    ) -> dict[str, Any]:
        """
        Run SQL with statement timeout set for each execution.

        Returns rows as list[dict], row_count, optional columns metadata.
        """
        for attempt in range(2):
            with self._lock:
                conn = self.connection()
                try:
                    return self._execute_once(
                        conn,
                        sql,
                        params,
                        include_column_meta=include_column_meta,
                    )
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        # Connection may be broken; force reconnect path.
                        self.close()

                    if attempt == 0 and self._is_retryable_session_error(exc):
                        # Broken TLS / network: open a new connection on retry. Aborted
                        # txn (25P02): rollback above is enough; keep the same socket.
                        if self._needs_new_socket_before_retry(exc):
                            self.close()
                        continue
                    raise

        msg = "Unexpected execution flow in RedshiftClient.execute"
        raise RuntimeError(msg)

    def _execute_once(
        self,
        conn: Any,
        sql: str,
        params: tuple[Any, ...] | None,
        *,
        include_column_meta: bool,
    ) -> dict[str, Any]:
        # Redshift rejects bound parameters for SET (driver would send $1); use a literal ms value.
        # Safe: derived only from validated int QUERY_TIMEOUT_SECONDS in settings.
        timeout_ms_int = int(self._settings.query_timeout_seconds) * 1000
        start = time_module.perf_counter()

        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout TO {timeout_ms_int}")
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return self._build_result(cur, start, include_column_meta=include_column_meta)

    @staticmethod
    def _build_result(cur: Any, start: float, *, include_column_meta: bool) -> dict[str, Any]:
        elapsed_ms = (time_module.perf_counter() - start) * 1000
        if cur.description is None:
            return {
                "rows": [],
                "row_count": cur.rowcount if cur.rowcount is not None else 0,
                "columns": [],
                "elapsed_ms": elapsed_ms,
            }

        colnames = [d[0] for d in cur.description]
        raw_rows = cur.fetchall()
        rows = []
        for row in raw_rows:
            rows.append(
                {colnames[i]: _json_safe(row[i]) for i in range(len(colnames))},
            )

        columns: list[dict[str, Any]] = []
        if include_column_meta:
            for d in cur.description:
                columns.append(
                    {
                        "name": d[0],
                        "type_code": d[1],
                    }
                )

        return {
            "rows": rows,
            "row_count": len(rows),
            "columns": columns,
            "elapsed_ms": elapsed_ms,
        }
