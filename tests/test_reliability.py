"""Crash, retry and retention acceptance using disposable data and fixture providers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from tacit_api.access import AccessService, now
from tacit_api.app import Settings, create_app
from tacit_api.artifacts import Artifacts, Objects
from tacit_api.db import Artifact, ModelOperation, OutboxEvent, PublicationIndex, TeachingRun
from tacit_api.github import GitHub
from tacit_api.publication import PublicationService
from tacit_api.worker import prune_transient, relay
from tacit_domain.access import DomainError
from test_access_management import Provider, login
from test_access_management import engine as engine
from test_publication import draft, publish


@pytest.fixture
def runtime(engine, tmp_path):
    provider = Provider()
    key = Fernet.generate_key().decode()
    github = GitHub("fixture", 100, httpx.MockTransport(provider.request))
    access = AccessService(engine, github, key)
    manager = PublicationService(access)
    objects = Objects(str(tmp_path / "objects"))
    artifacts = Artifacts(manager, objects, 3600)
    app = create_app(
        Settings(github_client_id="fixture", github_app_id=100, credential_encryption_key=key),
        github=github,
        database=engine,
    )
    with TestClient(app) as client:
        headers = login(client, provider, engine)
        token = headers["Authorization"].removeprefix("Bearer ")
        principal = access.principal(token)
        yield client, headers, access, artifacts, principal, token


def confirmed(runtime):
    client, headers, *_ = runtime
    view = draft(client, headers)
    response = publish(client, headers, view)
    assert response.status_code == 200, response.text
    return response.json()


def test_outbox_projection_replay_is_idempotent(runtime):
    result = confirmed(runtime)
    access = runtime[2]
    assert relay(access) == 1
    assert relay(access) == 0
    with Session(access.engine) as db, db.begin():
        item = db.scalar(select(OutboxEvent))
        item.processed_at = None  # Delivery replay after an acknowledged result was lost.
    assert relay(access) == 1
    with Session(access.engine) as db:
        assert db.scalar(select(func.count()).select_from(PublicationIndex)) == 1
        projection = db.scalar(select(PublicationIndex))
        assert projection.revision_id == result["revision_id"]


def test_outbox_crash_rolls_back_projection_and_ack(runtime):
    confirmed(runtime)
    access = runtime[2]

    def fail_ack(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE outbox_events SET processed_at"):
            raise RuntimeError("simulated crash before acknowledgment commit")

    event.listen(access.engine, "before_cursor_execute", fail_ack)
    try:
        # Handler failure is retained for retry rather than acknowledged.
        assert relay(access) == 0
    finally:
        event.remove(access.engine, "before_cursor_execute", fail_ack)
    with Session(access.engine) as db, db.begin():
        assert db.scalar(select(func.count()).select_from(PublicationIndex)) == 0
        item = db.scalar(select(OutboxEvent))
        assert item.processed_at is None
        assert item.attempts == 1
        item.next_attempt_at = now() - timedelta(seconds=1)
    assert relay(access) == 1


def test_outbox_poison_event_backoff_and_attempt_cap(runtime):
    access = runtime[2]
    with Session(access.engine) as db, db.begin():
        db.add(
            OutboxEvent(
                id="poison",
                workspace_id="gh-1",
                idempotency_key="poison",
                payload={"event": "unsupported"},
            )
        )
    for attempt in range(1, 9):
        assert relay(access) == 0
        with Session(access.engine) as db, db.begin():
            item = db.get(OutboxEvent, "poison")
            assert item.attempts == attempt
            assert item.processed_at is None
            assert item.error_code == "outbox_handler_failed"
            # Not eligible before backoff elapses.
        assert relay(access) == 0
        with Session(access.engine) as db, db.begin():
            db.get(OutboxEvent, "poison").next_attempt_at = now() - timedelta(seconds=1)
    assert relay(access) == 0
    with Session(access.engine) as db:
        assert db.get(OutboxEvent, "poison").attempts == 8


def test_concurrent_outbox_relays_do_not_duplicate(runtime):
    access = runtime[2]
    if access.engine.dialect.name != "postgresql":
        pytest.skip("Concurrent row locks require PostgreSQL")
    confirmed(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: relay(access), range(2)))
    assert sum(counts) == 1
    with Session(access.engine) as db:
        assert db.scalar(select(func.count()).select_from(PublicationIndex)) == 1


def test_artifact_encryption_integrity_delete_and_retention(runtime):
    _, _, access, artifacts, principal, token = runtime
    data = b"private fixture audio"
    created = artifacts.create(principal, "gh-101", token, data)
    aid = created["artifact_id"]
    with Session(access.engine) as db:
        item = db.get(Artifact, ("gh-1", "gh-101", aid))
        key = item.object_key
    encrypted = artifacts.objects.get(key)
    assert data not in encrypted
    assert artifacts.read(principal, "gh-101", token, aid) == data
    artifacts.objects.delete(key)
    artifacts.objects.put(key, access.cipher.encrypt(b"tampered"))
    with pytest.raises(DomainError, match="integrity"):
        artifacts.read(principal, "gh-101", token, aid)
    artifacts.delete(principal, "gh-101", token, aid)
    artifacts.delete(principal, "gh-101", token, aid)
    assert not artifacts.objects.local(key).exists()
    with pytest.raises(DomainError) as exc:
        artifacts.read(principal, "gh-101", token, aid)
    assert exc.value.status == 410
    assert artifacts.prune() == 0


def test_failed_object_upload_is_retained_for_cleanup(runtime, monkeypatch):
    _, _, access, artifacts, principal, token = runtime
    real_put = artifacts.objects.put

    def partial_put(key, data):
        real_put(key, data)
        raise OSError("simulated provider failure after writing object")

    monkeypatch.setattr(artifacts.objects, "put", partial_put)
    with pytest.raises(OSError):
        artifacts.create(principal, "gh-101", token, b"audio")
    with Session(access.engine) as db, db.begin():
        item = db.scalar(select(Artifact))
        assert item.status == "pending"
        key, aid = item.object_key, item.id
        item.expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError) as exc:
        artifacts.read(principal, "gh-101", token, aid)
    assert exc.value.status == 410
    assert artifacts.prune() == 1
    assert artifacts.prune() == 0
    assert not artifacts.objects.local(key).exists()


def test_artifact_quota_and_expired_slot_reuse(runtime):
    _, _, access, artifacts, principal, token = runtime
    for _ in range(10):
        artifacts.create(principal, "gh-101", token, b"audio")
    with pytest.raises(DomainError) as exc:
        artifacts.create(principal, "gh-101", token, b"audio")
    assert exc.value.code == "artifact_quota"
    with Session(access.engine) as db, db.begin():
        db.scalar(select(Artifact)).expires_at = now() - timedelta(seconds=1)
    assert artifacts.prune() == 1
    artifacts.create(principal, "gh-101", token, b"new audio")


def test_transient_cleanup_preserves_non_replayable_tombstones(runtime):
    access = runtime[2]
    with Session(access.engine) as db, db.begin():
        db.add(
            TeachingRun(
                workspace_id="gh-1",
                repository_id="gh-101",
                id="run",
                author_id="gh-1",
                operation_id=str(uuid4()),
                request_digest="digest",
                payload={"transcript": "private"},
                status="ready",
                expires_at=now() - timedelta(seconds=1),
            )
        )
        db.add(
            ModelOperation(
                workspace_id="gh-1",
                repository_id="gh-101",
                author_id="gh-1",
                operation_id="reserved",
                kind="extract",
                request_digest="digest",
                status="completed",
                result={"transcript": "private"},
                created_at=now() - timedelta(days=2),
            )
        )
    assert prune_transient(access) == 2
    assert prune_transient(access) == 0
    with Session(access.engine) as db:
        run = db.scalar(select(TeachingRun))
        operation = db.scalar(select(ModelOperation))
        assert run.status == operation.status == "expired"
        assert run.payload == {} and operation.result is None
        assert operation.operation_id == "reserved" and operation.request_digest == "digest"


def test_retention_does_not_tombstone_an_inflight_upload(runtime, monkeypatch):
    _, _, access, artifacts, principal, token = runtime
    if access.engine.dialect.name != "postgresql":
        pytest.skip("Retention/upload locking requires PostgreSQL")
    import tacit_api.artifacts as storage

    real_put = artifacts.objects.put
    real_now = storage.now
    cleanup_counts = []

    def slow_put(key, data):
        # The upload began before expiry, but cleanup runs after its pending lease.
        monkeypatch.setattr(storage, "now", lambda: real_now() + timedelta(minutes=10))
        try:
            cleanup_counts.append(artifacts.prune())
        finally:
            monkeypatch.setattr(storage, "now", real_now)
        real_put(key, data)

    monkeypatch.setattr(artifacts.objects, "put", slow_put)
    created = artifacts.create(principal, "gh-101", token, b"slow upload")
    assert cleanup_counts == [0]  # SKIP LOCKED; a later cycle can clean it safely.
    assert artifacts.read(principal, "gh-101", token, created["artifact_id"]) == b"slow upload"
    monkeypatch.setattr(storage, "now", lambda: real_now() + timedelta(hours=2))
    assert artifacts.prune() == 1
    assert not any(artifacts.objects.root.iterdir())


@pytest.mark.parametrize("status,revoked", [(401, True), (503, False)])
def test_reconciliation_revokes_denied_credentials_but_retries_provider_outages(
    runtime, monkeypatch, status, revoked
):
    from tacit_api.access import digest
    from tacit_api.db import Credential
    from tacit_api.worker import reconcile

    _, _, access, _, _, token = runtime
    with Session(access.engine) as db, db.begin():
        db.get(Credential, digest(token)).next_reconcile_at = now() - timedelta(seconds=1)

    def failed_provider(_):
        raise DomainError("provider_failure", "fixture failure", status)

    monkeypatch.setattr(access.github, "eligibility", failed_provider)
    assert reconcile(access) == 1
    with Session(access.engine) as db:
        item = db.get(Credential, digest(token))
        assert item.revoked is revoked
        from tacit_api.access import valid_time

        assert valid_time(item.next_reconcile_at) > now()
    assert reconcile(access) == 0
