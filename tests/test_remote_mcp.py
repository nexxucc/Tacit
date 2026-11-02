"""OAuth and real MCP protocol tests using GitHub fixtures; no paid/external calls."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_api.access import now
from tacit_api.app import Settings, create_app
from tacit_api.db import Credential, MCPGrant
from tacit_api.github import GitHub
from test_access_management import Provider, engine, login  # noqa: F401

ISSUER = "https://tacit.test"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "http://127.0.0.1:8345/callback"
VERIFIER = "a" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
CLIENTS = {
    "fixture": {"name": "Test editor", "redirect_uris": [REDIRECT]},
    "other": {"name": "Other editor", "redirect_uris": [REDIRECT]},
}


@pytest.fixture
def remote(engine):  # noqa: F811
    provider = Provider()
    settings = Settings(
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=Fernet.generate_key().decode(),
        mcp_public_url=ISSUER,
        mcp_clients=CLIENTS,
    )
    app = create_app(
        settings,
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
    )
    app.state.mcp_test_key = settings.credential_encryption_key
    with TestClient(app, base_url=ISSUER) as client:
        yield client, provider, engine


def start(client, **overrides):
    params = dict(
        client_id="fixture",
        redirect_uri=REDIRECT,
        response_type="code",
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="tacit:read",
        state="client-state",
        resource=RESOURCE,
    )
    params.update(overrides)
    return client.get("/authorize", params=params, follow_redirects=False)


def consent(remote, uid=1):
    client, provider, db = remote
    human = login(client, provider, db, uid=uid)
    response = start(client)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    page = client.get(location)
    assert page.status_code == 200, page.text
    assert "Test editor" in page.text and "cannot teach or publish" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    ticket = parse_qs(urlsplit(location).query)["request"][0]
    return human, ticket


def approve(remote, *, repos=None, uid=1):
    client, _, _ = remote
    human, ticket = consent(remote, uid=uid)
    result = client.post(
        "/oauth/consent",
        headers={**human, "Origin": ISSUER},
        json={"request": ticket, "repository_ids": repos or ["gh-101"]},
    )
    assert result.status_code == 200, result.text
    url = result.json()["redirect"]
    params = parse_qs(urlsplit(url).query)
    assert params["state"] == ["client-state"]
    return human, params["code"][0]


def exchange(client, code, **overrides):
    data = dict(
        grant_type="authorization_code",
        client_id="fixture",
        code=code,
        code_verifier=VERIFIER,
        redirect_uri=REDIRECT,
        resource=RESOURCE,
    )
    data.update(overrides)
    return client.post("/token", data=data)


def grant(remote):
    human, code = approve(remote)
    response = exchange(remote[0], code)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope"] == "tacit:read"
    assert 0 < payload["expires_in"] <= 3600 and "refresh_token" not in payload
    return human, payload["access_token"]


def rpc(client, token, method, params=None, ident=1):
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}},
    )


def test_metadata_challenge_and_no_dynamic_registration(remote):
    client, _, _ = remote
    response = client.post("/mcp", json={})
    assert response.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in response.headers["www-authenticate"]
    protected = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert protected["resource"] == RESOURCE and protected["authorization_servers"] == [
        ISSUER + "/"
    ]
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert "registration_endpoint" not in metadata
    assert client.post("/register", json={}).status_code == 404


def test_complete_oauth_and_six_read_only_tools(remote):
    client, _, db = remote
    human, token = grant(remote)
    initialized = rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert initialized.status_code == 200, initialized.text
    listed = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == {
        "list_workspaces",
        "list_repositories",
        "get_context",
        "ask_repository",
        "evaluate_scenario",
        "get_decision_history",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in listed)
    result = rpc(client, token, "tools/call", {"name": "list_workspaces", "arguments": {}})
    assert result.status_code == 200, result.text
    assert "gh-1" in result.text and "gh-20" not in result.text
    denied = rpc(
        client,
        token,
        "tools/call",
        {"name": "list_repositories", "arguments": {"workspace_id": "gh-20"}},
    )
    assert denied.json()["result"]["isError"] is True
    assert token not in denied.text and human["Authorization"][7:] not in denied.text
    # Tokens for either audience must never be interchangeable.
    assert (
        client.get("/v1/auth/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    )
    assert rpc(client, human["Authorization"][7:], "tools/list").status_code == 401
    with Session(db) as session:
        row = session.scalar(select(MCPGrant).where(MCPGrant.status == "token"))
        assert row.token_hash != token and row.code_hash is None
        assert token not in row.delegated_token
        credential = session.get(Credential, row.credential_hash)
        assert credential.agent and credential.repository_ids == ["gh-101"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"resource": "https://evil.test/mcp"},
        {"resource": ""},
        {"code_challenge_method": "plain"},
        {"code_challenge": "bad"},
        {"redirect_uri": "https://evil.test/callback"},
        {"client_id": "missing"},
        {"scope": "tacit:write"},
    ],
)
def test_invalid_authorize_does_not_create_flow(remote, overrides):
    client, _, db = remote
    response = start(client, **overrides)
    assert response.status_code in {302, 400}
    if response.status_code == 302:
        assert "error=" in response.headers["location"]
        assert "evil.test" not in response.headers["location"]
    with Session(db) as session:
        assert session.scalar(select(MCPGrant)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_verifier": "b" * 64},
        {"redirect_uri": REDIRECT + "different"},
        {"client_id": "other"},
        {"resource": "https://evil.test/mcp"},
        {"resource": ""},
    ],
)
def test_bad_exchange_cannot_consume_or_steal_code(remote, overrides):
    client, _, _ = remote
    _, code = approve(remote)
    assert exchange(client, code, **overrides).status_code == 400
    assert exchange(client, code).status_code == 200
    assert exchange(client, code).status_code == 400


@pytest.mark.parametrize(
    "problem", ["origin", "no_origin", "cookie", "unknown_repo", "empty_repos", "agent"]
)
def test_consent_failures_do_not_issue_code(remote, problem):
    client, _, db = remote
    human, ticket = consent(remote)
    headers = {**human, "Origin": ISSUER}
    repos = ["gh-101"]
    if problem == "origin":
        headers["Origin"] = "https://evil.test"
    if problem == "no_origin":
        del headers["Origin"]
    if problem == "cookie":
        client.cookies.clear()
    if problem == "unknown_repo":
        repos = ["gh-999"]
    if problem == "empty_repos":
        repos = []
    if problem == "agent":
        delegated = client.post(
            "/v1/auth/agent-tokens",
            headers=human,
            json={"repository_ids": ["gh-101"], "expires_in": 300},
        )
        assert delegated.status_code == 200, delegated.text
        headers["Authorization"] = "Bearer " + delegated.json()["token"]
    result = client.post(
        "/oauth/consent", headers=headers, json={"request": ticket, "repository_ids": repos}
    )
    assert result.status_code >= 400, result.text
    with Session(db) as session:
        assert session.scalar(select(MCPGrant)).status == "pending"


@pytest.mark.parametrize(
    "revocation", ["parent_logout", "github_removal", "oauth_revoke", "expiry"]
)
def test_every_request_rechecks_revocation(remote, revocation):
    client, provider, db = remote
    human, token = grant(remote)
    assert rpc(client, token, "tools/list").status_code == 200
    if revocation == "parent_logout":
        assert client.post("/v1/auth/logout", headers=human).status_code == 200
    elif revocation == "github_removal":
        provider.access = False
    elif revocation == "oauth_revoke":
        result = client.post("/revoke", data={"token": token, "client_id": "fixture"})
        assert result.status_code == 200, result.text
    else:
        with Session(db) as session, session.begin():
            session.scalar(select(MCPGrant)).expires_at = now() - timedelta(seconds=1)
    assert rpc(client, token, "tools/list").status_code == 401


def test_wrong_host_origin_duplicate_resource_and_approval_replay(remote):
    client, _, _ = remote
    assert (
        client.get(
            "/.well-known/oauth-authorization-server", headers={"Host": "evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/.well-known/oauth-authorization-server", headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    human, ticket = consent(remote)
    body = {"request": ticket, "repository_ids": ["gh-101"]}
    result = client.post("/oauth/consent", headers={**human, "Origin": ISSUER}, json=body)
    assert result.status_code == 200
    assert (
        client.post("/oauth/consent", headers={**human, "Origin": ISSUER}, json=body).status_code
        == 400
    )
    code = parse_qs(urlsplit(result.json()["redirect"]).query)["code"][0]
    assert (
        client.post("/token", content="resource=" + RESOURCE + "&resource=" + RESOURCE).status_code
        == 400
    )
    assert exchange(client, code).status_code == 200


def test_provider_restart_preserves_grant_but_changed_issuer_cannot_use_it(remote):
    client, _, db = remote
    _, token = grant(remote)
    import asyncio

    from tacit_api.access import AccessService
    from tacit_api.mcp_oauth import MCPAuth, RegisteredClient

    restarted = MCPAuth(
        AccessService(
            db,
            GitHub("fixture", 100, httpx.MockTransport(remote[1].request)),
            client.app.state.mcp_test_key,
        ),
        ISSUER,
        {k: RegisteredClient.model_validate(v) for k, v in CLIENTS.items()},
    )
    assert asyncio.run(restarted.load_access_token(token)) is not None
    # Durable grants survive provider reinstantiation but retain their issuer binding.
    with Session(db) as session:
        row = session.scalar(select(MCPGrant))
        row.issuer = "https://other.test"
        session.commit()
    assert rpc(client, token, "tools/list").status_code == 401


def test_atomic_code_exchange_postgres(remote):
    client, _, db = remote
    if db.dialect.name != "postgresql":
        pytest.skip("Concurrent exchange requires PostgreSQL")
    _, code = approve(remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: exchange(client, code).status_code, range(2)))
    assert sorted(statuses) == [200, 400]


def test_all_six_tools_retrieve_published_fixture_and_cannot_write(remote):
    from test_publication import COMMIT, draft, publish

    client, _, _ = remote
    human, token = grant(remote)
    view = draft(client, human)
    publication = publish(client, human, view)
    assert publication.status_code == 200
    sid = publication.json()["snapshot_id"]
    scope = {"repository_id": "gh-101", "snapshot_id": sid, "source_commit": COMMIT}
    cases = {
        "list_workspaces": {},
        "list_repositories": {"workspace_id": "gh-1"},
        "get_context": {**scope, "files": ["worker.py"]},
        "ask_repository": {**scope, "question": "May an expired worker publish?"},
        "evaluate_scenario": {**scope, "facts": {"lease_valid": False}},
        "get_decision_history": {
            "repository_id": "gh-101",
            "decision_id": view["decision_id"],
            "snapshot_id": sid,
        },
    }
    for name, args in cases.items():
        result = rpc(client, token, "tools/call", {"name": name, "arguments": args})
        assert result.status_code == 200, result.text
        assert not result.json()["result"].get("isError"), result.text
        if name in {"get_context", "ask_repository", "get_decision_history"}:
            assert "Expired workers must not publish." in result.text
    unknown = rpc(client, token, "tools/call", {"name": "publish", "arguments": {}})
    assert unknown.json()["result"]["isError"]
    # Even the encrypted internal delegation can never acquire human write access.
    with Session(remote[2]) as db:
        grant_row = db.scalar(select(MCPGrant).where(MCPGrant.status == "token"))
        from tacit_api.access import AccessService

        # Use the server's normal encryption key, not OAuth access tokens, to check its defense in depth.
        service = AccessService(remote[2], None, client.app.state.mcp_test_key)
        delegated = service.decrypt(grant_row.delegated_token)
    assert (
        client.post(
            "/v1/repos/gh-101/teaching-sessions",
            headers={"Authorization": "Bearer " + delegated},
            json={
                "operation_id": "remote-write-probe",
                "statement": "must never publish",
                "source_commit": COMMIT,
                "files": ["worker.py"],
                "expected_snapshot_id": sid,
            },
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "setting",
    [
        {"mcp_public_url": "http://public.test"},
        {"mcp_public_url": "https://tacit.test/path"},
        {"mcp_public_url": "https://user:pass@tacit.test"},
        {"mcp_clients": {}},
        {"mcp_clients": {"bad": {"name": "bad", "redirect_uris": ["javascript:alert(1)"]}}},
    ],
)
def test_unsafe_remote_configuration_fails_closed(engine, setting):  # noqa: F811
    kwargs = dict(
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=Fernet.generate_key().decode(),
        mcp_public_url=ISSUER,
        mcp_clients=CLIENTS,
    )
    kwargs.update(setting)
    with pytest.raises(ValueError):
        create_app(
            Settings(**kwargs),
            github=GitHub("fixture", 100, httpx.MockTransport(Provider().request)),
            database=engine,
        )


def test_real_socket_mcp_sdk_client(engine):  # noqa: F811
    import asyncio
    import socket
    import threading
    import time

    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    provider = Provider()
    settings = Settings(
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=Fernet.generate_key().decode(),
        mcp_public_url=origin,
        mcp_clients=CLIENTS,
    )
    application = create_app(
        settings,
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
    )
    server = uvicorn.Server(uvicorn.Config(application, log_config=None, log_level=None))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("Loopback MCP failed to start")
            time.sleep(0.01)
        with httpx.Client(base_url=origin, trust_env=False) as client:
            human = login(client, provider, engine)
            auth = client.get(
                "/authorize",
                params={
                    "client_id": "fixture",
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": CHALLENGE,
                    "code_challenge_method": "S256",
                    "scope": "tacit:read",
                    "resource": origin + "/mcp",
                    "state": "real-client",
                },
            )
            page = client.get(auth.headers["location"])
            assert page.status_code == 200
            ticket = parse_qs(urlsplit(auth.headers["location"]).query)["request"][0]
            approved = client.post(
                "/oauth/consent",
                headers={**human, "Origin": origin},
                json={"request": ticket, "repository_ids": ["gh-101"]},
            )
            assert approved.status_code == 200, approved.text
            code = parse_qs(urlsplit(approved.json()["redirect"]).query)["code"][0]
            response = exchange(client, code, resource=origin + "/mcp")
            assert response.status_code == 200, response.text
            token = response.json()["access_token"]

        async def exercise():
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token}, trust_env=False
            ) as http_client:
                async with streamable_http_client(origin + "/mcp", http_client=http_client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 6
                        result = await session.call_tool("list_workspaces", {})
                        assert not result.isError
                        assert "gh-1" in str(result) and "gh-20" not in str(result)

        asyncio.run(exercise())

        async def automatic_oauth():
            from mcp.client.auth import OAuthClientProvider
            from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

            metadata = OAuthClientMetadata(
                redirect_uris=[REDIRECT],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code"],
                scope="tacit:read",
            )

            class Storage:
                tokens = None
                info = OAuthClientInformationFull(
                    **metadata.model_dump(), client_id="fixture", issuer=origin + "/"
                )

                async def get_tokens(self):
                    return self.tokens

                async def set_tokens(self, value):
                    self.tokens = value

                async def get_client_info(self):
                    return self.info

                async def set_client_info(self, value):
                    self.info = value

            storage = Storage()
            callback_result = None

            async def redirect(url):
                nonlocal callback_result
                async with httpx.AsyncClient(trust_env=False) as browser:
                    authorized = await browser.get(url)
                    assert authorized.status_code == 302, authorized.text
                    page = await browser.get(authorized.headers["location"])
                    assert page.status_code == 200, page.text
                    ticket = parse_qs(urlsplit(authorized.headers["location"]).query)["request"][0]
                    consented = await browser.post(
                        origin + "/oauth/consent",
                        headers={**human, "Origin": origin},
                        json={"request": ticket, "repository_ids": ["gh-101"]},
                    )
                    assert consented.status_code == 200, consented.text
                    params = parse_qs(urlsplit(consented.json()["redirect"]).query)
                    callback_result = (params["code"][0], params["state"][0])

            async def callback():
                assert callback_result is not None
                return callback_result

            auth = OAuthClientProvider(origin + "/mcp", metadata, storage, redirect, callback)
            async with httpx.AsyncClient(auth=auth, trust_env=False) as oauth_client:
                async with streamable_http_client(origin + "/mcp", http_client=oauth_client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("list_workspaces", {})
                        assert not result.isError
            assert storage.tokens is not None

        asyncio.run(automatic_oauth())
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def test_wrong_client_cannot_revoke_and_no_refresh_grant(remote):
    client, _, _ = remote
    _, token = grant(remote)
    assert client.post("/revoke", data={"token": token, "client_id": "other"}).status_code == 200
    assert rpc(client, token, "tools/list").status_code == 200
    assert (
        client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": "fixture",
                "resource": RESOURCE,
            },
        ).status_code
        == 400
    )
    assert client.post("/revoke", data={"token": token, "client_id": "fixture"}).status_code == 200
    assert rpc(client, token, "tools/list").status_code == 401


@pytest.mark.parametrize("stage", ["pending", "code", "parent_expired"])
def test_expired_flows_never_issue_tokens(remote, stage):
    client, _, db = remote
    if stage == "pending":
        human, ticket = consent(remote)
        with Session(db) as session, session.begin():
            session.scalar(select(MCPGrant)).expires_at = now() - timedelta(seconds=1)
        assert (
            client.post(
                "/oauth/consent",
                headers={**human, "Origin": ISSUER},
                json={"request": ticket, "repository_ids": ["gh-101"]},
            ).status_code
            == 400
        )
    else:
        human, code = approve(remote)
        with Session(db) as session, session.begin():
            if stage == "code":
                session.scalar(select(MCPGrant)).expires_at = now() - timedelta(seconds=1)
            else:
                from tacit_api.access import digest

                session.get(Credential, digest(human["Authorization"][7:])).expires_at = (
                    now() - timedelta(seconds=1)
                )
        assert exchange(client, code).status_code == 400


def test_consent_cookie_cannot_be_rebound_by_another_browser(remote):
    client, _, _ = remote
    human, ticket = consent(remote)
    saved = dict(client.cookies)
    client.cookies.clear()
    assert client.get("/oauth/consent", params={"request": ticket}).status_code == 400
    assert (
        client.post(
            "/oauth/consent",
            headers={**human, "Origin": ISSUER},
            json={"request": ticket, "repository_ids": ["gh-101"]},
        ).status_code
        == 400
    )
    client.cookies.update(saved)
    assert (
        client.post(
            "/oauth/consent",
            headers={**human, "Origin": ISSUER},
            json={"request": ticket, "repository_ids": ["gh-101"]},
        ).status_code
        == 200
    )


def test_separate_grants_keep_repository_scopes_separate(remote):
    client, _, _ = remote
    _, a = grant(remote)
    _, code = approve(remote, repos=["gh-202"], uid=2)
    b = exchange(client, code).json()["access_token"]
    for token, own, forbidden in [(a, "gh-1", "gh-20"), (b, "gh-20", "gh-1")]:
        allowed = rpc(
            client,
            token,
            "tools/call",
            {"name": "list_repositories", "arguments": {"workspace_id": own}},
        )
        assert not allowed.json()["result"].get("isError")
        denied = rpc(
            client,
            token,
            "tools/call",
            {"name": "list_repositories", "arguments": {"workspace_id": forbidden}},
        )
        assert denied.json()["result"]["isError"]


def test_cancel_returns_oauth_denial_without_issuing_access(remote):
    client, _, db = remote
    _, ticket = consent(remote)
    wrong_origin = client.post(
        "/oauth/consent/deny", json={"request": ticket}, headers={"Origin": "https://evil.test"}
    )
    assert wrong_origin.status_code == 400
    response = client.post(
        "/oauth/consent/deny", json={"request": ticket}, headers={"Origin": ISSUER}
    )
    assert response.status_code == 200
    redirect = response.json()["redirect"]
    assert redirect.startswith(REDIRECT + "?")
    assert parse_qs(urlsplit(redirect).query) == {
        "error": ["access_denied"],
        "state": ["client-state"],
    }
    assert (
        client.post(
            "/oauth/consent/deny", json={"request": ticket}, headers={"Origin": ISSUER}
        ).status_code
        == 400
    )
    with Session(db) as session:
        row = session.scalar(select(MCPGrant))
        assert row.status == "denied" and row.delegated_token is None and row.token_hash is None


def test_concurrent_users_never_share_request_credentials(remote):
    client, _, db = remote
    if db.dialect.name != "postgresql":
        pytest.skip("Concurrent requests require PostgreSQL")
    _, a = grant(remote)
    _, code = approve(remote, repos=["gh-202"], uid=2)
    b = exchange(client, code).json()["access_token"]

    def check(index):
        token, own, forbidden = (a, "gh-1", "gh-20") if index % 2 == 0 else (b, "gh-20", "gh-1")
        result = rpc(client, token, "tools/call", {"name": "list_workspaces", "arguments": {}})
        assert result.status_code == 200 and not result.json()["result"].get("isError"), result.text
        assert own in result.text and forbidden not in result.text

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(check, range(16)))
