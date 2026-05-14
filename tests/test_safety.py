"""Unit tests for SQL safety validation (sqlglot)."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from redshift_mcp.config import Settings
from redshift_mcp.safety import UnsafeQueryError, validate_query


@pytest.fixture
def safety_settings() -> Settings:
    """Minimal settings for validation tests (no Redshift connection)."""
    return Settings.model_construct(
        redshift_host="h",
        redshift_database="d",
        redshift_user="u",
        redshift_password="p",
        redshift_iam=False,
        redshift_port=5439,
        allowlist_schemas=frozenset(),
        blocklist_schemas=frozenset({"pii"}),
        max_rows_returned=10000,
        query_timeout_seconds=60,
        default_schema="public",
    )


def _outer_limit(sql: str) -> int | None:
    root = sqlglot.parse_one(sql, dialect="redshift")
    if isinstance(root, exp.Select):
        return _limit_int(root.args.get("limit"))
    if isinstance(root, exp.Union):
        return _limit_int(root.args.get("limit"))
    return None


def _limit_int(lim: exp.Expression | None) -> int | None:
    if lim is None:
        return None
    expr = lim.expression
    if isinstance(expr, exp.Literal) and not expr.is_string:
        return int(expr.this)
    return None


def test_plain_select_passes(safety_settings: Settings) -> None:
    sql = "SELECT 1 AS x"
    out = validate_query(sql, safety_settings)
    assert "SELECT" in out.upper()


def test_multi_statement_rejected(safety_settings: Settings) -> None:
    sql = "SELECT 1; DELETE FROM users"
    with pytest.raises(UnsafeQueryError) as ei:
        validate_query(sql, safety_settings)
    assert ei.value.reason == "multi_statement"


def test_cte_delete_rejected(safety_settings: Settings) -> None:
    sql = "WITH x AS (DELETE FROM t) SELECT 1"
    with pytest.raises(UnsafeQueryError) as ei:
        validate_query(sql, safety_settings)
    assert ei.value.reason == "forbidden_ast_node"


def test_pg_terminate_backend_rejected(safety_settings: Settings) -> None:
    sql = "SELECT pg_terminate_backend(1)"
    with pytest.raises(UnsafeQueryError) as ei:
        validate_query(sql, safety_settings)
    assert ei.value.reason == "blocked_function"


def test_pg_advisory_prefix_rejected(safety_settings: Settings) -> None:
    sql = "SELECT pg_advisory_lock(1)"
    with pytest.raises(UnsafeQueryError) as ei:
        validate_query(sql, safety_settings)
    assert ei.value.reason == "blocked_function"


def test_limit_injected_when_missing(safety_settings: Settings) -> None:
    sql = "SELECT * FROM huge_table"
    out = validate_query(sql, safety_settings)
    assert _outer_limit(out) == 10000


def test_limit_clamped_when_too_high(safety_settings: Settings) -> None:
    sql = "SELECT * FROM huge_table LIMIT 9999999"
    out = validate_query(sql, safety_settings)
    assert _outer_limit(out) == 10000


def test_blocked_schema_rejected(safety_settings: Settings) -> None:
    sql = 'SELECT * FROM "pii"."customers"'
    with pytest.raises(UnsafeQueryError) as ei:
        validate_query(sql, safety_settings)
    assert ei.value.reason == "schema_blocked"


def test_keyword_case_insensitivity(safety_settings: Settings) -> None:
    sql = "sElEcT 1 FrOm dual"
    out = validate_query(sql, safety_settings)
    assert _outer_limit(out) == 10000


def test_comments_and_whitespace(safety_settings: Settings) -> None:
    sql = "/* leading */   \n\tSELECT 1\n-- trailing\n"
    out = validate_query(sql, safety_settings)
    assert _outer_limit(out) == 10000


def test_union_select_limit_applied(safety_settings: Settings) -> None:
    sql = "SELECT 1 UNION ALL SELECT 2"
    out = validate_query(sql, safety_settings)
    assert _outer_limit(out) == 10000
