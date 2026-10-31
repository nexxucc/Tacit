"""Adversarial wire compatibility boundaries; no credentials or external services."""

import asyncio
import importlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from tacit_api.compatibility import ProtocolCompatibility
from tacit_client.client import CompatibilityError, TacitClient, validate_response_versions
from tacit_contracts.versions import EVALUATOR_VERSION, PROTOCOL_VERSIONS, protocol_headers
from typer.testing import CliRunner
from websockets.datastructures import Headers


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("TACIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("TACIT_TOKEN", "fixture-human")
    monkeypatch.setenv("TACIT_AGENT_TOKEN", "fixture-agent")


def declarations():
    return [(k.lower().encode(), v.encode()) for k, v in protocol_headers().items()]


def asgi_call(scope, *, downstream=None):
    events = []
    calls = []

    async def receive():
        raise AssertionError("Incompatible requests must not read bodies or frames")

    async def endpoint(scope, receive, send):
        calls.append(True)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        else:
            await send({"type": "websocket.accept", "headers": []})

    async def send(event):
        events.append(event)

    asyncio.run(ProtocolCompatibility(downstream or endpoint)(scope, receive, send))
    return calls, events


@pytest.mark.parametrize("dimension,header,version", PROTOCOL_VERSIONS)
@pytest.mark.parametrize("bad", [b"", b" ", b"unknown", b"1, 1", b"\xff"])
def test_http_rejects_explicit_invalid_declaration_before_body(dimension, header, version, bad):
    scope = {
        "type": "http",
        "headers": [(header.encode(), bad)],
        "state": {"request_id": "fixture-id"},
    }
    calls, events = asgi_call(scope)
    assert calls == []
    assert events[0]["status"] == 426
    assert dict(events[0]["headers"])[b"cache-control"] == b"no-store"
    body = json.loads(events[1]["body"])
    assert body["code"] == "upgrade_required"
    assert dimension in body["message"]
    assert body["request_id"] == "fixture-id"
    assert body["retryable"] is False


@pytest.mark.parametrize("dimension,header,version", PROTOCOL_VERSIONS)
def test_duplicate_matching_header_is_rejected(dimension, header, version):
    calls, events = asgi_call(
        {"type": "http", "headers": [(header.encode(), version.encode())] * 2}
    )
    assert calls == []
    assert events[0]["status"] == 426


@pytest.mark.parametrize("headers", [[], declarations(), declarations()[:1]])
@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_legacy_matching_and_partial_requests_receive_complete_versions(headers, kind):
    calls, events = asgi_call({"type": kind, "headers": headers})
    assert calls == [True]
    assert all(dict(events[0]["headers"])[key] == value for key, value in declarations())


@pytest.mark.parametrize("denial_extension", [False, True])
def test_websocket_mismatch_denied_before_endpoint_or_frame(denial_extension):
    scope = {"type": "websocket", "headers": [(b"x-tacit-api-version", b"2")]}
    if denial_extension:
        scope["extensions"] = {"websocket.http.response": {}}
    calls, events = asgi_call(scope)
    assert not calls
    if denial_extension:
        assert events[0]["type"] == "websocket.http.response.start"
        assert events[0]["status"] == 426
        assert json.loads(events[1]["body"])["code"] == "upgrade_required"
    else:
        assert events == [
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "Incompatible Tacit versions; upgrade client and backend together",
            }
        ]


def test_response_declarations_replace_spoofed_duplicates_without_losing_other_headers():
    async def downstream(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"X-TACIT-API-VERSION", b"99"),
                    (b"x-tacit-api-version", b"99"),
                    (b"x-other", b"retained"),
                ],
            }
        )

    calls, events = asgi_call({"type": "http", "headers": []}, downstream=downstream)
    assert dict(events[0]["headers"])[b"x-other"] == b"retained"
    assert sum(key.lower() == b"x-tacit-api-version" for key, _ in events[0]["headers"]) == 1
    assert dict(events[0]["headers"])[b"x-tacit-api-version"] == b"1"


@pytest.mark.parametrize("headers", [{}, protocol_headers(), Headers(protocol_headers())])
def test_sdk_accepts_legacy_or_complete_matching_response(headers):
    validate_response_versions(headers)


@pytest.mark.parametrize(
    "headers",
    [
        httpx.Headers({"X-Tacit-API-Version": "2"}),
        httpx.Headers({"X-Tacit-API-Version": "1"}),
        httpx.Headers(
            [(key, value) for key, value in protocol_headers().items()]
            + [("X-Tacit-API-Version", "1")]
        ),
        Headers(
            [(key, value) for key, value in protocol_headers().items()]
            + [("X-Tacit-API-Version", "1")]
        ),
        httpx.Headers({**protocol_headers(), "X-Tacit-Schema-Version": ""}),
    ],
)
def test_sdk_rejects_partial_wrong_blank_and_duplicate_responses(headers):
    with pytest.raises(CompatibilityError):
        validate_response_versions(headers)


@pytest.mark.parametrize("mode", ["request", "audio"])
@pytest.mark.parametrize(
    "status,headers", [(200, {**protocol_headers(), "X-Tacit-API-Version": "2"}), (426, {})]
)
def test_sdk_rejects_incompatible_response_without_retry_or_consuming_body(mode, status, headers):
    calls = []

    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Audio body must not be consumed after version rejection")

    def transport(request):
        calls.append(request)
        assert all(request.headers[key] == value for key, value in protocol_headers().items())
        if mode == "audio":
            return httpx.Response(status, headers=headers, stream=Unreadable())
        return httpx.Response(status, headers=headers, json={"untrusted": "payload"})

    with TacitClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(CompatibilityError):
            getattr(client, mode)(
                *(("GET", "/v1/context") if mode == "request" else ("/v1/audio",))
            )
    assert len(calls) == 1


def test_sdk_rejects_wrong_evaluator_even_when_headers_match():
    with TacitClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers=protocol_headers(), json={"evaluator_version": "typed-equality-v1"}
            )
        )
    ) as client:
        with pytest.raises(CompatibilityError):
            client.request("POST", "/v1/evaluate")


def test_sdk_accepts_matching_evaluator():
    payload = {"evaluator_version": EVALUATOR_VERSION}
    with TacitClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=protocol_headers(), json=payload)
        )
    ) as client:
        assert client.request("POST", "/v1/evaluate") == payload


def test_websocket_client_rejects_mismatch_before_yield_and_closes(monkeypatch):
    module = importlib.import_module("tacit_client.client")
    opened = []
    closed = []

    @contextmanager
    def connect(url, **kwargs):
        opened.append(kwargs)
        try:
            yield SimpleNamespace(
                response=SimpleNamespace(
                    headers=Headers({**protocol_headers(), "X-Tacit-API-Version": "2"}),
                    status_code=101,
                )
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(module, "connect", connect)
    with TacitClient() as client:
        with pytest.raises(CompatibilityError):
            with client.voice_socket("/v1/live"):
                pytest.fail("Incompatible socket reached caller")
    assert opened[0]["additional_headers"]["Authorization"] == "Bearer fixture-human"
    assert closed == [True]


def test_device_login_rejects_version_before_prompt_or_saving(monkeypatch):
    cli = importlib.import_module("tacit_cli.main")
    factory = httpx.Client
    requests = []
    saves = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={**protocol_headers(), "X-Tacit-Schema-Version": "2"},
            json={
                "verification_uri": "https://must-not-open.invalid",
                "user_code": "PRIVATE",
                "expires_in": 60,
                "interval": 1,
                "pairing_token": "fixture",
            },
        )

    monkeypatch.setattr(cli, "secure_backend", lambda: None)
    monkeypatch.setattr(cli, "save_token", saves.append)
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(transport)),
    )
    result = CliRunner().invoke(cli.app, ["login"])
    assert result.exit_code == 1
    assert "Incompatible Tacit" in result.output
    assert "PRIVATE" not in result.output
    assert saves == []
    assert len(requests) == 1
    assert all(requests[0].headers[key] == value for key, value in protocol_headers().items())


def test_mcp_inherits_version_rejection_before_consuming_tool_payload(monkeypatch):
    server = importlib.import_module("tacit_mcp.server")
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path == "/v1/auth/me":
            return httpx.Response(200, headers=protocol_headers(), json={"agent": True})
        return httpx.Response(
            200,
            headers={**protocol_headers(), "X-Tacit-Schema-Version": "2"},
            json=[{"private": "untrusted"}],
        )

    monkeypatch.setattr(
        server,
        "TacitClient",
        lambda **kwargs: TacitClient(**kwargs, transport=httpx.MockTransport(transport)),
    )
    mcp = server.create_server()
    tool = mcp._tool_manager.get_tool("list_workspaces")
    with pytest.raises(CompatibilityError):
        tool.fn()
    assert [request.url.path for request in requests] == ["/v1/auth/me", "/v1/workspaces"]


def test_full_api_rejects_version_before_body_limit_or_authentication():
    from fastapi.testclient import TestClient
    from tacit_api.app import Settings, create_app

    with TestClient(create_app(Settings(demo=True))) as client:
        response = client.post(
            "/v1/repos/scheduler/voice-drafts",
            headers={"X-Tacit-API-Version": "2"},
            content=b"x" * 2_000_001,
        )
    assert response.status_code == 426
    assert response.json()["code"] == "upgrade_required"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert all(response.headers[key] == value for key, value in protocol_headers().items())


@pytest.mark.parametrize("status", [200, 401, 413, 422])
def test_full_api_declares_complete_versions_on_normal_and_error_responses(status):
    from fastapi.testclient import TestClient
    from tacit_api.app import Settings, create_app

    with TestClient(create_app(Settings(demo=True))) as client:
        if status == 200:
            response = client.get("/health/live")
        elif status == 401:
            response = client.get("/v1/workspaces")
        elif status == 413:
            response = client.post("/v1/workspaces", content=b"x" * 2_000_001)
        else:
            response = client.post(
                "/v1/repos/scheduler/context",
                headers={"Authorization": "Bearer demo-agent"},
                json={"unrecognized": True},
            )
    assert response.status_code == status
    assert all(response.headers[key] == value for key, value in protocol_headers().items())
