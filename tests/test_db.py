"""Unit tests for RedshiftClient transaction/session recovery behavior."""

from __future__ import annotations

from typing import Any

import pytest

from redshift_mcp.config import Settings
from redshift_mcp.db import RedshiftClient


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.description: list[tuple[Any, ...]] | None = [("value", 23)]
        self.rowcount = 0
        self._stage = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        _ = params
        self._stage += 1
        if (
            self._conn.fail_first_set
            and self._stage == 1
            and sql.startswith("SET statement_timeout")
        ):
            self._conn.fail_first_set = False
            msg = "ERROR: 25P02 current transaction is aborted"
            raise RuntimeError(msg)
        if self._stage >= 2:
            self.description = [("value", 23)]

    def fetchall(self) -> list[tuple[int]]:
        return [(1,)]


class _FakeConn:
    def __init__(self, *, fail_first_set: bool = False) -> None:
        self.fail_first_set = fail_first_set
        self.rollback_calls = 0
        self.close_calls = 0
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        redshift_host="h",
        redshift_database="d",
        redshift_user="u",
        redshift_password="p",
        redshift_iam=False,
        redshift_port=5439,
        allowlist_schemas=frozenset(),
        blocklist_schemas=frozenset(),
        max_rows_returned=1000,
        query_timeout_seconds=60,
        default_schema="public",
    )


def test_execute_recovers_from_aborted_transaction(settings: Settings) -> None:
    client = RedshiftClient(settings)
    conn = _FakeConn(fail_first_set=True)
    client._conn = conn  # noqa: SLF001 - intentional for controlled unit test.

    result = client.execute("SELECT 1", include_column_meta=False)

    assert conn.rollback_calls == 1
    assert result["row_count"] == 1
    assert result["rows"] == [{"value": 1}]


def test_execute_non_retryable_error_raises(settings: Settings) -> None:
    client = RedshiftClient(settings)

    class _BadConn(_FakeConn):
        def cursor(self) -> _FakeCursor:
            cur = super().cursor()

            def _fail(_: str, __: tuple[Any, ...] | None = None) -> None:
                raise RuntimeError("syntax error at or near SELECT")

            cur.execute = _fail  # type: ignore[assignment]
            return cur

    conn = _BadConn()
    client._conn = conn  # noqa: SLF001 - intentional for controlled unit test.

    with pytest.raises(RuntimeError, match="syntax error"):
        client.execute("SELECT nope")

    assert conn.rollback_calls == 1
