"""Regression coverage for reviewed Evon workflow boundaries, without live inference."""

from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_api.app import Settings, create_app
from tacit_api.db import DecisionRevision, ModelOperation, Snapshot, SnapshotMember
from tacit_api.db import TeachingDraft as Draft
from tacit_api.github import GitHub
from tacit_api.publication import PublicationService
from tacit_api.reasoning import Evon
from tacit_contracts.models import CandidateSet, CitedAnswer, Decision, Evidence, Usage
from tacit_domain.access import DomainError
from test_access_management import engine as engine
from test_access_management import login
from test_evon import COMMIT, TEXT, SourceProvider


@pytest.fixture
def backend(engine, monkeypatch):
    calls = []

    async def structured(self, instruction, data, schema):
        calls.append(data)
        if schema is CitedAnswer:
            return CitedAnswer(
                answer="The evidence conflicts.", revision_ids=("a-restriction", "z-permission")
            ), Usage(reasoning_calls=1)
        return CandidateSet.model_validate(
            {
                "candidates": [
                    {
                        "id": "candidate",
                        "statement": TEXT,
                        "transcript_quote": TEXT,
                        "citations": [
                            {
                                "path": "worker.py",
                                "blob_sha": "b" * 40,
                                "start_line": 1,
                                "end_line": 2,
                            }
                        ],
                        "assumptions": ["This applies to every worker."],
                    }
                ]
            }
        ), Usage(reasoning_calls=1)

    monkeypatch.setattr(Evon, "structured", structured)
    provider = SourceProvider()
    app = create_app(
        Settings(
            github_client_id="fixture",
            github_app_id=100,
            credential_encryption_key=Fernet.generate_key().decode(),
            evon_url="http://localhost/v1",
            evon_api_key="fixture",
        ),
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
    )
    with TestClient(app) as client:
        owner = login(client, provider, engine)
        yield client, owner, engine, calls


def clarify(client, owner, view, accept=True):
    response = client.post(
        f"/v1/repos/gh-101/teaching-runs/{view['id']}/answers",
        headers=owner,
        json={
            "operation_id": str(uuid4()),
            "expected_version": view["version"],
            "candidate_id": "candidate",
            "answers": {},
            "accept_assumptions": accept,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def failed_preparation(backend, monkeypatch):
    client, owner, db, calls = backend
    response = client.post(
        "/v1/repos/gh-101/teaching-runs",
        headers=owner,
        json={
            "operation_id": str(uuid4()),
            "source_commit": COMMIT,
            "files": ["worker.py"],
            "statement": TEXT,
            "expected_snapshot_id": None,
        },
    )
    assert response.status_code == 200, response.text
    ready = clarify(client, owner, response.json())
    assert ready["status"] == "ready_for_review"
    path = f"/v1/repos/gh-101/teaching-runs/{ready['id']}/draft"
    body = {"operation_id": str(uuid4()), "expected_version": ready["version"]}
    create = PublicationService.create

    def unavailable(*args, **kwargs):
        raise DomainError("source_unavailable", "Temporary fixture failure", 503)

    monkeypatch.setattr(PublicationService, "create", unavailable)
    assert client.post(path, headers=owner, json=body).status_code == 503
    monkeypatch.setattr(PublicationService, "create", create)
    assert len(calls) == 1
    return ready, path, body


@pytest.mark.parametrize("withdraw_acceptance", [True, False])
def test_stale_preparation_cannot_bypass_clarification(backend, monkeypatch, withdraw_acceptance):
    client, owner, db, calls = backend
    ready, path, old = failed_preparation(backend, monkeypatch)
    edited = clarify(client, owner, ready, accept=not withdraw_acceptance)
    assert edited["version"] > old["expected_version"]
    assert edited["status"] == (
        "needs_clarification" if withdraw_acceptance else "ready_for_review"
    )
    rejected = client.post(path, headers=owner, json=old)
    assert rejected.status_code == 409 and rejected.json()["code"] == "teaching_conflict"
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(Draft)) == 0
    if withdraw_acceptance:
        # Updating the version alone must not bypass outstanding assumptions.
        assert (
            client.post(
                path,
                headers=owner,
                json={"operation_id": str(uuid4()), "expected_version": edited["version"]},
            ).status_code
            == 409
        )
        edited = clarify(client, owner, edited)
    fresh = {"operation_id": str(uuid4()), "expected_version": edited["version"]}
    result = client.post(path, headers=owner, json=fresh)
    assert result.status_code == 200, result.text
    assert result.json()["draft"]["grounding"]["assumptions_accepted"] is True
    assert client.post(path, headers=owner, json=fresh).json() == result.json()
    assert len(calls) == 1


def test_unchanged_failed_preparation_can_retry_and_completed_result_is_immutable(
    backend, monkeypatch
):
    client, owner, db, calls = backend
    ready, path, body = failed_preparation(backend, monkeypatch)
    result = client.post(path, headers=owner, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["version"] > body["expected_version"]
    assert client.post(path, headers=owner, json=body).json() == result.json()
    assert (
        client.post(path, headers=owner, json={**body, "decision_id": "changed"}).status_code == 409
    )
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(Draft)) == 1


@pytest.mark.parametrize("cursor,limit", [(1, 20), (0, 1)])
def test_synthesis_requires_the_entire_scoped_context(backend, cursor, limit):
    client, owner, db, calls = backend
    with Session(db) as session, session.begin():
        session.add(
            Snapshot(
                workspace_id="gh-1",
                repository_id="gh-101",
                id="conflicts",
                source_commit=COMMIT,
                digest="fixture",
            )
        )
        for rid, statement in [
            ("a-restriction", TEXT),
            ("z-permission", "Workers may publish results."),
        ]:
            decision = Decision(
                id=rid,
                revision_id=rid,
                statement=statement,
                files=("worker.py",),
                source_commit=COMMIT,
                evidence=(Evidence(decision_id=rid, revision_id=rid, segment_id=rid, kind="text"),),
            )
            session.add(
                DecisionRevision(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    id=rid,
                    decision_id=rid,
                    confirmed_by="gh-1",
                    content=decision.model_dump(mode="json"),
                )
            )
        session.flush()
        for rid in ("a-restriction", "z-permission"):
            session.add(
                SnapshotMember(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    snapshot_id="conflicts",
                    revision_id=rid,
                )
            )
    body = {
        "snapshot_id": "conflicts",
        "source_commit": COMMIT,
        "question": "May an expired worker publish?",
        "reason": True,
        "operation_id": str(uuid4()),
        "cursor": cursor,
        "limit": limit,
    }
    result = client.post("/v1/repos/gh-101/questions", headers=owner, json=body)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["answer_kind"] == "none" and data["answer"] is None
    assert data["usage"] == Usage().model_dump()
    assert data["warnings"] and not calls
    if cursor:
        assert data["truncated"] is False
        assert data["decisions"][0]["revision_id"] == "z-permission"
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(ModelOperation)) == 0
    complete = client.post(
        "/v1/repos/gh-101/questions", headers=owner, json={**body, "cursor": 0, "limit": 20}
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["answer_kind"] == "cited_reasoning"
    assert {d["revision_id"] for d in calls[0]["decisions"]} == {"a-restriction", "z-permission"}


def test_inflight_preparation_rejects_duplicate_until_completed(backend, monkeypatch):
    client, owner, db, calls = backend
    ready, path, body = failed_preparation(backend, monkeypatch)
    create = PublicationService.create
    attempts = []

    def probe_duplicate(*args, **kwargs):
        attempts.append(1)
        assert len(attempts) == 1, "An in-flight request must not start another draft creation"
        duplicate = client.post(path, headers=owner, json=body)
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["code"] == "teaching_conflict"
        return create(*args, **kwargs)

    monkeypatch.setattr(PublicationService, "create", probe_duplicate)
    result = client.post(path, headers=owner, json=body)
    assert result.status_code == 200, result.text
    assert client.post(path, headers=owner, json=body).json() == result.json()
    assert len(attempts) == 1
