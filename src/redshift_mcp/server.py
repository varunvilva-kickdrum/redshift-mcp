"""Read-only Redshift MCP server (stdio + optional streamable HTTP for remote clients)."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

from redshift_mcp.audit import AuditEvent, configure_audit_logging, emit_audit
from redshift_mcp.auth0_jwt import Auth0JWTVerifier
from redshift_mcp.config import Settings, _find_project_root, get_settings
from redshift_mcp.db import RedshiftClient, quote_ident, validate_identifier
from redshift_mcp.http_middleware import MCP_RATE_LIMIT_JSONRPC, RateLimitHttp429Middleware
from redshift_mcp.ratelimit import hourly_limiter_for
from redshift_mcp.safety import UnsafeQueryError, validate_query
from redshift_mcp.tiers import TierName, normalize_tier, tool_allowed
from redshift_mcp.tool_cache import TOOL_CACHE_TTL_SEC, ToolResultCache, cache_key

LOG_PATH = Path.cwd() / "redshift_mcp.log"

TIER_FORBIDDEN = -32030

_tool_cache: ToolResultCache | None = None
_tool_cache_lock = threading.Lock()


def _tool_cache_instance() -> ToolResultCache:
    global _tool_cache
    with _tool_cache_lock:
        if _tool_cache is None:
            _tool_cache = ToolResultCache(max_entries=get_settings().mcp_cache_max_entries)
        return _tool_cache


def _transport_security_from_env() -> TransportSecuritySettings:
    if os.getenv("MCP_RELAX_TRANSPORT_SECURITY", "").lower() in ("1", "true", "yes"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts_raw = (os.getenv("MCP_ALLOWED_HOSTS") or "").strip()
    origins_raw = (os.getenv("MCP_ALLOWED_ORIGINS") or "").strip()
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
    if not hosts:
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if not origins:
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _auth_bundle() -> tuple[AuthSettings | None, Auth0JWTVerifier | None]:
    domain = (os.getenv("AUTH0_DOMAIN") or "").strip()
    audience = (os.getenv("AUTH0_AUDIENCE") or "").strip()
    public_url = (os.getenv("MCP_PUBLIC_URL") or "").strip()
    tier_claim = (os.getenv("AUTH0_TIER_CLAIM") or "https://redshift-mcp/tier").strip()
    if not (domain and audience and public_url):
        return None, None

    domain = domain.removeprefix("https://").removesuffix("/")
    verifier = Auth0JWTVerifier(domain=domain, audience=audience, tier_claim=tier_claim)
    auth = AuthSettings(
        issuer_url=f"https://{domain}/",
        resource_server_url=public_url,
        required_scopes=[],
    )
    return auth, verifier


def _tier_from_access_token(token: AccessToken | None, local_tier: str) -> TierName:
    if token is None:
        return normalize_tier(local_tier)
    for s in token.scopes:
        if isinstance(s, str) and s.startswith("tier:"):
            return normalize_tier(s.split(":", 1)[1])
    return "free"


def _user_id(token: AccessToken | None) -> str:
    if token is None:
        return "anonymous"
    return token.client_id or "anonymous"


class RedshiftProductionMCP(FastMCP):
    """FastMCP with tiered tools, caching, rate limits, and HTTP 429 mapping."""

    def streamable_http_app(self):
        inner = super().streamable_http_app()
        return RateLimitHttp429Middleware(inner)

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        settings = get_settings()
        token = get_access_token()
        tier = _tier_from_access_token(token, settings.mcp_local_dev_tier)
        return [t for t in tools if tool_allowed(tier, t.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        settings = get_settings()
        token = get_access_token()
        tier = _tier_from_access_token(token, settings.mcp_local_dev_tier)
        user_id = _user_id(token)

        if not tool_allowed(tier, name):
            emit_audit(
                AuditEvent(
                    user_id=user_id,
                    tier=tier,
                    tool=name,
                    cache_hit=False,
                    stale_fallback=False,
                    duration_ms=0.0,
                    ok=False,
                    error="insufficient_tier",
                ),
            )
            raise McpError(
                ErrorData(
                    code=TIER_FORBIDDEN,
                    message="This tool is not enabled for your subscription tier.",
                    data={"tier": tier, "tool": name},
                ),
            )

        ttl = TOOL_CACHE_TTL_SEC.get(name)
        cache = _tool_cache_instance()
        key = cache_key(name, arguments)
        if ttl is not None:
            cached = cache.get_fresh(key)
            if cached is not None:
                emit_audit(
                    AuditEvent(
                        user_id=user_id,
                        tier=tier,
                        tool=name,
                        cache_hit=True,
                        stale_fallback=False,
                        duration_ms=0.0,
                        ok=True,
                        error=None,
                    ),
                )
                return cached

        if token is not None:
            limiter = hourly_limiter_for(settings)
            decision = limiter.check(user_id=user_id, tier=tier)
            if not decision.allowed:
                emit_audit(
                    AuditEvent(
                        user_id=user_id,
                        tier=tier,
                        tool=name,
                        cache_hit=False,
                        stale_fallback=False,
                        duration_ms=0.0,
                        ok=False,
                        error="rate_limited",
                    ),
                )
                raise McpError(
                    ErrorData(
                        code=MCP_RATE_LIMIT_JSONRPC,
                        message="Rate limit exceeded for your tier.",
                        data={"retry_after_sec": decision.retry_after_sec},
                    ),
                )

        start = time.perf_counter()
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as exc:
            _get_logger().warning("tool_failed name=%s error=%s", name, str(exc))
            if ttl is not None:
                stale = cache.get_stale(key)
                if stale is not None:
                    emit_audit(
                        AuditEvent(
                            user_id=user_id,
                            tier=tier,
                            tool=name,
                            cache_hit=True,
                            stale_fallback=True,
                            duration_ms=(time.perf_counter() - start) * 1000,
                            ok=True,
                            error=None,
                        ),
                    )
                    return stale
            emit_audit(
                AuditEvent(
                    user_id=user_id,
                    tier=tier,
                    tool=name,
                    cache_hit=False,
                    stale_fallback=False,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    ok=False,
                    error="tool_error",
                ),
            )
            raise McpError(
                ErrorData(
                    code=-32031,
                    message="The warehouse returned an error for this request.",
                    data=None,
                ),
            ) from exc
        except McpError:
            raise
        except Exception as exc:
            if ttl is not None:
                stale = cache.get_stale(key)
                if stale is not None:
                    emit_audit(
                        AuditEvent(
                            user_id=user_id,
                            tier=tier,
                            tool=name,
                            cache_hit=True,
                            stale_fallback=True,
                            duration_ms=(time.perf_counter() - start) * 1000,
                            ok=True,
                            error=None,
                        ),
                    )
                    return stale
            emit_audit(
                AuditEvent(
                    user_id=user_id,
                    tier=tier,
                    tool=name,
                    cache_hit=False,
                    stale_fallback=False,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    ok=False,
                    error=type(exc).__name__,
                ),
            )
            raise McpError(
                ErrorData(
                    code=-32032,
                    message="Unexpected server error while executing a tool.",
                    data=None,
                ),
            ) from exc

        duration_ms = (time.perf_counter() - start) * 1000
        if ttl is not None:
            cache.set_success(key, result, ttl_sec=ttl)

        emit_audit(
            AuditEvent(
                user_id=user_id,
                tier=tier,
                tool=name,
                cache_hit=False,
                stale_fallback=False,
                duration_ms=duration_ms,
                ok=True,
                error=None,
            ),
        )
        return result


load_dotenv(_find_project_root() / ".env", override=False)

_auth, _verifier = _auth_bundle()
_mcp_host = (os.getenv("MCP_HOST") or "0.0.0.0").strip()
_mcp_port = int((os.getenv("MCP_PORT") or "8000").strip())
_mcp_stateless = os.getenv("MCP_STATELESS_HTTP", "").lower() in ("1", "true", "yes")
_mcp_json = os.getenv("MCP_JSON_RESPONSE", "true").lower() not in ("0", "false", "no")

mcp = RedshiftProductionMCP(
    "redshift",
    host=_mcp_host,
    port=_mcp_port,
    stateless_http=_mcp_stateless,
    json_response=_mcp_json,
    auth=_auth,
    token_verifier=_verifier,
    transport_security=_transport_security_from_env(),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

_client: RedshiftClient | None = None


def setup_logging() -> logging.Logger:
    """Configure audit logging to stderr and project-root log file."""
    logger = logging.getLogger("redshift_mcp")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def _get_logger() -> logging.Logger:
    return logging.getLogger("redshift_mcp")


def _get_client() -> RedshiftClient:
    global _client
    if _client is None:
        _client = RedshiftClient(get_settings())
    return _client


def _settings() -> Settings:
    return get_settings()


def _filter_schema(name: str, settings: Settings) -> bool:
    low = name.lower()
    if low in settings.blocklist_schemas:
        return False
    if settings.allowlist_schemas and low not in settings.allowlist_schemas:
        return False
    return True


def _should_include_avg(data_type: str) -> bool:
    d = data_type.lower()
    tokens = (
        "int",
        "numeric",
        "decimal",
        "double",
        "real",
        "float",
        "date",
        "timestamp",
    )
    return any(t in d for t in tokens)


@mcp.tool()
def list_schemas() -> list[str]:
    """List schemas visible to the user (honoring ALLOWED_SCHEMAS / BLOCKED_SCHEMAS)."""
    logger = _get_logger()
    settings = _settings()
    sql_primary = "SELECT schema_name FROM SVV_REDSHIFT_SCHEMAS ORDER BY schema_name"
    sql_fallback = "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"

    try:
        result = _get_client().execute(sql_primary, include_column_meta=False)
        executed_sql = sql_primary
    except Exception as e:
        logger.warning(
            "list_schemas SVV_REDSHIFT_SCHEMAS failed (%s); falling back to information_schema",
            e,
        )
        result = _get_client().execute(sql_fallback, include_column_meta=False)
        executed_sql = sql_fallback

    rows = result["rows"]
    logger.info(
        "tool=list_schemas executed_sql=%s elapsed_ms=%.2f rows=%s",
        executed_sql,
        result["elapsed_ms"],
        len(rows),
    )

    names = [str(r["schema_name"]) for r in rows if r.get("schema_name") is not None]
    return sorted([n for n in names if _filter_schema(n, settings)])


@mcp.tool()
def list_tables(schema: str) -> list[dict[str, Any]]:
    """List tables/views for a schema with estimated rows/size where available."""
    logger = _get_logger()
    validate_identifier(schema, kind="schema")

    sql_svv = """
    SELECT
      "table" AS table_name,
      tbl_rows AS estimated_rows,
      size AS size_mb
    FROM svv_table_info
    WHERE "schema" = %s
    ORDER BY "table"
    """

    try:
        result = _get_client().execute(sql_svv, (schema,), include_column_meta=False)
        executed_sql = "svv_table_info"
    except Exception as e:
        logger.warning(
            "list_tables svv_table_info failed (%s); falling back to information_schema",
            e,
        )
        sql_is = """
        SELECT
          table_name,
          CAST(NULL AS BIGINT) AS estimated_rows,
          CAST(NULL AS BIGINT) AS size_mb
        FROM information_schema.tables
        WHERE table_schema = %s
        ORDER BY table_name
        """
        result = _get_client().execute(sql_is, (schema,), include_column_meta=False)
        executed_sql = "information_schema.tables"

    logger.info(
        "tool=list_tables schema=%s source=%s elapsed_ms=%.2f rows=%s",
        schema,
        executed_sql,
        result["elapsed_ms"],
        len(result["rows"]),
    )

    type_map: dict[str, str] = {}
    try:
        tr = _get_client().execute(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = %s
            """,
            (schema,),
            include_column_meta=False,
        )
        for row in tr["rows"]:
            tn = row.get("table_name")
            if tn:
                type_map[str(tn)] = str(row.get("table_type") or "")
    except Exception:
        pass

    out: list[dict[str, Any]] = []
    for row in result["rows"]:
        tn = row.get("table_name")
        if tn is None:
            continue
        name = str(tn)
        out.append(
            {
                "table_name": name,
                "table_type": type_map.get(name, "unknown"),
                "estimated_rows": row.get("estimated_rows"),
                "size_mb": row.get("size_mb"),
            }
        )
    return out


@mcp.tool()
def describe_table(schema: str, table: str) -> list[dict[str, Any]]:
    """Describe columns and table-level stats using catalog views."""
    logger = _get_logger()
    validate_identifier(schema, kind="schema")
    validate_identifier(table, kind="table")

    cols = _get_client().execute(
        """
        SELECT
          column_name,
          data_type,
          is_nullable,
          column_default,
          ordinal_position,
          encoding,
          distkey,
          sortkey
        FROM svv_redshift_columns
        WHERE schema_name = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
        include_column_meta=False,
    )

    info_row: dict[str, Any] | None = None
    try:
        ti = _get_client().execute(
            """
            SELECT
              diststyle,
              sortkey1,
              max_varchar,
              size,
              tbl_rows
            FROM svv_table_info
            WHERE "schema" = %s AND "table" = %s
            LIMIT 1
            """,
            (schema, table),
            include_column_meta=False,
        )
        if ti["rows"]:
            info_row = ti["rows"][0]
    except Exception:
        info_row = None

    logger.info(
        "tool=describe_table schema=%s table=%s elapsed_ms=%.2f column_rows=%s",
        schema,
        table,
        cols["elapsed_ms"],
        len(cols["rows"]),
    )

    enriched: list[dict[str, Any]] = []
    for row in cols["rows"]:
        item = dict(row)
        if info_row:
            item["table_diststyle"] = info_row.get("diststyle")
            item["table_sortkey1"] = info_row.get("sortkey1")
            item["table_max_varchar"] = info_row.get("max_varchar")
            item["table_size_mb"] = info_row.get("size")
            item["table_tbl_rows"] = info_row.get("tbl_rows")
        enriched.append(item)
    return enriched


@mcp.tool()
def sample_table(schema: str, table: str, limit: int = 5) -> dict[str, Any]:
    """Sample rows from a table (LIMIT capped at 100)."""
    logger = _get_logger()
    validate_identifier(schema, kind="schema")
    validate_identifier(table, kind="table")
    lim = max(1, min(int(limit), 100))

    qs = quote_ident(schema)
    qt = quote_ident(table)
    sql_text = f"SELECT * FROM {qs}.{qt} LIMIT {lim}"

    rewritten = validate_query(sql_text, _settings())
    result = _get_client().execute(rewritten)

    logger.info(
        "tool=sample_table schema=%s table=%s limit=%s user_sql=%s "
        "executed_sql=%s elapsed_ms=%.2f rows=%s",
        schema,
        table,
        lim,
        sql_text,
        rewritten,
        result["elapsed_ms"],
        result["row_count"],
    )

    return {
        "rows": result["rows"],
        "columns": result["columns"],
        "row_count": result["row_count"],
        "rewritten_sql": rewritten,
        "elapsed_ms": result["elapsed_ms"],
    }


@mcp.tool()
def run_select_query(sql: str) -> dict[str, Any]:
    """Run an arbitrary SELECT-only query after validation and rewriting."""
    logger = _get_logger()
    settings = _settings()

    try:
        rewritten = validate_query(sql, settings)
    except UnsafeQueryError as e:
        logger.warning(
            "validation_failed reason=%s user_sql=%s message=%s",
            e.reason,
            sql,
            str(e),
        )
        raise ValueError(f"{e.reason}: {e}") from e

    result = _get_client().execute(rewritten)
    truncated = result["row_count"] >= settings.max_rows_returned

    logger.info(
        "tool=run_select_query user_sql=%s executed_sql=%s elapsed_ms=%.2f rows=%s truncated=%s",
        sql,
        rewritten,
        result["elapsed_ms"],
        result["row_count"],
        truncated,
    )

    return {
        "rewritten_sql": rewritten,
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": truncated,
        "elapsed_ms": result["elapsed_ms"],
    }


@mcp.tool()
def profile_column(schema: str, table: str, column: str) -> dict[str, Any]:
    """Profile a column with safe aggregates (null/distinct/min/max/avg when applicable)."""
    logger = _get_logger()
    validate_identifier(schema, kind="schema")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")

    dtype_rows = _get_client().execute(
        """
        SELECT data_type
        FROM svv_redshift_columns
        WHERE schema_name = %s AND table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (schema, table, column),
        include_column_meta=False,
    )["rows"]

    if not dtype_rows:
        msg = f"Column not found: {schema}.{table}.{column}"
        raise ValueError(msg)

    data_type = str(dtype_rows[0]["data_type"])
    use_avg = _should_include_avg(data_type)

    qs = quote_ident(schema)
    qt = quote_ident(table)
    qc = quote_ident(column)

    parts = [
        "COUNT(*) AS total_rows",
        f"SUM(CASE WHEN {qc} IS NULL THEN 1 ELSE 0 END) AS null_count",
        f"COUNT(DISTINCT {qc}) AS distinct_count",
        f"MIN({qc}) AS min_val",
        f"MAX({qc}) AS max_val",
    ]
    if use_avg:
        parts.append(f"AVG({qc}) AS avg_val")

    sql_text = f"SELECT {', '.join(parts)} FROM {qs}.{qt}"
    rewritten = validate_query(sql_text, _settings())
    result = _get_client().execute(rewritten)

    logger.info(
        "tool=profile_column schema=%s table=%s column=%s dtype=%s "
        "executed_sql=%s elapsed_ms=%.2f rows=%s",
        schema,
        table,
        column,
        data_type,
        rewritten,
        result["elapsed_ms"],
        result["row_count"],
    )

    row = result["rows"][0] if result["rows"] else {}
    out = dict(row)
    out["data_type"] = data_type
    return out


@mcp.tool()
def get_table_size(schema: str, table: str) -> dict[str, Any]:
    """Return estimated table size (MB) and row count from SVV_TABLE_INFO."""
    logger = _get_logger()
    validate_identifier(schema, kind="schema")
    validate_identifier(table, kind="table")

    result = _get_client().execute(
        """
        SELECT size AS size_mb, tbl_rows AS row_count
        FROM svv_table_info
        WHERE "schema" = %s AND "table" = %s
        LIMIT 1
        """,
        (schema, table),
        include_column_meta=False,
    )

    logger.info(
        "tool=get_table_size schema=%s table=%s elapsed_ms=%.2f rows=%s",
        schema,
        table,
        result["elapsed_ms"],
        len(result["rows"]),
    )

    if not result["rows"]:
        return {"size_mb": None, "row_count": None}
    return {
        "size_mb": result["rows"][0].get("size_mb"),
        "row_count": result["rows"][0].get("row_count"),
    }


def main() -> None:
    try:
        setup_logging()
        configure_audit_logging()
        settings = get_settings()
        _get_logger().info("redshift-mcp starting transport=%s", settings.mcp_transport)
        mcp.run(transport=settings.mcp_transport)  # type: ignore[arg-type]
    except Exception:
        # Cursor / Claude log MCP stderr; ensure crashes are visible when the client shows -32000.
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
