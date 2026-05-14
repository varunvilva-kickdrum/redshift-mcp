"""AWS Lambda entrypoint: Mangum wraps FastMCP streamable HTTP ASGI."""

from __future__ import annotations

from mangum import Mangum

from redshift_mcp.server import mcp

# API Gateway HTTP API v2 and Lambda Function URLs share the same request shape; Mangum
# supports both. Default invoke mode (buffered) matches MCP_JSON_RESPONSE=true JSON bodies.
handler = Mangum(mcp.streamable_http_app(), lifespan="off")
