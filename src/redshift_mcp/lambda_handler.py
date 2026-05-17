"""AWS Lambda entrypoint: Mangum wraps FastMCP streamable HTTP ASGI."""

from __future__ import annotations

from mangum import Mangum

from redshift_mcp.server import mcp

_starlette_app = None


def _get_starlette_app():
    """Build the Starlette app once per warm container (after any session reset)."""
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = mcp.streamable_http_app()
    return _starlette_app


def _reset_streamable_session() -> None:
    """Session manager.run() is once-per-instance; reset when a prior invoke shut it down."""
    global _starlette_app
    mcp._session_manager = None  # noqa: SLF001 — Lambda lifecycle hook
    _starlette_app = None


class _LambdaStreamableMcpApp:
    """
    Initialize StreamableHTTP task group per Lambda invocation.

    Mangum lifespan=off avoids double-lifespan bugs; lifespan=auto breaks /healthz on Lambda
    because the session manager cannot be restarted on the same instance after shutdown.
    """

    async def __call__(self, scope, receive, send):
        sm = mcp._session_manager  # noqa: SLF001
        if sm is not None and sm._has_started and sm._task_group is None:  # noqa: SLF001
            _reset_streamable_session()

        app = _get_starlette_app()
        async with mcp.session_manager.run():
            await app(scope, receive, send)


handler = Mangum(_LambdaStreamableMcpApp(), lifespan="off")
