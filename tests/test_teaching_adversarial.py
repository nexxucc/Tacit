"""Adversarial teaching state and grounding checks using fixture Evon only."""

import asyncio
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_api.db import TeachingDraft
from tacit_api.publication import PublicationService
from tacit_api.reasoning import Evon
from tacit_contracts.models import CandidateSet, Usage
from test_access_management import engine as engine
from test_evon import COMMIT, TEXT
from test_evon_review import backend as backend
from test_evon_review import clarify


def start(client, owner, **changes):
    body = {
        "operation_id": str(uuid4()),
        "source_commit": COMMIT,
        "files": ["worker.py"],
        "statement": TEXT,
        "expected_snapshot_id": None,
        **changes,
    }
    return client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body), body


def candidate(identity="candidate", **changes):
    return {
        "id": identity,
        "statement": TEXT,
        "transcript_quote": TEXT,
        "citations": [{"path": "worker.py", "blob_sha": "b" * 40, "start_line": 1, "end_line": 2}],
        **changes,
    }


def model_result(monkeypatch, candidates):
    calls = []

    async def structured(self, instruction, data, schema):
        calls.append(data)
        return CandidateSet.model_validate({"candidates": candidates}), Usage(reasoning_calls=1)

    monkeypatch.setattr(Evon, "structured", structured)
    return calls


def test_cancel_during_preparation_never_leaves_a_confirmable_draft(backend, monkeypatch):
    client, owner, database, _ = backend
    response, _ = start(client, owner)
    ready = clarify(client, owner, response.json())
    create = PublicationService.create

    def cancel_then_create(*args, **kwargs):
        cancelled = client.delete(f"/v1/repos/gh-101/teaching-runs/{ready['id']}", headers=owner)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        return create(*args, **kwargs)

    monkeypatch.setattr(PublicationService, "create", cancel_then_create)
    result = client.post(
        f"/v1/repos/gh-101/teaching-runs/{ready['id']}/draft",
        headers=owner,
        json={"operation_id": str(uuid4()), "expected_version": ready["version"]},
    )
    assert result.status_code == 409, result.text
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 0


def test_candidate_id_prefix_does_not_require_another_candidates_questions(backend, monkeypatch):
    client, owner, _, _ = backend
    model_result(
        monkeypatch,
        [
            candidate(questions=["Does this apply to all workers?"]),
            candidate("candidate:other", questions=["An unrelated alternative question?"]),
        ],
    )
    response, _ = start(client, owner)
    assert response.status_code == 200, response.text
    run = response.json()
    answered = client.post(
        f"/v1/repos/gh-101/teaching-runs/{run['id']}/answers",
        headers=owner,
        json={
            "operation_id": str(uuid4()),
            "expected_version": run["version"],
            "candidate_id": "candidate",
            "answers": {"candidate:0": "Yes."},
            "accept_assumptions": True,
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "ready_for_review"


def test_citation_cannot_escape_selected_line_range_or_replay_model(backend, monkeypatch):
    client, owner, _, _ = backend
    calls = model_result(monkeypatch, [candidate()])
    selections = [
        {
            "path": "worker.py",
            "start_line": 2,
            "end_line": 2,
            "captured_at": "2026-10-31T00:00:00+00:00",
        }
    ]
    result, body = start(client, owner, selections=selections)
    assert result.status_code == 503, result.text
    retry = client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body)
    assert retry.status_code == 409
    assert len(calls) == 1
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


def test_old_clarification_retry_cannot_restore_withdrawn_assumption_acceptance(backend):
    client, owner, _, _ = backend
    response, _ = start(client, owner)
    initial = response.json()
    path = f"/v1/repos/gh-101/teaching-runs/{initial['id']}/answers"
    body = {
        "operation_id": str(uuid4()),
        "expected_version": initial["version"],
        "candidate_id": "candidate",
        "answers": {},
        "accept_assumptions": True,
    }
    accepted = client.post(path, headers=owner, json=body)
    assert accepted.status_code == 200
    withdrawn = clarify(client, owner, accepted.json(), accept=False)
    retried = client.post(path, headers=owner, json=body)
    assert retried.status_code == 200
    assert retried.json()["version"] == withdrawn["version"]
    assert retried.json()["status"] == "needs_clarification"
    prepared = client.post(
        f"/v1/repos/gh-101/teaching-runs/{initial['id']}/draft",
        headers=owner,
        json={"operation_id": str(uuid4()), "expected_version": withdrawn["version"]},
    )
    assert prepared.status_code == 409


def test_cancel_during_model_call_discards_candidates_without_replay(backend, monkeypatch):
    from tacit_api.db import TeachingRun

    client, owner, database, _ = backend
    calls = []

    async def cancel_then_return(self, instruction, data, schema):
        calls.append(data)
        with Session(database) as db:
            run = db.scalar(select(TeachingRun))
            run_id = run.id
        cancelled = await asyncio.to_thread(
            client.delete, f"/v1/repos/gh-101/teaching-runs/{run_id}", headers=owner
        )
        assert cancelled.status_code == 200
        return CandidateSet.model_validate({"candidates": [candidate()]}), Usage(reasoning_calls=1)

    monkeypatch.setattr(Evon, "structured", cancel_then_return)
    result, body = start(client, owner)
    assert result.status_code == 409, result.text
    retry = client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body)
    assert retry.status_code == 200
    assert retry.json()["status"] == "cancelled" and not retry.json()["candidates"]
    assert len(calls) == 1
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 0


def test_cancellation_after_draft_commit_sees_completed_preparation(backend, monkeypatch):
    client, owner, database, _ = backend
    response, _ = start(client, owner)
    ready = clarify(client, owner, response.json())
    original = PublicationService.create
    cancellations = []

    def create_then_cancel(*args, **kwargs):
        draft = original(*args, **kwargs)
        response = client.delete(f"/v1/repos/gh-101/teaching-runs/{ready['id']}", headers=owner)
        assert response.status_code == 200
        cancellations.append(response.json())
        return draft

    monkeypatch.setattr(PublicationService, "create", create_then_cancel)
    result = client.post(
        f"/v1/repos/gh-101/teaching-runs/{ready['id']}/draft",
        headers=owner,
        json={"operation_id": str(uuid4()), "expected_version": ready["version"]},
    )
    assert result.status_code == 200, result.text
    assert cancellations[0]["status"] == "drafted"
    assert cancellations[0]["draft"]["id"] == result.json()["draft"]["id"]
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 1


def test_lost_preparation_response_recovers_atomic_result_without_second_increment(
    backend, monkeypatch
):
    from tacit_domain.access import DomainError

    client, owner, database, _ = backend
    response, _ = start(client, owner)
    ready = clarify(client, owner, response.json())
    original = PublicationService.create
    committed = []

    def create_then_lose_response(*args, **kwargs):
        draft = original(*args, **kwargs)
        committed.append(draft.id)
        raise DomainError("fixture_response_lost", "Fixture response loss after commit", 503)

    monkeypatch.setattr(PublicationService, "create", create_then_lose_response)
    path = f"/v1/repos/gh-101/teaching-runs/{ready['id']}/draft"
    body = {"operation_id": str(uuid4()), "expected_version": ready["version"]}
    failed = client.post(path, headers=owner, json=body)
    assert failed.status_code == 503
    recovered = client.post(path, headers=owner, json=body)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "drafted"
    assert recovered.json()["version"] == ready["version"] + 1
    assert recovered.json()["draft"]["id"] == committed[0]
    assert len(committed) == 1
    assert client.post(path, headers=owner, json=body).json() == recovered.json()
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 1
