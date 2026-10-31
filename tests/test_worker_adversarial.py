"""Adversarial bounded maintenance using disposable tenant databases only."""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_api.access import digest, now, valid_time
from tacit_api.db import Credential, OutboxEvent, PublicationIndex
from tacit_api.worker import reconcile, relay
from test_access_management import engine as engine
from test_reliability import confirmed
from test_reliability import runtime as runtime


def test_poison_batch_backoff_does_not_starve_valid_publication(runtime):
    result = confirmed(runtime)
    access = runtime[2]
    with Session(access.engine) as db, db.begin():
        publication = db.scalar(select(OutboxEvent))
        publication.next_attempt_at = now() - timedelta(seconds=1)
        db.add_all(
            [
                OutboxEvent(
                    id=f"poison-{index}",
                    workspace_id="gh-1",
                    idempotency_key=f"poison-{index}",
                    payload={"event": "unknown"},
                    next_attempt_at=now() - timedelta(days=1),
                )
                for index in range(3)
            ]
        )
    assert relay(access, limit=2) == 0
    assert relay(access, limit=2) == 1
    assert relay(access, limit=2) == 0
    with Session(access.engine) as db:
        assert db.scalar(select(func.count()).select_from(PublicationIndex)) == 1
        projection = db.scalar(select(PublicationIndex))
        assert projection.revision_id == result["revision_id"]
        assert {
            item.attempts
            for item in db.scalars(select(OutboxEvent).where(OutboxEvent.id.like("poison-%")))
        } == {1}


def test_outbox_cannot_project_foreign_workspace_revision(runtime):
    result = confirmed(runtime)
    access = runtime[2]
    with Session(access.engine) as db, db.begin():
        db.add(
            OutboxEvent(
                id="wrong-workspace",
                workspace_id="gh-20",
                idempotency_key="wrong-workspace",
                payload={
                    "event": "knowledge.confirmed",
                    "repository_id": "gh-101",
                    "revision_id": result["revision_id"],
                },
            )
        )
    assert relay(access) == 1
    with Session(access.engine) as db:
        bad = db.get(OutboxEvent, "wrong-workspace")
        assert bad.processed_at is None and bad.attempts == 1
        assert db.scalar(select(func.count()).select_from(PublicationIndex)) == 1
        assert db.scalar(select(PublicationIndex)).workspace_id == "gh-1"


def test_reconcile_missing_provider_token_cannot_starve_due_credentials(runtime, monkeypatch):
    _, _, access, _, _, token = runtime
    with Session(access.engine) as db, db.begin():
        real = db.get(Credential, digest(token))
        real.next_reconcile_at = now() - timedelta(seconds=1)
        db.add(
            Credential(
                token_hash="invalid-human",
                user_id="gh-1",
                agent=False,
                provider_token=None,
                expires_at=now() + timedelta(hours=1),
                next_reconcile_at=now() - timedelta(days=1),
            )
        )
    calls = []
    original = access.github.eligibility

    def eligibility(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(access.github, "eligibility", eligibility)
    assert reconcile(access, limit=1) == 1
    assert reconcile(access, limit=1) == 1
    assert calls == ["user-1"]
    with Session(access.engine) as db:
        assert db.get(Credential, "invalid-human").revoked
        assert valid_time(db.get(Credential, digest(token)).next_reconcile_at) > now()


def test_reconcile_revoked_during_provider_lookup_does_not_restore_credential(runtime, monkeypatch):
    _, _, access, _, _, token = runtime
    with Session(access.engine) as db, db.begin():
        db.get(Credential, digest(token)).next_reconcile_at = now() - timedelta(seconds=1)
    original = access.github.eligibility

    def revoke(value):
        access.logout(token)
        return original(value)

    monkeypatch.setattr(access.github, "eligibility", revoke)
    assert reconcile(access) == 1
    with Session(access.engine) as db:
        assert db.get(Credential, digest(token)).revoked
    assert reconcile(access) == 0


def test_worker_signal_stops_wait_and_disposes_engine(monkeypatch):
    worker = importlib.import_module("tacit_worker.main")
    callbacks = {}
    disposed = []
    cycles = []
    settings = SimpleNamespace(
        database_url="postgresql://fixture",
        github_client_id="fixture",
        github_app_id=1,
        credential_encryption_key="fixture",
        demo=False,
    )
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), dispose=lambda: disposed.append(True)
    )
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(worker, "GitHub", lambda *a: object())
    monkeypatch.setattr(worker, "AccessService", lambda *a: object())
    monkeypatch.setattr(worker.signal, "signal", lambda sig, fn: callbacks.update({sig: fn}))

    def cycle(*args):
        cycles.append(True)
        callbacks[worker.signal.SIGTERM](None, None)
        return {}

    monkeypatch.setattr(worker, "cycle", cycle)
    monkeypatch.setattr("sys.argv", ["tacit-worker", "--interval", "60"])
    worker.main()
    assert cycles == [True] and disposed == [True]


@pytest.mark.parametrize("once", [False, True])
def test_worker_failure_disposes_engine_and_never_acknowledges_success(monkeypatch, once):
    worker = importlib.import_module("tacit_worker.main")
    callbacks = {}
    disposed = []
    settings = SimpleNamespace(
        database_url="postgresql://fixture",
        github_client_id="fixture",
        github_app_id=1,
        credential_encryption_key="fixture",
        demo=False,
    )
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), dispose=lambda: disposed.append(True)
    )
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(worker, "GitHub", lambda *a: object())
    monkeypatch.setattr(worker, "AccessService", lambda *a: object())
    monkeypatch.setattr(worker.signal, "signal", lambda sig, fn: callbacks.update({sig: fn}))

    def fail(*args):
        callbacks[worker.signal.SIGTERM](None, None)
        raise RuntimeError("secret fixture provider error")

    monkeypatch.setattr(worker, "cycle", fail)
    monkeypatch.setattr("sys.argv", ["tacit-worker", *(["--once"] if once else [])])
    if once:
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 1
    else:
        worker.main()
    assert disposed == [True]


def test_reconciliation_ignores_agents_revoked_and_expired_at_boundary(runtime, monkeypatch):
    import tacit_api.worker as worker

    _, _, access, _, _, token = runtime
    boundary = now()
    with Session(access.engine) as db, db.begin():
        original = db.get(Credential, digest(token))
        original.next_reconcile_at = boundary - timedelta(seconds=1)
        for identity, agent, revoked, expires in [
            ("agent", True, False, boundary + timedelta(hours=1)),
            ("revoked", False, True, boundary + timedelta(hours=1)),
            ("expired", False, False, boundary),
        ]:
            db.add(
                Credential(
                    token_hash=identity,
                    user_id="gh-1",
                    agent=agent,
                    revoked=revoked,
                    provider_token=access.encrypt(identity),
                    expires_at=expires,
                    next_reconcile_at=boundary - timedelta(days=1),
                )
            )
    calls = []
    real = access.github.eligibility

    def collect(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(worker, "now", lambda: boundary)
    monkeypatch.setattr(access.github, "eligibility", collect)
    assert reconcile(access) == 1
    assert calls == ["user-1"]
    assert reconcile(access) == 0


def test_pruning_exact_boundary_keeps_future_state_and_operation_tombstone(runtime, monkeypatch):
    import tacit_api.worker as worker
    from tacit_api.db import ModelOperation, TeachingRun

    access = runtime[2]
    boundary = now()
    with Session(access.engine) as db, db.begin():
        for identity, expires in [
            ("boundary", boundary),
            ("future", boundary + timedelta(microseconds=1)),
        ]:
            db.add(
                TeachingRun(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    id=identity,
                    author_id="gh-1",
                    operation_id=identity,
                    request_digest="digest",
                    payload={"sensitive": "fixture"},
                    status="ready_for_review",
                    expires_at=expires,
                )
            )
            db.add(
                ModelOperation(
                    workspace_id="gh-1",
                    repository_id="gh-101",
                    author_id="gh-1",
                    operation_id=identity,
                    kind="extract",
                    request_digest="digest",
                    status="completed",
                    result={"private": "fixture"},
                    created_at=expires - timedelta(days=1),
                )
            )
    monkeypatch.setattr(worker, "now", lambda: boundary)
    assert worker.prune_transient(access) == 2
    assert worker.prune_transient(access) == 0
    with Session(access.engine) as db:
        runs = {item.id: item for item in db.scalars(select(TeachingRun))}
        operations = {item.operation_id: item for item in db.scalars(select(ModelOperation))}
        assert runs["boundary"].status == "expired" and runs["boundary"].payload == {}
        assert operations["boundary"].status == "expired" and operations["boundary"].result is None
        assert operations["boundary"].request_digest == "digest"
        assert runs["future"].status == "ready_for_review" and runs["future"].payload
        assert operations["future"].status == "completed" and operations["future"].result
