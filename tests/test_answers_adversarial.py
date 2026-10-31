"""Adversarial exact/cited retrieval with fixture model and isolated databases."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from tacit_api.answers import Answers
from tacit_api.db import DecisionRevision, ModelOperation, Snapshot, SnapshotMember
from tacit_api.persistence import PostgreSQLStore
from tacit_api.providers import ProviderUnavailable
from tacit_api.reasoning import Evon
from tacit_contracts.models import CitedAnswer, Decision, Evidence, QuestionRequest, Role, Usage
from tacit_domain.access import DomainError, Grant, Principal
from tacit_domain.knowledge import KnowledgeService
from test_access_management import engine as engine
from test_evon import COMMIT, TEXT
from test_evon_review import backend as backend


def seed(database, *, large=False):
    with Session(database) as db, db.begin():
        db.add(
            Snapshot(
                workspace_id="gh-1",
                repository_id="gh-101",
                id="answers",
                source_commit=COMMIT,
                digest="fixture",
            )
        )
        for index in range(2 if large else 1):
            revision = f"revision-{index}"
            decision = Decision(
                id=f"decision-{index}",
                revision_id=revision,
                statement=("Evidence " * 5000 if large else TEXT),
                files=("worker.py",),
                source_commit=COMMIT,
                evidence=(
                    Evidence(
                        decision_id=f"decision-{index}",
                        revision_id=revision,
                        segment_id=revision,
                        kind="text",
                    ),
                ),
            )
            db.add(
                DecisionRevision(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    id=revision,
                    decision_id=decision.id,
                    confirmed_by="gh-1",
                    content=decision.model_dump(mode="json"),
                )
            )
            db.flush()
            db.add(
                SnapshotMember(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    snapshot_id="answers",
                    revision_id=revision,
                )
            )


def question(**changes):
    return {
        "snapshot_id": "answers",
        "source_commit": COMMIT,
        "question": "May expired workers publish?",
        "reason": True,
        "operation_id": str(uuid4()),
        **changes,
    }


def count_operations(database):
    with Session(database) as db:
        return db.scalar(select(func.count()).select_from(ModelOperation))


@pytest.mark.parametrize(
    "changes", [{"cursor": 1}, {"source_commit": "c" * 40}, {"files": ["unrelated.py"]}]
)
def test_incomplete_or_unmatched_context_never_reserves_model(backend, changes):
    client, owner, database, calls = backend
    seed(database)
    response = client.post("/v1/repos/gh-101/questions", headers=owner, json=question(**changes))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer_kind"] == "none" and result["answer"] is None
    assert result["usage"] == Usage().model_dump()
    assert not calls and count_operations(database) == 0


def test_character_budget_abstains_before_model_or_operation(backend):
    client, owner, database, calls = backend
    seed(database, large=True)
    response = client.post("/v1/repos/gh-101/questions", headers=owner, json=question())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["truncated"] and len(result["decisions"]) == 1
    assert result["answer_kind"] == "none" and result["usage"] == Usage().model_dump()
    assert not calls and count_operations(database) == 0


@pytest.mark.parametrize("failure", ["citation", "provider"])
def test_invalid_answer_is_not_cached_or_paid_again(backend, monkeypatch, failure):
    client, owner, database, calls = backend
    seed(database)
    attempted = []

    async def invalid(self, instruction, data, schema):
        attempted.append(data)
        if failure == "provider":
            raise ProviderUnavailable("fixture-private-provider-error")
        return CitedAnswer(answer="Unsupported", revision_ids=("foreign-revision",)), Usage(
            reasoning_calls=1
        )

    monkeypatch.setattr(Evon, "structured", invalid)
    body = question()
    first = client.post("/v1/repos/gh-101/questions", headers=owner, json=body)
    assert first.status_code == 503 and first.json()["code"] == "answer_failed"
    assert "fixture-private" not in first.text and "foreign-revision" not in first.text
    repeated = client.post("/v1/repos/gh-101/questions", headers=owner, json=body)
    assert repeated.status_code == 409 and repeated.json()["code"] == "model_outcome_uncertain"
    assert len(attempted) == 1
    with Session(database) as db:
        operation = db.scalar(select(ModelOperation))
        assert operation.status == "processing" and operation.result is None


def test_exact_reads_and_cached_receipt_accounting_are_separate(backend, monkeypatch):
    client, owner, database, calls = backend
    seed(database)
    measured = Usage(reasoning_calls=1, input_tokens=127, output_tokens=19)

    async def valid(self, instruction, data, schema):
        calls.append(data)
        return CitedAnswer(answer="No.", revision_ids=("revision-0",)), measured

    monkeypatch.setattr(Evon, "structured", valid)
    exact = client.post("/v1/repos/gh-101/questions", headers=owner, json=question(reason=False))
    assert exact.status_code == 200
    assert exact.json()["answer_kind"] == "confirmed_statements"
    assert exact.json()["usage"] == Usage().model_dump() and not calls
    body = question()
    answer = client.post("/v1/repos/gh-101/questions", headers=owner, json=body)
    assert answer.status_code == 200, answer.text
    assert answer.json()["usage"] == measured.model_dump()
    assert (
        client.post("/v1/repos/gh-101/questions", headers=owner, json=body).json() == answer.json()
    )
    assert len(calls) == 1
    changed = client.post(
        "/v1/repos/gh-101/questions", headers=owner, json={**body, "question": "Different"}
    )
    assert changed.status_code == 409 and len(calls) == 1
    delegated = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    agent = {"Authorization": "Bearer " + delegated["token"]}
    response = client.post("/v1/repos/gh-101/questions", headers=agent, json=body)
    assert response.status_code == 200 and len(calls) == 2
    assert count_operations(database) == 2


def test_revocation_during_synthesis_discards_output_and_disables_cache(backend, monkeypatch):
    client, owner, database, calls = backend
    seed(database)

    async def revoke(self, instruction, data, schema):
        calls.append(data)
        response = await asyncio.to_thread(client.post, "/v1/auth/logout", headers=owner)
        assert response.status_code == 200
        return CitedAnswer(answer="Private final output.", revision_ids=("revision-0",)), Usage(
            reasoning_calls=1
        )

    monkeypatch.setattr(Evon, "structured", revoke)
    body = question()
    result = client.post("/v1/repos/gh-101/questions", headers=owner, json=body)
    assert result.status_code == 401 and "Private final output" not in result.text
    assert client.post("/v1/repos/gh-101/questions", headers=owner, json=body).status_code == 401
    assert len(calls) == 1
    with Session(database) as db:
        operation = db.scalar(select(ModelOperation))
        assert operation.status == "processing" and operation.result is None


def test_same_operation_reservation_race_is_a_safe_conflict(backend):
    _, _, database, _ = backend
    if database.dialect.name != "postgresql":
        pytest.skip("Concurrent absent-row reservation requires PostgreSQL")
    seed(database)
    principal = Principal(
        "gh-1", False, frozenset({"gh-1"}), frozenset({"gh-101"}), (Grant("gh-1", Role.MAINTAINER),)
    )
    knowledge = KnowledgeService(PostgreSQLStore(database))
    service = Answers(SimpleNamespace(engine=database), knowledge, None)
    request = QuestionRequest.model_validate(question())
    context = knowledge.context(principal, "gh-101", request)
    barrier = threading.Barrier(2, timeout=10)

    def synchronize(mapper, connection, target):
        if target.operation_id == request.operation_id:
            barrier.wait()

    def reserve():
        try:
            service.reserve(principal, "gh-101", request, context)
            return "reserved"
        except DomainError as exc:
            assert exc.code == "model_outcome_uncertain" and exc.status == 409
            return "conflict"

    event.listen(ModelOperation, "before_insert", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
    finally:
        event.remove(ModelOperation, "before_insert", synchronize)
    assert sorted(results) == ["conflict", "reserved"]
    assert count_operations(database) == 1


@pytest.mark.parametrize(
    "route,changes", [("gh-102", {}), ("gh-101", {"snapshot_id": "foreign-snapshot"})]
)
def test_unavailable_scope_or_snapshot_never_calls_model(backend, route, changes):
    client, owner, database, calls = backend
    seed(database)
    response = client.post(f"/v1/repos/{route}/questions", headers=owner, json=question(**changes))
    assert response.status_code == 404
    assert not calls and count_operations(database) == 0
