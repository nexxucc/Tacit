"""Provider responses are fixtures: these tests never spend Gnani credits or contact GitHub."""

import hashlib
import hmac
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from tacit_api.access import now
from tacit_api.app import Settings, create_app
from tacit_api.db import AuditEvent, Base, Credential, DevicePairing
from tacit_api.github import GitHub


class Provider:
    def __init__(self):
        self.uid = 1
        self.pending = False
        self.access = True
        self.org_member = True
        self.org_owner = False
        self.membership_error = False
        self.poll_count = 0

    def request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login/device/code":
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if path == "/login/oauth/access_token":
            self.poll_count += 1
            return httpx.Response(
                200,
                json={"error": "authorization_pending"}
                if self.pending
                else {"access_token": f"user-{self.uid}", "expires_in": 28800},
            )
        token = request.headers.get("Authorization", "").split("user-")[-1]
        uid = int(token)
        if path == "/user":
            return httpx.Response(200, json={"id": uid, "login": f"user{uid}"})
        owner = {"id": 1, "login": "user1", "type": "User"}
        org = {"id": 20, "login": "team", "type": "Organization"}
        if path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {"id": 10, "app_id": 100, "account": owner},
                        {"id": 30, "app_id": 100, "account": org},
                        {"id": 999, "app_id": 999, "account": owner},
                    ]
                    if self.access
                    else []
                },
            )
        if path == "/user/installations/10/repositories":
            return httpx.Response(
                200, json={"repositories": [{"id": 101, "full_name": "user1/code", "owner": owner}]}
            )
        if path == "/user/installations/30/repositories":
            return httpx.Response(
                200, json={"repositories": [{"id": 202, "full_name": "team/code", "owner": org}]}
            )
        if path == "/user/memberships/orgs/team":
            if self.membership_error:
                return httpx.Response(403, json={"message": "Cannot verify"})
            return (
                httpx.Response(
                    200, json={"state": "active", "role": "admin" if self.org_owner else "member"}
                )
                if self.org_member
                else httpx.Response(404)
            )
        if path.startswith("/repos/") and "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"sha": sha})
        if path.startswith("/repos/") and "/contents/" in path:
            file = path.split("/contents/", 1)[1]
            if file == "missing.py":
                return httpx.Response(404)
            return httpx.Response(200, json={"type": "file", "path": file, "sha": "b" * 40})
        raise AssertionError(path)


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request: pytest.FixtureRequest):
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(engine, "connect")
        def foreign_keys(conn, record):
            conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()
        return
    url = os.environ.get("TACIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TACIT_TEST_DATABASE_URL to run real PostgreSQL migration/access tests")
    schema = "test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    env = {**os.environ, "TACIT_DATABASE_URL": scoped.render_as_string(hide_password=False)}
    try:
        subprocess.run(
            ["uv", "run", "--no-sync", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
        )
        engine = create_engine(scoped)
        yield engine
        engine.dispose()
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def setup(engine: Engine):
    provider = Provider()
    key = Fernet.generate_key().decode()
    settings = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=key,
        github_webhook_secret="fixture-secret",
    )
    github = GitHub("fixture", 100, httpx.MockTransport(provider.request))
    app = create_app(settings, github=github, database=engine)
    with TestClient(app) as client:
        yield client, provider, engine


def login(client, provider, engine, uid=1):
    provider.uid = uid
    start = client.post("/v1/auth/device/start")
    assert start.status_code == 200, start.text
    assert "device_code" not in start.json()
    with Session(engine) as db, db.begin():
        pairing = db.scalar(select(DevicePairing).where(DevicePairing.consumed.is_(False)))
        assert pairing.device_code != "device-secret"
        pairing.next_poll_at = now() - timedelta(seconds=1)
    result = client.post(
        "/v1/auth/device/poll", json={"pairing_token": start.json()["pairing_token"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "authorized", result.text
    return {"Authorization": "Bearer " + result.json()["token"]}


def test_pairing_interval_replay_encryption_and_logout(setup):
    client, provider, engine = setup
    pairing = client.post("/v1/auth/device/start").json()
    assert (
        client.post(
            "/v1/auth/device/poll", json={"pairing_token": pairing["pairing_token"]}
        ).json()["status"]
        == "pending"
    )
    assert provider.poll_count == 0
    with Session(engine) as db, db.begin():
        row = db.scalar(select(DevicePairing))
        row.next_poll_at = now() - timedelta(seconds=1)
    body = {"pairing_token": pairing["pairing_token"]}
    result = client.post("/v1/auth/device/poll", json=body).json()
    assert result["status"] == "authorized"
    assert client.post("/v1/auth/device/poll", json=body).status_code == 401
    headers = {"Authorization": "Bearer " + result["token"]}
    assert client.get("/v1/workspaces", headers=headers).json() == [{"id": "gh-1"}, {"id": "gh-20"}]
    with Session(engine) as db:
        credential = db.scalar(select(Credential))
        assert credential.token_hash != result["token"]
        assert credential.provider_token != "user-1"
    assert client.post("/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/v1/workspaces", headers=headers).status_code == 401


def test_multi_workspace_roles_agent_ceiling_and_revocation(setup):
    client, provider, engine = setup
    owner = login(client, provider, engine)
    # Organization membership alone gives learner, never administrator/maintainer.
    assert client.post("/v1/repos/gh-202/publications", headers=owner).status_code == 403
    learner = login(client, provider, engine, 2)
    assert client.get("/v1/workspaces", headers=learner).json() == [{"id": "gh-20"}]
    assert client.post("/v1/repos/gh-101/publications", headers=learner).status_code == 404
    assert client.post("/v1/repos/gh-202/publications", headers=learner).status_code == 403
    # Personal owner must explicitly invite a collaborator; GitHub access already exists.
    invite = {"github_id": "2", "repository_ids": ["gh-101"]}
    assert (
        client.post("/v1/workspaces/gh-1/invitations", headers=owner, json=invite).status_code
        == 200
    )
    assert client.get("/v1/workspaces", headers=learner).json() == [{"id": "gh-1"}, {"id": "gh-20"}]
    grant = {"user_id": "gh-2", "role": "maintainer", "repository_id": "gh-101"}
    assert client.post("/v1/workspaces/gh-1/roles", headers=learner, json=grant).status_code == 403
    assert client.post("/v1/workspaces/gh-1/roles", headers=owner, json=grant).status_code == 200
    assert client.post("/v1/repos/gh-101/publications", headers=learner).status_code == 422
    agent = client.post(
        "/v1/auth/agent-tokens", headers=learner, json={"repository_ids": ["gh-101"]}
    ).json()
    headers = {"Authorization": "Bearer " + agent["token"]}
    assert client.get("/v1/auth/me", headers=headers).json()["agent"] is True
    assert client.post("/v1/repos/gh-101/publications", headers=headers).status_code == 403
    assert client.get("/v1/workspaces/gh-20/repos", headers=headers).status_code == 404
    assert (
        client.post(
            "/v1/auth/agent-tokens", headers=headers, json={"repository_ids": ["gh-101"]}
        ).status_code
        == 403
    )
    assert (
        client.post("/v1/workspaces/gh-1/roles/revoke", headers=owner, json=grant).status_code
        == 200
    )
    assert client.post("/v1/repos/gh-101/publications", headers=learner).status_code == 403
    assert client.delete("/v1/workspaces/gh-1/members/gh-2", headers=owner).status_code == 200
    assert client.get("/v1/workspaces/gh-1/repos", headers=headers).status_code == 404
    assert client.post("/v1/auth/logout", headers=learner).status_code == 200
    assert client.get("/v1/auth/me", headers=headers).status_code == 401


def test_cross_workspace_grants_last_admin_and_provider_failure(setup):
    client, provider, engine = setup
    owner = login(client, provider, engine)
    assert (
        client.post(
            "/v1/workspaces/gh-1/roles",
            headers=owner,
            json={"user_id": "gh-1", "role": "maintainer", "repository_id": "gh-202"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/workspaces/gh-1/roles/revoke",
            headers=owner,
            json={"user_id": "gh-1", "role": "workspace_admin"},
        ).status_code
        == 409
    )
    provider.membership_error = True
    assert client.get("/v1/workspaces", headers=owner).status_code == 503
    provider.membership_error = False
    provider.access = False
    assert client.get("/v1/workspaces", headers=owner).json() == []
    assert client.post("/v1/repos/gh-101/publications", headers=owner).status_code == 404


def webhook(client, payload, event="installation_repositories", delivery="delivery-1"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"fixture-secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/v1/github/webhooks",
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
        },
    )


def test_webhook_deduplication_removal_and_authorization_revoke(setup):
    client, provider, engine = setup
    owner = login(client, provider, engine)
    payload = {
        "installation": {"id": 10},
        "action": "removed",
        "repositories_removed": [{"id": 101}],
    }
    assert webhook(client, payload).json()["status"] == "accepted"
    assert webhook(client, payload).json()["status"] == "duplicate"
    assert webhook(client, {**payload, "action": "different"}).status_code == 409
    assert client.get("/v1/workspaces/gh-1/repos", headers=owner).json() == []
    assert (
        webhook(
            client,
            {"sender": {"id": 1}, "action": "revoked"},
            "github_app_authorization",
            "delivery-2",
        ).status_code
        == 200
    )
    assert client.get("/v1/auth/me", headers=owner).status_code == 401


def test_org_owner_bootstrap_requires_provider_verification(setup):
    client, provider, engine = setup
    provider.org_owner = True
    owner = login(client, provider, engine)
    assert client.get("/v1/workspaces/gh-20/members", headers=owner).status_code == 200
    assert client.post("/v1/repos/gh-202/publications", headers=owner).status_code == 403
    with Session(engine) as db:
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.action == "role.bootstrap")) is not None
        )


def test_postgres_pairing_race_and_immutable_audit(setup):
    client, provider, engine = setup
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL locks and immutable triggers")
    start = client.post("/v1/auth/device/start").json()
    with Session(engine) as db, db.begin():
        db.scalar(select(DevicePairing)).next_poll_at = now() - timedelta(seconds=1)

    def poll():
        return client.post(
            "/v1/auth/device/poll", json={"pairing_token": start["pairing_token"]}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: poll(), range(2))) == [200, 401]
    with pytest.raises(Exception, match="Tacit immutable record"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE audit_events SET action = 'tampered'"))


def test_installation_suspend_cannot_be_undone_by_access_refresh(setup):
    client, provider, engine = setup
    owner = login(client, provider, engine)
    payload = {"installation": {"id": 10}, "action": "suspend"}
    assert webhook(client, payload, "installation", "suspend").status_code == 200
    assert client.get("/v1/workspaces/gh-1/repos", headers=owner).status_code == 404
    payload["action"] = "unsuspend"
    assert webhook(client, payload, "installation", "unsuspend").status_code == 200
    assert client.get("/v1/workspaces/gh-1/repos", headers=owner).status_code == 200


def test_persisted_snapshot_retrieval_and_tenant_isolation(setup):
    from tacit_api.db import DecisionRevision, Snapshot, SnapshotMember
    from tacit_api.demo import DemoStore

    client, provider, engine = setup
    owner = login(client, provider, engine)
    decision = DemoStore().snapshot("scheduler", "snapshot-1").decisions[0]
    with Session(engine) as db, db.begin():
        db.add(
            DecisionRevision(
                workspace_id="gh-1",
                repository_id="gh-101",
                id=decision.revision_id,
                decision_id=decision.id,
                confirmed_by="gh-1",
                content=decision.model_dump(mode="json"),
            )
        )
        db.add(
            Snapshot(
                workspace_id="gh-1",
                repository_id="gh-101",
                id="snapshot-1",
                source_commit="demo-commit-1",
                digest="fixture",
            )
        )
        db.flush()
        db.add(
            SnapshotMember(
                workspace_id="gh-1",
                repository_id="gh-101",
                snapshot_id="snapshot-1",
                revision_id=decision.revision_id,
            )
        )
    body = {"snapshot_id": "snapshot-1", "source_commit": "demo-commit-1"}
    result = client.post("/v1/repos/gh-101/context", headers=owner, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["decisions"][0]["statement"] == decision.statement
    assert result.json()["usage"] == {
        "stt_calls": 0,
        "tts_calls": 0,
        "reasoning_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert client.post("/v1/repos/gh-202/context", headers=owner, json=body).status_code == 404
    learner = login(client, provider, engine, 2)
    assert client.post("/v1/repos/gh-101/context", headers=learner, json=body).status_code == 404


def test_expired_credentials_and_agent_scope_cannot_be_bypassed(setup):
    from tacit_api.access import digest

    client, provider, engine = setup
    owner = login(client, provider, engine)
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    headers = {"Authorization": "Bearer " + agent["token"]}
    assert (
        client.post(
            "/v1/repos/gh-202/context",
            headers=headers,
            json={"snapshot_id": "any", "source_commit": "any"},
        ).status_code
        == 404
    )
    with Session(engine) as db, db.begin():
        db.get(Credential, digest(owner["Authorization"].split()[1])).expires_at = (
            now() - timedelta(seconds=1)
        )
    assert client.get("/v1/auth/me", headers=headers).status_code == 401


def test_repository_readdition_requires_signed_event(setup):
    client, provider, engine = setup
    owner = login(client, provider, engine)
    payload = {"installation": {"id": 10}, "repositories_removed": [{"id": 101}]}
    assert webhook(client, payload).status_code == 200
    assert client.post("/v1/repos/gh-101/publications", headers=owner).status_code == 404
    payload = {
        "installation": {"id": 10},
        "repositories_added": [{"id": 101, "full_name": "user1/code"}],
    }
    assert webhook(client, payload, delivery="add").status_code == 200
    assert client.post("/v1/repos/gh-101/publications", headers=owner).status_code == 422
