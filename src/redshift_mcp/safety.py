"""SQL validation and rewriting for read-only Redshift queries (sqlglot)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

if TYPE_CHECKING:
    from redshift_mcp.config import Settings

# Configurable set of dangerous function names (lowercase).
BLOCKED_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_sleep",
        "pg_read_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink_exec",
    }
)

# Expression types that must never appear anywhere in the AST (including CTEs/subqueries).
_BLOCKED_DESCENDANT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.TruncateTable,
    exp.Create,
    exp.Grant,
    exp.Revoke,
    exp.Copy,
    exp.Command,
    exp.Analyze,
    exp.Transaction,
    exp.Comment,
    exp.Lock,
    exp.Alter,
)


class UnsafeQueryError(ValueError):
    """Raised when a query fails validation."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _strip_bom(sql: str) -> str:
    if sql.startswith("\ufeff"):
        return sql[1:]
    return sql


def _parse_statements(sql: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(sql, dialect="redshift")
    except ParseError as e:
        raise UnsafeQueryError(f"SQL parse error: {e}", reason="parse_error") from e
    return [p for p in parsed if p is not None]


def _root_allowed(stmt: exp.Expression) -> bool:
    return isinstance(stmt, (exp.Select, exp.Union))


def _walk_blocked_descendants(root: exp.Expression) -> None:
    for node in root.walk():
        if isinstance(node, _BLOCKED_DESCENDANT_TYPES):
            raise UnsafeQueryError(
                f"Forbidden statement or clause ({type(node).__name__}) is not allowed.",
                reason="forbidden_ast_node",
            )


def _callable_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        sn = node.sql_name
        name = sn() if callable(sn) else str(sn)
        return str(name).lower()
    return None


def _walk_blocked_functions(root: exp.Expression) -> None:
    for node in root.walk():
        if not isinstance(node, (exp.Func, exp.Anonymous)):
            continue
        name = _callable_name(node)
        if not name:
            continue
        if name in BLOCKED_FUNCTION_NAMES or name.startswith("pg_advisory"):
            raise UnsafeQueryError(
                f"Function '{name}' is not allowed.",
                reason="blocked_function",
            )


def _schema_from_table(table: exp.Table) -> str | None:
    db = table.args.get("db")
    if db is None:
        return None
    if isinstance(db, exp.Identifier):
        return db.this.lower()
    return str(db).lower()


def _walk_schema_allow_block(root: exp.Expression, settings: Settings) -> None:
    allowed = settings.allowlist_schemas
    blocked = settings.blocklist_schemas
    for node in root.find_all(exp.Table):
        schema = _schema_from_table(node)
        if schema is None:
            continue
        if schema in blocked:
            raise UnsafeQueryError(
                f"Schema '{schema}' is blocked by BLOCKED_SCHEMAS.",
                reason="schema_blocked",
            )
        if allowed and schema not in allowed:
            raise UnsafeQueryError(
                f"Schema '{schema}' is not in ALLOWED_SCHEMAS.",
                reason="schema_not_allowed",
            )


def _literal_int(expr: exp.Expression | None) -> int | None:
    if expr is None:
        return None
    if isinstance(expr, exp.Literal) and not expr.is_string:
        try:
            return int(expr.this)
        except (TypeError, ValueError):
            return None
    return None


def _apply_limit(root: exp.Expression, cap: int) -> None:
    """Inject or clamp LIMIT on the outer Select or Union."""
    if isinstance(root, exp.Union):
        lim = root.args.get("limit")
        if lim is None:
            root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
            return
        current = _literal_int(lim.expression)
        if current is None:
            lim.set("expression", exp.Literal.number(cap))
            return
        if current > cap:
            lim.set("expression", exp.Literal.number(cap))
        return

    if isinstance(root, exp.Select):
        lim = root.args.get("limit")
        if lim is None:
            root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
            return
        current = _literal_int(lim.expression)
        if current is None:
            lim.set("expression", exp.Literal.number(cap))
            return
        if current > cap:
            lim.set("expression", exp.Literal.number(cap))
        return

    raise UnsafeQueryError(
        f"Cannot apply LIMIT to expression {type(root).__name__}.",
        reason="limit_target_unsupported",
    )


def validate_query(sql: str, settings: Settings) -> str:
    """
    Parse and validate a single-statement SELECT-only query.

    Returns rewritten SQL (Redshift dialect) with LIMIT enforced.
    """
    text = _strip_bom(sql).strip()
    if not text:
        raise UnsafeQueryError("Empty query.", reason="empty_query")

    statements = _parse_statements(text)
    if len(statements) != 1:
        raise UnsafeQueryError(
            "Only one SQL statement per request is allowed.",
            reason="multi_statement",
        )

    stmt = statements[0]
    if not _root_allowed(stmt):
        raise UnsafeQueryError(
            f"Only SELECT queries are allowed (got {type(stmt).__name__}).",
            reason="not_select_root",
        )

    _walk_blocked_descendants(stmt)
    _walk_blocked_functions(stmt)
    _walk_schema_allow_block(stmt, settings)

    cap = settings.max_rows_returned
    _apply_limit(stmt, cap)

    try:
        return stmt.sql(dialect="redshift")
    except Exception as e:
        raise UnsafeQueryError(f"Failed to serialize SQL: {e}", reason="serialize_error") from e
