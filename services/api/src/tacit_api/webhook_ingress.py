"""Development-only ingress: expose signed GitHub webhooks, never the Tacit API."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from tacit_api.github import verify_webhook

PATH = "/v1/github/webhooks"
MAXIMUM = 2_000_000


def create_proxy(secret: str, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    if not secret:
        raise ValueError("Configure the backend webhook secret before exposing webhook ingress")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False, transport=transport
        ) as client:
            app.state.backend = client
            yield

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )

    @app.post(PATH)
    async def webhook(request: Request) -> Response:
        # No inbound authorization/cookies/query/host can choose a destination.
        delivery = request.headers.get("x-github-delivery", "")
        event = request.headers.get("x-github-event", "")
        signature = request.headers.get("x-hub-signature-256", "")
        if not delivery or len(delivery) > 128 or not event or len(event) > 64:
            raise HTTPException(422, "Missing or invalid webhook metadata")
        raw = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAXIMUM:
                        raise HTTPException(413, "Webhook exceeds upload budget")
                    raw.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Webhook upload timed out") from None
        body = bytes(raw)
        if not verify_webhook(body, signature, secret):
            raise HTTPException(401, "Invalid webhook signature")
        headers = {
            "content-type": "application/json",
            "x-github-delivery": delivery,
            "x-github-event": event,
            "x-hub-signature-256": signature,
        }
        try:
            result = await request.app.state.backend.post(
                "http://127.0.0.1:8000" + PATH, content=body, headers=headers
            )
        except httpx.HTTPError:
            raise HTTPException(502, "Local webhook backend unavailable") from None
        return Response(
            result.content, status_code=result.status_code, media_type="application/json"
        )

    return app


if __name__ == "__main__":
    import uvicorn

    from tacit_api.app import Settings

    uvicorn.run(
        create_proxy(Settings().github_webhook_secret),
        host="127.0.0.1",
        port=8002,
        limit_concurrency=16,
        timeout_keep_alive=5,
    )
