"""Adversarial scoped authorization; fixture GitHub, isolated SQLite/PostgreSQL."""

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_api.access import AccessService, digest
from tacit_api.app import Settings, create_app
from tacit_api.db import Credential, Membership, RoleAssignment, TeachingDraft, VoiceAttempt
from tacit_api.github import GitHub
from tacit_contracts.models import Role
from tacit_domain.access import DomainError
from test_access_management import Provider, login, webhook
from test_access_management import engine as engine


class AdversarialProvider(Provider):
    pending_org = False

    def __init__(self):
        super().__init__()
        self.removed_org_users = set()

    def request(self, request):
        if request.url.path == "/user/installations/30/repositories":
            org = {"id": 20, "login": "team", "type": "Organization"}
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"id": 202, "full_name": "team/code", "owner": org},
                        {"id": 203, "full_name": "team/other", "owner": org},
                    ]
                },
            )
        if request.url.path == "/user/installations/10/repositories":
            owner = {"id": 1, "login": "user1", "type": "User"}
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"id": 101, "full_name": "user1/code", "owner": owner},
                        {"id": 102, "full_name": "user1/other", "owner": owner},
                    ]
                },
            )
        if (
            request.url.path == "/user/memberships/orgs/team"
            and request.headers.get("Authorization", "").split("user-")[-1]
            in self.removed_org_users
        ):
            return httpx.Response(404)
        if request.url.path == "/user/memberships/orgs/team" and self.pending_org:
            return httpx.Response(200, json={"state": "pending", "role": "admin"})
        return super().request(request)


@pytest.fixture
def runtime(engine):
    provider = AdversarialProvider()
    key = Fernet.generate_key().decode()
    github = GitHub("fixture", 100, httpx.MockTransport(provider.request))
    settings = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=key,
        github_webhook_secret="fixture-secret",
    )
    with TestClient(create_app(settings, github=github, database=engine)) as client:
        yield client, provider, engine, AccessService(engine, github, key, read_cache_seconds=60)


MUTATIONS = (
    ("POST", "teaching-sessions"),
    ("POST", "teaching-runs"),
    ("POST", "teaching-runs/invented/answers"),
    ("POST", "teaching-runs/invented/draft"),
    ("DELETE", "teaching-runs/invented"),
    ("POST", "clarification-audio"),
    ("DELETE", "artifacts/invented"),
    ("POST", "voice-sessions/invented/archive"),
    ("POST", "voice-sessions"),
    ("PUT", "voice-sessions/invented/chunks/0"),
    ("POST", "voice-sessions/invented/finish"),
    ("DELETE", "voice-sessions/invented"),
    ("POST", "voice-sessions/invented/transcribe"),
    ("POST", "voice-drafts"),
    ("POST", "publications"),
)


@pytest.mark.parametrize("subject", ["learner", "maintainer_agent", "admin_only"])
def test_every_mutation_is_denied_before_resource_or_payload_processing(runtime, subject):
    client, provider, database, access = runtime
    if subject == "learner":
        headers = login(client, provider, database, 2)
        repo = "gh-202"
    elif subject == "admin_only":
        provider.org_owner = True
        headers = login(client, provider, database)
        repo = "gh-202"
        # Org ownership bootstraps administrator/learner, never maintainer.
    else:
        parent = login(client, provider, database)
        value = client.post(
            "/v1/auth/agent-tokens", headers=parent, json={"repository_ids": ["gh-101"]}
        ).json()["token"]
        headers = {"Authorization": "Bearer " + value}
        repo = "gh-101"
    for method, route in MUTATIONS:
        response = client.request(method, f"/v1/repos/{repo}/{route}", headers=headers, json={})
        assert response.status_code == 403, (subject, method, route, response.text)
        assert response.json()["code"] == "forbidden"
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 0
        assert db.scalar(select(func.count()).select_from(VoiceAttempt)) == 0


def test_pending_org_admin_invitation_never_bootstraps_membership_or_roles(runtime):
    client, provider, database, access = runtime
    provider.pending_org = True
    learner = login(client, provider, database, 2)
    assert client.get("/v1/workspaces", headers=learner).json() == []
    assert client.get("/v1/workspaces/gh-20/members", headers=learner).status_code == 404
    with Session(database) as db:
        assert db.get(Membership, ("gh-20", "gh-2")) is None
        assert not list(db.scalars(select(RoleAssignment).where(RoleAssignment.user_id == "gh-2")))


def test_outside_collaborator_invitation_does_not_expose_other_selected_repository(runtime):
    client, provider, database, access = runtime
    provider.org_member = False
    owner = login(client, provider, database)
    outside = login(client, provider, database, 2)
    assert client.get("/v1/workspaces", headers=outside).json() == []
    assert (
        client.post(
            "/v1/workspaces/gh-1/invitations",
            headers=owner,
            json={"github_id": "2", "repository_ids": ["gh-101"]},
        ).status_code
        == 200
    )
    assert [r["id"] for r in client.get("/v1/workspaces/gh-1/repos", headers=outside).json()] == [
        "gh-101"
    ]
    assert client.get("/v1/repos/gh-102/head", headers=outside).status_code == 404
    assert (
        client.post(
            "/v1/auth/agent-tokens", headers=outside, json={"repository_ids": ["gh-102"]}
        ).status_code
        == 404
    )
    scoped = client.post(
        "/v1/auth/agent-tokens", headers=outside, json={"repository_ids": ["gh-101"]}
    )
    assert scoped.status_code == 200
    agent = {"Authorization": "Bearer " + scoped.json()["token"]}
    assert client.get("/v1/repos/gh-102/head", headers=agent).status_code == 404


def test_cached_provider_eligibility_cannot_restore_locally_revoked_roles(runtime):
    client, provider, database, access = runtime
    owner = login(client, provider, database)
    token = owner["Authorization"].split()[1]
    assert access.principal(token, fresh=False).grants
    with Session(database) as db, db.begin():
        for grant in db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == "gh-1",
                RoleAssignment.workspace_id == "gh-1",
                RoleAssignment.role.in_([Role.LEARNER.value, Role.MAINTAINER.value]),
            )
        ):
            db.delete(grant)
    principal = access.principal(token, fresh=False)
    from tacit_domain.access import authorize

    with pytest.raises(DomainError) as denied:
        authorize(principal, "gh-1", "gh-101")
    assert denied.value.code == "forbidden"


def test_local_suspension_survives_a_fresh_provider_signin(runtime):
    client, provider, database, access = runtime
    provider.org_owner = True
    owner = login(client, provider, database)
    provider.org_owner = False
    learner = login(client, provider, database, 2)
    assert client.delete("/v1/workspaces/gh-20/members/gh-2", headers=owner).status_code == 200
    fresh = login(client, provider, database, 2)
    assert client.get("/v1/workspaces/gh-20/repos", headers=learner).status_code == 404
    assert client.get("/v1/workspaces/gh-20/repos", headers=fresh).status_code == 404
    with Session(database) as db:
        assert db.get(Membership, ("gh-20", "gh-2")).active is False


def test_delegation_cannot_change_subject_or_survive_parent_revocation(runtime):
    client, provider, database, access = runtime
    owner = login(client, provider, database)
    second = login(client, provider, database, 2)
    response = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    )
    agent_token = response.json()["token"]
    with Session(database) as db, db.begin():
        db.get(Credential, digest(agent_token)).user_id = "gh-2"
    assert (
        client.get("/v1/auth/me", headers={"Authorization": "Bearer " + agent_token}).status_code
        == 401
    )
    response = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    )
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    assert client.post("/v1/auth/logout", headers=owner).status_code == 200
    assert client.get("/v1/auth/me", headers=headers).status_code == 401
    assert client.get("/v1/auth/me", headers=second).status_code == 200


def test_repository_webhook_delivery_cannot_be_reused_to_restore_access(runtime):
    client, provider, database, access = runtime
    owner = login(client, provider, database)
    removed = {"installation": {"id": 10}, "repositories_removed": [{"id": 101}]}
    assert webhook(client, removed, delivery="immutable-delivery").status_code == 200
    added = {
        "installation": {"id": 10},
        "repositories_added": [{"id": 101, "full_name": "user1/code"}],
    }
    assert webhook(client, added, delivery="immutable-delivery").status_code == 409
    assert client.get("/v1/repos/gh-101/head", headers=owner).status_code == 404


def test_former_org_member_can_accept_scoped_outside_collaborator_invitation(runtime):
    client, provider, database, access = runtime
    provider.org_owner = True
    owner = login(client, provider, database)
    provider.org_owner = False
    learner = login(client, provider, database, 2)
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 200
    provider.removed_org_users.add("2")
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 404
    assert (
        client.post(
            "/v1/workspaces/gh-20/invitations",
            headers=owner,
            json={"github_id": "2", "repository_ids": ["gh-202"]},
        ).status_code
        == 200
    )
    fresh = login(client, provider, database, 2)
    assert client.get("/v1/repos/gh-202/head", headers=fresh).status_code == 200
    assert client.get("/v1/repos/gh-203/head", headers=fresh).status_code == 404
    assert [
        repo["id"] for repo in client.get("/v1/workspaces/gh-20/repos", headers=fresh).json()
    ] == ["gh-202"]


def test_reinvitation_cannot_reactivate_local_suspension(runtime):
    client, provider, database, access = runtime
    provider.org_member = False
    owner = login(client, provider, database)
    outside = login(client, provider, database, 2)
    invitation = {"github_id": "2", "repository_ids": ["gh-101"]}
    assert (
        client.post("/v1/workspaces/gh-1/invitations", headers=owner, json=invitation).status_code
        == 200
    )
    assert client.get("/v1/repos/gh-101/head", headers=outside).status_code == 200
    assert client.delete("/v1/workspaces/gh-1/members/gh-2", headers=owner).status_code == 200
    assert (
        client.post("/v1/workspaces/gh-1/invitations", headers=owner, json=invitation).status_code
        == 200
    )
    refreshed = login(client, provider, database, 2)
    assert client.get("/v1/repos/gh-101/head", headers=refreshed).status_code == 404
