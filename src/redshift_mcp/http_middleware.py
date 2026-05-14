"""ASGI middleware: map rate-limit JSON-RPC errors to HTTP 429 + Retry-After."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MCP_RATE_LIMIT_JSONRPC = -32029


class RateLimitHttp429Middleware:
    """Defer small JSON responses so JSON-RPC -32029 can become HTTP 429."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start_message: dict[str, Any] | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = dict(message)
                return

            if message["type"] == "http.response.body":
                if start_message is None:
                    await send(message)
                    return

                body = message.get("body", b"")
                more = message.get("more_body", False)
                if more:
                    await send(start_message)
                    start_message = None
                    await send(message)
                    return

                status = int(start_message.get("status", 200))
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except Exception:
                    await send(start_message)
                    start_message = None
                    await send(message)
                    return

                err = parsed.get("error") if isinstance(parsed, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                data = err.get("data") if isinstance(err, dict) else None
                retry_after: int | None = None
                if isinstance(data, dict):
                    raw = data.get("retry_after_sec")
                    if isinstance(raw, int) and raw > 0:
                        retry_after = raw

                if code == MCP_RATE_LIMIT_JSONRPC and status == 200:
                    headers = list(start_message.get("headers") or [])
                    headers = [(k, v) for (k, v) in headers if k.lower() != b"content-length"]
                    if retry_after is not None:
                        headers.append((b"retry-after", str(retry_after).encode("ascii")))

                    new_body = json.dumps(parsed).encode("utf-8")
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 429,
                            "headers": headers,
                            "trailers": False,
                        },
                    )
                    await send({"type": "http.response.body", "body": new_body, "more_body": False})
                    start_message = None
                    return

                await send(start_message)
                start_message = None
                await send(message)
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)
