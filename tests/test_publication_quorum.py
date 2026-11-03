"""Adversarial quorum checks with synthetic humans/GitHub and real transactions."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from tacit_api.access import digest, now
from tacit_api.db import AuditEvent, Credential, DraftApproval, RoleAssignment
from test_access_management import engine as engine
from test_access_management import login
from test_access_management import setup as setup
from test_publication import draft, publish

RID = "gh-202"


@pytest.fixture
def humans(setup):
    client, provider, database = setup
    provider.org_owner = True
    owner = login(client, provider, database)
    provider.org_owner = False
    reviewer = login(client, provider, database, 2)
    learner = login(client, provider, database, 3)
    with Session(database) as db, db.begin():
        for uid in ("gh-1", "gh-2"):
            db.add(
                RoleAssignment(
                    id=str(uuid4()),
                    workspace_id="gh-20",
                    user_id=uid,
                    repository_id=RID,
                    role="maintainer",
                )
            )
    return client, provider, database, owner, reviewer, learner


def policy(client, owner, required=2, version=0):
    return client.put(
        f"/v1/repos/{RID}/publication-policy",
        headers=owner,
        json={"required_maintainers": required, "expected_version": version},
    )


def approve(client, headers, view, version=1, **changes):
    return client.post(
        f"/v1/repos/{RID}/teaching-sessions/{view['id']}/approvals",
        headers=headers,
        json={
            "review_digest": view["review_digest"],
            "expected_snapshot_id": view["expected_snapshot_id"],
            "policy_version": version,
            **changes,
        },
    )


def approvals_path(view):
    return f"/v1/repos/{RID}/teaching-sessions/{view['id']}/approvals"


def test_default_compatibility_and_two_distinct_humans(humans):
    client, _, database, owner, reviewer, _ = humans
    assert client.get(f"/v1/repos/{RID}/publication-policy", headers=owner).json()["version"] == 0
    assert policy(client, owner).status_code == 200
    view = draft(client, owner, repo=RID)
    assert "quorum_required" in publish(client, owner, view).text
    # Self-approval and repeated requests can never count as a second person.
    assert approve(client, owner, view).status_code == 200
    assert approve(client, owner, view).status_code == 200
    assert publish(client, owner, view).status_code == 409
    assert approve(client, reviewer, view).status_code == 200
    operation = str(uuid4())
    result = publish(client, owner, view, operation=operation)
    assert result.status_code == 200, result.text
    assert publish(client, owner, view, operation=operation).json() == result.json()
    with Session(database) as db:
        audit = db.scalar(
            select(AuditEvent).where(AuditEvent.action == "publication.quorum_satisfied")
        )
        assert audit.details["approvers"] == ["gh-1", "gh-2"]
    assert approve(client, reviewer, view).status_code == 409
    assert client.delete(approvals_path(view), headers=reviewer).status_code == 409


def test_approver_can_confirm_but_not_double_count(humans):
    client, _, _, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    assert publish(client, reviewer, view).status_code == 409
    approve(client, owner, view)
    assert publish(client, reviewer, view).status_code == 200


def test_withdrawal_and_policy_version_invalidate_votes(humans):
    client, _, _, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    assert client.delete(approvals_path(view), headers=reviewer).status_code == 200
    assert publish(client, owner, view).status_code == 409
    approve(client, reviewer, view)
    assert policy(client, owner, version=1).json()["version"] == 2
    assert publish(client, owner, view).status_code == 409
    assert approve(client, reviewer, view).status_code == 409
    assert approve(client, reviewer, view, version=2).status_code == 200
    assert publish(client, owner, view).status_code == 200


@pytest.mark.parametrize(
    "change", ["credential_revocation", "credential_expiry", "role_removal", "github_removal"]
)
def test_current_reviewer_authority_required(humans, change, monkeypatch):
    client, provider, database, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    with Session(database) as db, db.begin():
        credential = db.get(Credential, digest(reviewer["Authorization"].split()[1]))
        if change == "credential_revocation":
            credential.revoked = True
        elif change == "credential_expiry":
            credential.expires_at = now() - timedelta(seconds=1)
        elif change == "role_removal":
            db.execute(
                delete(RoleAssignment).where(
                    RoleAssignment.user_id == "gh-2", RoleAssignment.role == "maintainer"
                )
            )
    if change == "github_removal":
        from tacit_api.github import GitHub

        original = GitHub.eligibility

        def eligibility(self, token):
            return () if token == "user-2" else original(self, token)

        monkeypatch.setattr(GitHub, "eligibility", eligibility)
    result = publish(client, owner, view)
    assert result.status_code == 409, result.text


def test_exact_content_scope_agents_and_learners(humans):
    client, _, _, owner, reviewer, learner = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    assert approve(client, reviewer, view, review_digest="0" * 64).status_code == 409
    assert approve(client, reviewer, view, expected_snapshot_id="wrong").status_code == 409
    assert approve(client, learner, view).status_code == 403
    agent = client.post("/v1/auth/agent-tokens", headers=reviewer, json={"repository_ids": [RID]})
    assert agent.status_code == 200, agent.text
    agent_headers = {"Authorization": "Bearer " + agent.json()["token"]}
    assert approve(client, agent_headers, view).status_code == 403
    assert policy(client, reviewer).status_code == 403
    assert (
        client.get(
            f"/v1/repos/gh-101/teaching-sessions/{view['id']}/approvals", headers=owner
        ).status_code
        == 404
    )
    assert approve(client, reviewer, view).status_code == 200
    second = draft(client, owner, repo=RID, statement="A different statement")
    assert publish(client, owner, second).status_code == 409


@pytest.mark.parametrize("required", [0, 6, True, "2"])
def test_policy_contract_and_cas(humans, required):
    client, _, _, owner, _, _ = humans
    assert policy(client, owner, required=required).status_code == 422
    assert policy(client, owner).status_code == 200
    assert policy(client, owner).status_code == 409


def test_base_head_change_invalidates_approval(humans):
    client, _, _, owner, reviewer, _ = humans
    view = draft(client, owner, repo=RID)
    newer = draft(client, owner, repo=RID, statement="New head")
    assert publish(client, owner, newer).status_code == 200
    policy(client, owner)
    assert approve(client, reviewer, view).status_code == 409
    assert publish(client, owner, view).status_code == 409


@pytest.mark.parametrize("mutation", ["withdraw", "policy", "role", "credential"])
def test_recheck_under_publication_lock(humans, monkeypatch, mutation):
    from tacit_api.quorum import Quorum

    client, _, database, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    original = Quorum.preflight

    def changed(self, *args):
        proofs = original(self, *args)
        if mutation == "withdraw":
            assert client.delete(approvals_path(view), headers=reviewer).status_code == 200
        elif mutation == "policy":
            assert policy(client, owner, required=3, version=1).status_code == 200
        else:
            with Session(database) as db, db.begin():
                if mutation == "role":
                    db.execute(
                        delete(RoleAssignment).where(
                            RoleAssignment.user_id == "gh-2", RoleAssignment.role == "maintainer"
                        )
                    )
                else:
                    credential = db.get(Credential, digest(reviewer["Authorization"].split()[1]))
                    credential.revoked = True
        return proofs

    monkeypatch.setattr(Quorum, "preflight", changed)
    assert publish(client, owner, view).status_code == 409


def test_three_humans_and_policy_lowering(humans):
    client, _, database, owner, reviewer, third = humans
    with Session(database) as db, db.begin():
        db.add(
            RoleAssignment(
                id=str(uuid4()),
                workspace_id="gh-20",
                user_id="gh-3",
                repository_id=RID,
                role="maintainer",
            )
        )
    assert policy(client, owner, required=3).status_code == 200
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    assert publish(client, owner, view).status_code == 409
    approve(client, third, view)
    assert publish(client, owner, view).status_code == 200
    # Explicit admin policy relaxation is allowed, never an implicit publication bypass.
    assert policy(client, owner, required=1, version=1).status_code == 200
    head = client.get(f"/v1/repos/{RID}/head", headers=owner).json()["snapshot_id"]
    next_view = draft(client, owner, repo=RID, parent=head)
    assert publish(client, owner, next_view).status_code == 200


def test_approval_status_never_exposes_credential_hash(humans):
    client, _, database, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    result = approve(client, reviewer, view)
    assert "credential" not in result.text
    assert "Recorded approvals only" in result.json()["note"]
    with Session(database) as db:
        approvals = list(db.scalars(select(DraftApproval)))
        assert len(approvals) == 1
        assert approvals[0].credential_hash == digest(reviewer["Authorization"].split()[1])


def test_concurrent_confirmation_publishes_once(humans):
    from concurrent.futures import ThreadPoolExecutor

    from test_publication import counts

    client, _, database, owner, reviewer, _ = humans
    if database.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL workspace serialization")
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    approve(client, owner, view)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda headers: publish(client, headers, view).status_code, [owner, reviewer])
        )
    assert sorted(results) == [200, 409]
    with Session(database) as db:
        assert counts(db)["publication_receipts"] == 1


def test_custom_role_downgrade_invalidates_approval(humans):
    from tacit_api.db import CustomRole, CustomRoleAssignment

    client, _, database, owner, reviewer, _ = humans
    with Session(database) as db, db.begin():
        db.execute(
            delete(RoleAssignment).where(
                RoleAssignment.user_id == "gh-2", RoleAssignment.role == "maintainer"
            )
        )
        db.add(
            CustomRole(
                workspace_id="gh-20",
                id="reviewer",
                description="Fixture reviewer",
                permissions=["knowledge.read", "knowledge.write"],
                version=1,
                active=True,
            )
        )
        db.flush()
        db.add(
            CustomRoleAssignment(
                id=str(uuid4()),
                workspace_id="gh-20",
                custom_role_id="reviewer",
                repository_id=RID,
                user_id="gh-2",
            )
        )
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    assert approve(client, reviewer, view).status_code == 200
    with Session(database) as db, db.begin():
        role = db.get(CustomRole, ("gh-20", "reviewer"))
        role.permissions = ["knowledge.read"]
        role.version += 1
    assert publish(client, owner, view).status_code == 409


def test_provider_failure_is_closed_and_published_retry_skips_revalidation(humans, monkeypatch):
    from tacit_api.access import AccessService
    from tacit_domain.access import DomainError

    client, _, _, owner, reviewer, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    approve(client, reviewer, view)
    original = AccessService.principal_hash
    reviewer_hash = digest(reviewer["Authorization"].split()[1])
    unavailable = True
    calls = []

    def check(self, token_hash, **kwargs):
        if token_hash == reviewer_hash:
            calls.append(token_hash)
            if unavailable:
                raise DomainError("service_unavailable", "Fixture provider unavailable", 503)
        return original(self, token_hash, **kwargs)

    monkeypatch.setattr(AccessService, "principal_hash", check)
    assert publish(client, owner, view).status_code == 503
    unavailable = False
    operation = str(uuid4())
    result = publish(client, owner, view, operation=operation)
    assert result.status_code == 200
    calls.clear()
    unavailable = True
    assert publish(client, owner, view, operation=operation).json() == result.json()
    assert calls == []


def test_cli_approval_requires_review_confirmation(humans, monkeypatch):
    from tacit_cli.quorum import app as commands
    from typer.testing import CliRunner

    client, _, _, owner, _, _ = humans
    policy(client, owner)
    view = draft(client, owner, repo=RID)
    sent = []

    class LocalClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            sent.append(method)
            response = client.request(method, path, headers=owner, **kwargs)
            assert response.status_code == 200, response.text
            return response.json()

    monkeypatch.setattr("tacit_cli.quorum.TacitClient", LocalClient)
    result = CliRunner().invoke(commands, ["approve", view["id"], "--repo", RID], input="n\n")
    assert result.exit_code == 0, result.output
    assert sent == ["GET", "GET"]
    sent.clear()
    result = CliRunner().invoke(commands, ["approve", view["id"], "--repo", RID], input="y\n")
    assert result.exit_code == 0, result.output
    assert sent == ["GET", "GET", "POST"]
