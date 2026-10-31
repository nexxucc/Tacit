"""Reject incompatible declarations before reading HTTP bodies or accepting sockets."""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from tacit_contracts.versions import PROTOCOL_VERSIONS, compatibility_message, protocol_headers


class ProtocolCompatibility:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        incoming: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", []):
            incoming.setdefault(name.lower(), []).append(value)
        incompatible = []
        for dimension, header, version in PROTOCOL_VERSIONS:
            values = incoming.get(header.lower().encode("ascii"))
            # An omitted declaration remains a legacy request. A duplicated,
            # blank, list-valued or different token is explicitly incompatible.
            if values is not None and values != [version.encode("ascii")]:
                incompatible.append(dimension)
        response_headers = [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in protocol_headers().items()
        ]
        if incompatible:
            request_id = str(scope.get("state", {}).get("request_id") or uuid4())
            data = json.dumps(
                {
                    "code": "upgrade_required",
                    "message": compatibility_message(incompatible),
                    "request_id": request_id,
                    "retryable": False,
                }
            ).encode("utf-8")
            headers = response_headers + [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
                (b"cache-control", b"no-store"),
            ]
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 426, "headers": headers})
                await send({"type": "http.response.body", "body": data})
            elif "websocket.http.response" in scope.get("extensions", {}):
                await send(
                    {"type": "websocket.http.response.start", "status": 426, "headers": headers}
                )
                await send({"type": "websocket.http.response.body", "body": data})
            else:
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "Incompatible Tacit versions; upgrade client and backend together",
                    }
                )
            return

        async def versioned_send(message: dict[str, Any]) -> None:
            if message["type"] in {
                "http.response.start",
                "websocket.accept",
                "websocket.http.response.start",
            }:
                names = {name for name, _ in response_headers}
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() not in names
                    ]
                    + response_headers,
                }
            await send(message)

        await self.app(scope, receive, versioned_send)
