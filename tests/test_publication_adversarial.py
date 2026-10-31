"""Adversarial immutable publication with fixture GitHub and disposable databases."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from tacit_api.db import OutboxEvent, PublicationReceipt, RepositoryHead, Snapshot, SnapshotMember
from tacit_api.publication import fingerprint
from test_access_management import engine as engine
from test_access_management import login
from test_access_management import setup as setup
from test_publication import COMMIT, counts, draft, publish


def test_same_confirmation_operation_concurrently_returns_one_receipt(setup):
    client, provider, database = setup
    if database.dialect.name != "postgresql":
        pytest.skip("Concurrent confirmation requires PostgreSQL row locking")
    owner = login(client, provider, database)
    view = draft(client, owner)
    operation = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, client, owner, view, operation=operation) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json() == results[1].json()
    with Session(database) as db:
        assert set(counts(db).values()) == {1}
        assert db.scalar(select(RepositoryHead.version)) == 1


def test_outbox_failure_rolls_back_existing_head_and_retry_commits_once(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    original = publish(client, owner, draft(client, owner)).json()
    update = draft(client, owner, parent=original["snapshot_id"], statement="Second policy.")
    operation = str(uuid4())

    def fail_outbox(mapper, connection, target):
        raise RuntimeError("fixture outbox crash")

    event.listen(OutboxEvent, "before_insert", fail_outbox)
    try:
        with pytest.raises(RuntimeError, match="fixture outbox crash"):
            publish(client, owner, update, operation=operation)
    finally:
        event.remove(OutboxEvent, "before_insert", fail_outbox)
    assert (
        client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"]
        == original["snapshot_id"]
    )
    with Session(database) as db:
        assert set(counts(db).values()) == {1}
        assert db.scalar(select(RepositoryHead.version)) == 1
    accepted = publish(client, owner, update, operation=operation)
    assert accepted.status_code == 200, accepted.text
    assert publish(client, owner, update, operation=operation).json() == accepted.json()
    with Session(database) as db:
        assert set(counts(db).values()) == {2}
        assert db.scalar(select(RepositoryHead.version)) == 2


def test_supersession_retains_other_members_and_exact_manifest_digest(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    first = publish(client, owner, draft(client, owner, statement="Original A.")).json()
    second = publish(
        client, owner, draft(client, owner, parent=first["snapshot_id"], statement="Policy B.")
    ).json()
    third = publish(
        client,
        owner,
        draft(
            client,
            owner,
            parent=second["snapshot_id"],
            decision=first["decision_id"],
            statement="Replacement A.",
        ),
    ).json()
    with Session(database) as db:
        snapshots = {item.id: item for item in db.scalars(select(Snapshot))}

        def members(sid):
            return sorted(
                db.scalars(
                    select(SnapshotMember.revision_id).where(SnapshotMember.snapshot_id == sid)
                )
            )

        assert members(first["snapshot_id"]) == [first["revision_id"]]
        assert members(second["snapshot_id"]) == sorted(
            [first["revision_id"], second["revision_id"]]
        )
        assert members(third["snapshot_id"]) == sorted(
            [second["revision_id"], third["revision_id"]]
        )
        assert snapshots[third["snapshot_id"]].digest == fingerprint(
            {
                "schema_version": 1,
                "repository_id": "gh-101",
                "parent_snapshot_id": second["snapshot_id"],
                "source_commit": COMMIT,
                "revision_ids": members(third["snapshot_id"]),
            }
        )
    history = client.get(
        f"/v1/repos/gh-101/decisions/{first['decision_id']}/history",
        headers=owner,
        params={"snapshot_id": third["snapshot_id"]},
    ).json()
    assert [item["revision_id"] for item in history] == [third["revision_id"], first["revision_id"]]


def test_confirmation_retry_cannot_bypass_revoked_credential(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    view = draft(client, owner)
    operation = str(uuid4())
    assert publish(client, owner, view, operation=operation).status_code == 200
    assert client.post("/v1/auth/logout", headers=owner).status_code == 200
    assert publish(client, owner, view, operation=operation).status_code == 401
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(PublicationReceipt)) == 1


def test_confirmation_rejects_foreign_draft_even_with_exact_digest(setup):
    client, provider, database = setup
    provider.org_owner = True
    owner = login(client, provider, database)
    view = draft(client, owner)
    # The same human is maintainer in another repository; exact digest possession
    # cannot move this source-bound draft into that repository.
    owner_org = client.post(
        "/v1/workspaces/gh-20/roles",
        headers=owner,
        json={"user_id": "gh-1", "role": "maintainer", "repository_id": "gh-202"},
    )
    assert owner_org.status_code == 200, owner_org.text
    response = client.post(
        "/v1/repos/gh-202/publications",
        headers=owner,
        json={
            "draft_id": view["id"],
            "operation_id": str(uuid4()),
            "review_digest": view["review_digest"],
            "expected_snapshot_id": None,
        },
    )
    assert response.status_code == 404
    with Session(database) as db:
        assert set(counts(db).values()) == {0}
