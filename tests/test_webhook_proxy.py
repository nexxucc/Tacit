"""The temporary tunnel must never expose other API routes or forward unsigned data."""

import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient
from tacit_api.webhook_ingress import MAXIMUM, PATH, create_proxy

SECRET = "fixture-webhook-secret"
BODY = b'{"zen":"fixture"}'


def signature(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def headers(body=BODY):
    return {
        "x-github-delivery": "fixture-delivery",
        "x-github-event": "ping",
        "x-hub-signature-256": signature(body),
    }


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", PATH),
        ("POST", PATH + "/"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("POST", "/v1/auth/device/start"),
        ("GET", "/v1/repos/gh-1422572999/context"),
    ],
)
def test_other_routes_never_forward(method, path):
    def forbidden(request):
        raise AssertionError("Non-webhook traffic must never reach the API")

    with TestClient(create_proxy(SECRET, transport=httpx.MockTransport(forbidden))) as client:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code in {404, 405}


def test_signed_body_is_forwarded_unchanged_with_only_webhook_headers():
    forwarded = []

    def respond(request):
        forwarded.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    with TestClient(create_proxy(SECRET, transport=httpx.MockTransport(respond))) as client:
        response = client.post(
            PATH + "?destination=https://example.com",
            content=BODY,
            headers={
                **headers(),
                "Authorization": "Bearer fixture-private-token",
                "Cookie": "private=value",
            },
        )
        assert response.json() == {"status": "accepted"}
    assert len(forwarded) == 1
    request = forwarded[0]
    assert str(request.url) == "http://127.0.0.1:8000" + PATH
    assert request.content == BODY
    assert request.headers["x-hub-signature-256"] == signature(BODY)
    assert "authorization" not in request.headers and "cookie" not in request.headers


@pytest.mark.parametrize("case,status", [("signature", 401), ("metadata", 422), ("oversize", 413)])
def test_invalid_or_oversized_webhook_never_forwards(case, status):
    def forbidden(request):
        raise AssertionError("Invalid webhook must not reach the API")

    body = BODY
    supplied = headers()
    if case == "signature":
        supplied["x-hub-signature-256"] = "sha256=invalid"
    elif case == "metadata":
        supplied["x-github-delivery"] = "x" * 129
    else:
        body = b"x" * (MAXIMUM + 1)
        supplied = headers(body)
    with TestClient(create_proxy(SECRET, transport=httpx.MockTransport(forbidden))) as client:
        assert client.post(PATH, content=body, headers=supplied).status_code == status


def test_secret_is_required():
    with pytest.raises(ValueError, match="webhook secret"):
        create_proxy("")
