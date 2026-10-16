"""Bound HTTP uploads before FastAPI parses bodies; audio sockets have separate frame limits."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any


class BodyLimit:
    def __init__(self, app: Any, maximum: int = 2000000):
        self.app, self.maximum = app, maximum

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        deadline = time.monotonic() + 30
        total = 0
        buffered: list[dict[str, Any]] = []
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(0.01, deadline - time.monotonic()))
            except TimeoutError:
                await send({"type": "http.response.start", "status": 408, "headers": []})
                await send({"type": "http.response.body", "body": b"Request upload timed out"})
                return
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > self.maximum or len(buffered) >= 4096:
                data = json.dumps(
                    {
                        "code": "request_too_large",
                        "message": "Request exceeds the upload budget",
                        "request_id": scope.get("state", {}).get("request_id", ""),
                        "retryable": False,
                    }
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": data})
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay() -> dict[str, Any]:
            nonlocal index
            if index < len(buffered):
                item = buffered[index]
                index += 1
                return item
            return await receive()

        await self.app(scope, replay, send)
