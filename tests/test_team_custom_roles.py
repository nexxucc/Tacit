"""Team/custom-role boundaries with fixture GitHub; no external writes/provider billing."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tacit_api.access import AccessService
from tacit_api.app import Settings, create_app
from tacit_api.db import (
    CustomRoleAssignment,
    Membership,
    Repository,
    RoleAssignment,
    TeamRoleMapping,
)
from tacit_api.github import GitHub
from tacit_api.publication import PublicationService
from tacit_domain.access import DomainError
from test_access_management import Provider, login
from test_access_management import engine as engine
from test_publication import draft, publish

BASE = "/v1/workspaces/gh-20"
READ = ["knowledge.read"]
WRITE = ["knowledge.read", "knowledge.write"]


class TeamProvider(Provider):
    def __init__(self):
        super().__init__()
        self.team_id = 700
        self.slug = "writers"
        self.team_state = {1: "active", 2: "active"}
        self.team_error = None
        self.team_org = 20
        self.identity_wrong = False
        self.team_calls = []
        self.malformed = False

    def request(self, request):
        path = request.url.path
        if path == "/user" and self.identity_wrong:
            return httpx.Response(200, json={"id": 999, "login": "imposter"})
        if path == f"/orgs/team/teams/{self.slug}":
            return httpx.Response(
                200,
                json={
                    "id": self.team_id,
                    "slug": self.slug,
                    "organization": {"id": self.team_org, "login": "team"},
                },
            )
        if path.startswith("/organizations/"):
            self.team_calls.append(path)
            if self.team_error:
                return httpx.Response(self.team_error)
            if self.malformed:
                return httpx.Response(200, json={"state": "active"})
            org, team, login_name = path.split("/")[2], path.split("/")[4], path.rsplit("/", 1)[1]
            assert org == "20"
            if team != str(self.team_id):
                return httpx.Response(404)
            uid = int(request.headers["Authorization"].split("user-")[-1])
            assert login_name == f"user{uid}", "Membership lookup changed the credential subject"
            state = self.team_state.get(uid)
            return (
                httpx.Response(404)
                if state is None
                else httpx.Response(200, json={"state": state, "role": "member"})
            )
        if path.startswith("/orgs/team/teams/"):
            return httpx.Response(404)
        return super().request(request)


@pytest.fixture
def setup(engine):
    provider = TeamProvider()
    key = Fernet.generate_key().decode()
    github = GitHub("fixture", 100, httpx.MockTransport(provider.request))
    app = create_app(
        Settings(
            github_client_id="fixture",
            github_app_id=100,
            credential_encryption_key=key,
            access_read_cache_seconds=60,
        ),
        github=github,
        database=engine,
    )
    with TestClient(app) as client:
        provider.org_owner = True
        owner = login(client, provider, engine, uid=1)
        provider.org_owner = False
        learner = login(client, provider, engine, uid=2)
        yield client, provider, engine, owner, learner, AccessService(engine, github, key)


def put_role(setup, permissions=WRITE, *, role_id="reviewers", version=0, workspace="gh-20"):
    return setup[0].put(
        f"/v1/workspaces/{workspace}/custom-roles/{role_id}",
        headers=setup[3],
        json={
            "permissions": permissions,
            "description": "Fixture role",
            "expected_version": version,
        },
    )


def assign(
    setup, *, revoke=False, role_id="reviewers", user="gh-2", repo="gh-202", workspace="gh-20"
):
    return setup[0].post(
        f"/v1/workspaces/{workspace}/custom-role-assignments" + ("/revoke" if revoke else ""),
        headers=setup[3],
        json={"user_id": user, "custom_role_id": role_id, "repository_id": repo},
    )


def mapping(setup, **kwargs):
    body = {"team_slug": setup[1].slug, "role": "maintainer", "repository_id": "gh-202", **kwargs}
    return setup[0].post(BASE + "/team-role-mappings", headers=setup[3], json=body)


def effective(setup, headers=None):
    response = setup[0].get(BASE + "/effective-access", headers=headers or setup[4])
    assert response.status_code == 200, response.text
    return response.json()["grants"]


def blocked_teaching(setup, headers=None):
    response = setup[0].post(
        "/v1/repos/gh-202/teaching-sessions", headers=headers or setup[4], json={}
    )
    assert response.status_code == 403, response.text


def test_custom_writer_can_teach_publish_and_revocation_takes_effect(setup):
    client, provider, db, owner, learner, _ = setup
    assert put_role(setup).status_code == 200
    assert assign(setup).status_code == 200
    assert any(g["role"] == "maintainer" and g["source"] == "custom_role" for g in effective(setup))
    view = draft(client, learner, repo="gh-202")
    result = publish(client, learner, view)
    assert result.status_code == 200, result.text
    assert assign(setup, revoke=True).status_code == 200
    blocked_teaching(setup)
    assert not any(g["source"] == "custom_role" for g in effective(setup))
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 200


def test_custom_reader_works_without_a_builtin_content_role_and_cannot_write(setup):
    client, _, db, _, learner, _ = setup
    with Session(db) as session, session.begin():
        session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.workspace_id == "gh-20", RoleAssignment.user_id == "gh-2"
            )
        )
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 403
    assert put_role(setup, READ).status_code == 200
    assert assign(setup).status_code == 200
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 200
    blocked_teaching(setup)
    for path in ("voice-sessions", "teaching-runs", "publications"):
        assert client.post(f"/v1/repos/gh-202/{path}", headers=learner, json={}).status_code == 403


def test_role_versions_deletion_and_retired_ids_prevent_stale_updates(setup):
    client, _, _, owner, learner, _ = setup
    assert put_role(setup).json()["version"] == 1
    assert put_role(setup, READ).status_code == 409
    assert assign(setup).status_code == 200
    view = draft(client, learner, repo="gh-202")
    assert put_role(setup, READ, version=1).json()["version"] == 2
    assert publish(client, learner, view).status_code == 403
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=2", headers=owner).status_code == 409
    )
    assert assign(setup, revoke=True).status_code == 200
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=1", headers=owner).status_code == 409
    )
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=2", headers=owner).status_code == 200
    )
    assert client.get(BASE + "/custom-roles", headers=owner).json() == []
    assert put_role(setup).status_code == 409
    assert assign(setup).status_code == 404


@pytest.mark.parametrize(
    "permissions",
    [
        [],
        ["workspace.admin"],
        ["knowledge.write"],
        ["knowledge.read", "knowledge.read"],
        ["maintainer"],
    ],
)
def test_invalid_custom_permissions_never_grant_admin(setup, permissions):
    assert put_role(setup, permissions).status_code == 422


@pytest.mark.parametrize("role_id", ["workspace_admin", "maintainer", "learner", "X-role"])
def test_reserved_or_invalid_role_ids_are_rejected(setup, role_id):
    assert put_role(setup, role_id=role_id).status_code == 422


def test_team_mapping_grants_only_active_members_and_does_not_materialize_user_roles(setup):
    client, provider, db, owner, learner, _ = setup
    response = mapping(setup)
    assert response.status_code == 200, response.text
    assert response.json()["github_team_id"] == "700"
    assert any(g["source"] == "github_team" and g["role"] == "maintainer" for g in effective(setup))
    view = draft(client, learner, repo="gh-202")
    assert publish(client, learner, view).status_code == 200
    with Session(db) as session:
        assert not session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.user_id == "gh-2", RoleAssignment.role == "maintainer"
            )
        )
    provider.team_state[2] = None
    # The eligibility cache remains warm; mapped team membership is still checked fresh.
    blocked_teaching(setup)
    assert not any(g["source"] == "github_team" for g in effective(setup))
    assert client.get("/v1/repos/gh-202/head", headers=learner).status_code == 200
    provider.team_state[2] = "pending"
    blocked_teaching(setup)
    provider.team_state[2] = "active"
    assert any(g["source"] == "github_team" for g in effective(setup))
    assert (
        client.delete(
            BASE + "/team-role-mappings/" + response.json()["id"], headers=owner
        ).status_code
        == 200
    )
    blocked_teaching(setup)


def test_numeric_team_identity_survives_rename_but_not_slug_recreation(setup):
    assert mapping(setup).status_code == 200
    setup[1].slug = "renamed-writers"
    assert any(g["source"] == "github_team" for g in effective(setup))
    setup[1].team_id = 701
    blocked_teaching(setup)
    response = mapping(setup)
    assert response.status_code == 200, response.text
    assert response.json()["github_team_id"] == "701"
    assert any(g["source"] == "github_team" for g in effective(setup))


def test_team_custom_role_updates_and_mapping_removal_are_immediate(setup):
    client, _, _, owner, learner, _ = setup
    assert put_role(setup).status_code == 200
    response = mapping(setup, role=None, custom_role_id="reviewers")
    assert response.status_code == 200
    assert (
        mapping(setup, role=None, custom_role_id="reviewers").json()["id"] == response.json()["id"]
    )
    view = draft(client, learner, repo="gh-202")
    assert put_role(setup, READ, version=1).status_code == 200
    assert publish(client, learner, view).status_code == 403
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=2", headers=owner).status_code == 409
    )
    assert (
        client.delete(
            BASE + "/team-role-mappings/" + response.json()["id"], headers=owner
        ).status_code
        == 200
    )
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=2", headers=owner).status_code == 200
    )


@pytest.mark.parametrize("status", [403, 429, 500])
def test_team_provider_failure_fails_closed(setup, status):
    assert mapping(setup).status_code == 200
    assert any(g["source"] == "github_team" for g in effective(setup))
    setup[1].team_error = status
    response = setup[0].get("/v1/repos/gh-202/head", headers=setup[4])
    assert response.status_code == 503


def test_malformed_membership_and_wrong_identity_fail_closed(setup):
    assert mapping(setup).status_code == 200
    setup[1].malformed = True
    assert setup[0].get(BASE + "/effective-access", headers=setup[4]).status_code == 503
    setup[1].malformed = False
    setup[1].identity_wrong = True
    assert setup[0].get(BASE + "/effective-access", headers=setup[4]).status_code == 401


def test_direct_role_survives_team_removal_and_grants_are_deduplicated_per_team_lookup(setup):
    client, provider, _, owner, learner, _ = setup
    assert mapping(setup).status_code == 200
    assert mapping(setup, role="learner", repository_id=None).status_code == 200
    provider.team_calls.clear()
    effective(setup)
    assert len(provider.team_calls) == 1
    assert (
        client.post(
            BASE + "/roles",
            headers=owner,
            json={"user_id": "gh-2", "role": "maintainer", "repository_id": "gh-202"},
        ).status_code
        == 200
    )
    provider.team_state[2] = None
    view = draft(client, learner, repo="gh-202")
    assert view["author_id"] == "gh-2"


def test_no_team_calls_when_workspace_has_no_mapping(setup):
    effective(setup)
    assert setup[1].team_calls == []


def test_team_mapping_cannot_cross_org_repo_or_personal_workspace(setup):
    client, provider, _, owner, _, _ = setup
    provider.team_org = 99
    assert mapping(setup).status_code == 404
    provider.team_org = 20
    assert mapping(setup, repository_id="gh-101").status_code == 404
    assert (
        client.post(
            "/v1/workspaces/gh-1/team-role-mappings",
            headers=owner,
            json={"team_slug": "writers", "role": "maintainer"},
        ).status_code
        == 409
    )
    assert mapping(setup, role="workspace_admin").status_code == 422
    assert mapping(setup, role="maintainer", custom_role_id="reviewers").status_code == 422
    assert mapping(setup, role=None).status_code == 422


def test_new_configuration_endpoints_reject_learners_and_agents(setup):
    client, _, _, owner, learner, _ = setup
    assert put_role(setup).status_code == 200
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-202"]}
    ).json()["token"]
    for headers in (learner, {"Authorization": "Bearer " + agent}):
        assert client.get(BASE + "/custom-roles", headers=headers).status_code == 403
        assert client.get(BASE + "/custom-role-assignments", headers=headers).status_code == 403
        assert client.get(BASE + "/team-role-mappings", headers=headers).status_code == 403
        assert (
            client.put(
                BASE + "/custom-roles/editor",
                headers=headers,
                json={"permissions": WRITE, "expected_version": 0},
            ).status_code
            == 403
        )
        assert (
            client.post(
                BASE + "/team-role-mappings",
                headers=headers,
                json={"team_slug": "writers", "role": "maintainer"},
            ).status_code
            == 403
        )


def test_agent_with_custom_or_team_maintainer_remains_read_only_and_repo_scoped(setup):
    client, _, _, _, learner, _ = setup
    assert put_role(setup).status_code == 200
    assert assign(setup).status_code == 200
    assert mapping(setup).status_code == 200
    token = client.post(
        "/v1/auth/agent-tokens", headers=learner, json={"repository_ids": ["gh-202"]}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/v1/repos/gh-202/head", headers=headers).status_code == 200
    assert client.get("/v1/repos/gh-101/head", headers=headers).status_code == 404
    blocked_teaching(setup, headers)


def test_revoke_cleanup_works_after_member_suspension_and_repository_disable(setup):
    client, _, db, owner, _, _ = setup
    assert put_role(setup).status_code == 200
    assert assign(setup).status_code == 200
    mapped = mapping(setup).json()
    with Session(db) as session, session.begin():
        session.get(Membership, ("gh-20", "gh-2")).active = False
        session.get(Repository, ("gh-20", "gh-202")).enabled = False
    assert assign(setup, revoke=True).status_code == 200
    assert (
        client.delete(BASE + "/team-role-mappings/" + mapped["id"], headers=owner).status_code
        == 200
    )
    assert (
        client.delete(BASE + "/custom-roles/reviewers?version=1", headers=owner).status_code == 200
    )


def test_final_publication_check_reloads_policy_without_repeating_team_http(setup):
    client, provider, db, owner, learner, access = setup
    assert put_role(setup).status_code == 200
    assert assign(setup).status_code == 200
    mapped = mapping(setup).json()
    token = learner["Authorization"][7:]
    stale = access.principal(token)
    assert assign(setup, revoke=True).status_code == 200
    assert (
        client.delete(BASE + "/team-role-mappings/" + mapped["id"], headers=owner).status_code
        == 200
    )
    provider.team_calls.clear()
    with Session(db) as session, session.begin(), pytest.raises(DomainError):
        PublicationService(access).write_repository(session, stale, "gh-202", token)
    assert provider.team_calls == []


def test_final_check_sees_definition_downgrade_under_workspace_lock(setup):
    assert put_role(setup).status_code == 200
    assert assign(setup).status_code == 200
    token = setup[4]["Authorization"][7:]
    stale = setup[5].principal(token)
    assert put_role(setup, READ, version=1).status_code == 200
    with Session(setup[2]) as session, session.begin(), pytest.raises(DomainError):
        PublicationService(setup[5]).write_repository(session, stale, "gh-202", token)


def test_last_admin_cannot_be_replaced_by_custom_or_team_roles(setup):
    assert put_role(setup).status_code == 200
    assert assign(setup, user="gh-1").status_code == 200
    assert mapping(setup).status_code == 200
    result = setup[0].post(
        BASE + "/roles/revoke",
        headers=setup[3],
        json={"user_id": "gh-1", "role": "workspace_admin"},
    )
    assert result.status_code == 409


def test_concurrent_role_updates_and_duplicate_mappings_are_serialized(setup):
    if setup[2].dialect.name != "postgresql":
        pytest.skip("Workspace locking requires PostgreSQL")
    assert put_role(setup).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: put_role(setup, READ, version=1).status_code, range(2)))
    assert sorted(results) == [200, 409]
    with ThreadPoolExecutor(max_workers=2) as pool:
        mappings = list(pool.map(lambda _: mapping(setup), range(2)))
    assert all(r.status_code == 200 for r in mappings)
    assert mappings[0].json()["id"] == mappings[1].json()["id"]
    with Session(setup[2]) as db:
        assert db.scalar(select(func.count()).select_from(TeamRoleMapping)) == 1


def test_durable_voice_uses_team_role_then_stops_after_local_mapping_removal(setup):
    from test_voice_pipeline import start

    client, _, db, owner, learner, _ = setup
    if db.dialect.name != "postgresql":
        pytest.skip("Concurrent socket/API transactions require PostgreSQL")
    mapped = mapping(setup)
    assert mapped.status_code == 200
    view, _ = start(client, learner, repo="gh-202")
    route = f"/v1/repos/gh-202/voice-sessions/{view['id']}/upload"
    with client.websocket_connect(route, headers=learner) as ws:
        assert ws.receive_json()["status"] == "uploading"
        assert (
            client.delete(
                BASE + "/team-role-mappings/" + mapped.json()["id"], headers=owner
            ).status_code
            == 200
        )
        ws.send_bytes(bytes(8) + b"\0\0" * 16000)
        assert ws.receive_json()["code"] == "forbidden"
    from tacit_api.db import VoiceChunk

    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(VoiceChunk)) == 0


@pytest.mark.parametrize("change", ["membership", "repository", "role_downgrade"])
def test_cached_team_grant_never_bypasses_local_state_changes(setup, change):
    assert put_role(setup).status_code == 200
    assert mapping(setup, role=None, custom_role_id="reviewers").status_code == 200
    token = setup[4]["Authorization"][7:]
    stale = setup[5].principal(token)
    if change == "role_downgrade":
        assert put_role(setup, READ, version=1).status_code == 200
    else:
        with Session(setup[2]) as db, db.begin():
            if change == "membership":
                db.get(Membership, ("gh-20", "gh-2")).active = False
            else:
                db.get(Repository, ("gh-20", "gh-202")).enabled = False
    with Session(setup[2]) as db, db.begin(), pytest.raises(DomainError):
        PublicationService(setup[5]).write_repository(db, stale, "gh-202", token)


def test_new_cli_commands_send_explicit_scoped_contracts(monkeypatch):
    from tacit_cli import roles
    from tacit_cli.main import app
    from typer.testing import CliRunner

    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"status": "fixture"}

    monkeypatch.setattr(roles, "TacitClient", Client)
    runner = CliRunner()
    cases = [
        [
            "custom-roles",
            "put",
            "reviewers",
            "--workspace",
            "gh-20",
            "--permission",
            "knowledge.read",
            "--permission",
            "knowledge.write",
            "--expected-version",
            "0",
        ],
        ["custom-roles", "grant", "gh-2", "reviewers", "--workspace", "gh-20", "--repo", "gh-202"],
        ["custom-roles", "revoke", "gh-2", "reviewers", "--workspace", "gh-20", "--repo", "gh-202"],
        ["custom-roles", "list", "--workspace", "gh-20"],
        ["custom-roles", "assignments", "--workspace", "gh-20"],
        ["custom-roles", "delete", "reviewers", "--workspace", "gh-20", "--expected-version", "1"],
        [
            "team-roles",
            "add",
            "writers",
            "--workspace",
            "gh-20",
            "--custom-role",
            "reviewers",
            "--repo",
            "gh-202",
        ],
        ["team-roles", "list", "--workspace", "gh-20"],
        ["team-roles", "remove", "mapping-id", "--workspace", "gh-20"],
        ["team-roles", "effective", "--workspace", "gh-20"],
    ]
    for args in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert len(calls) == len(cases)
    assert calls[0][2]["json"]["expected_version"] == 0
    assert calls[1][2]["json"]["repository_id"] == "gh-202"
    assert calls[2][1].endswith("/revoke")
    assert calls[6][2]["json"]["custom_role_id"] == "reviewers"
    assert calls[6][2]["json"]["role"] is None
    count = len(calls)
    for args in [
        ["team-roles", "add", "writers", "--workspace", "gh-20", "--role", "workspace_admin"],
        [
            "custom-roles",
            "put",
            "bad",
            "--workspace",
            "gh-20",
            "--permission",
            "knowledge.write",
            "--expected-version",
            "0",
        ],
    ]:
        assert runner.invoke(app, args).exit_code != 0
    assert len(calls) == count


def test_schema_constraints_reject_cross_workspace_custom_assignments_and_admin_mapping(setup):
    assert put_role(setup).status_code == 200
    with Session(setup[2]) as db, pytest.raises(IntegrityError):
        with db.begin():
            db.add(
                CustomRoleAssignment(
                    id="cross",
                    workspace_id="gh-1",
                    user_id="gh-1",
                    custom_role_id="reviewers",
                    repository_id="gh-101",
                )
            )
            db.flush()
    with Session(setup[2]) as db, pytest.raises(IntegrityError):
        with db.begin():
            db.add(
                TeamRoleMapping(
                    id="admin",
                    workspace_id="gh-20",
                    github_team_id="700",
                    team_slug="writers",
                    role="workspace_admin",
                    custom_role_id=None,
                )
            )
            db.flush()


def test_builtin_admin_can_remove_failing_mapping_without_team_derived_authority(setup):
    mapped = mapping(setup)
    assert mapped.status_code == 200
    setup[1].team_error = 403
    assert setup[0].get("/v1/repos/gh-202/head", headers=setup[4]).status_code == 503
    assert setup[0].get(BASE + "/team-role-mappings", headers=setup[4]).status_code == 403
    response = setup[0].delete(
        BASE + "/team-role-mappings/" + mapped.json()["id"], headers=setup[3]
    )
    assert response.status_code == 200, response.text
    assert setup[0].get("/v1/repos/gh-202/head", headers=setup[4]).status_code == 200


def test_admin_can_remove_excess_mappings_without_team_verification_budget(monkeypatch, setup):
    from tacit_api import roles

    assert mapping(setup).status_code == 200
    setup[1].team_id = 701
    extra = mapping(setup)
    assert extra.status_code == 200
    monkeypatch.setattr(roles, "MAX_TEAM_CHECKS", 1)
    assert setup[0].get("/v1/repos/gh-202/head", headers=setup[4]).status_code == 503
    response = setup[0].delete(BASE + "/team-role-mappings/" + extra.json()["id"], headers=setup[3])
    assert response.status_code == 200
