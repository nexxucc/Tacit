"""Real transactions plus fixture GitHub responses; no model/provider billing."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from tacit_api.db import (
    AuditEvent,
    DecisionRevision,
    OutboxEvent,
    PublicationReceipt,
    Snapshot,
)
from test_access_management import engine as engine
from test_access_management import login, webhook
from test_access_management import setup as setup

COMMIT = "a" * 40


def draft(
    client,
    headers,
    *,
    parent=None,
    statement="Expired workers must not publish.",
    decision=None,
    commit=COMMIT,
    operation=None,
    files=None,
    rule=None,
    repo="gh-101",
):
    response = client.post(
        f"/v1/repos/{repo}/teaching-sessions",
        headers=headers,
        json={
            "operation_id": operation or str(uuid4()),
            "statement": statement,
            "source_commit": commit,
            "files": files or ["worker.py"],
            "expected_snapshot_id": parent,
            "decision_id": decision,
            "rule": rule,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, headers, view, *, operation=None, **changes):
    body = {
        "draft_id": view["id"],
        "operation_id": operation or str(uuid4()),
        "review_digest": view["review_digest"],
        "expected_snapshot_id": view["expected_snapshot_id"],
        **changes,
    }
    return client.post(
        f"/v1/repos/{view['repository_id']}/publications", headers=headers, json=body
    )


def counts(db):
    return {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (DecisionRevision, Snapshot, PublicationReceipt, OutboxEvent)
    }


def test_draft_private_confirmation_retrieval_and_retries(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    operation = str(uuid4())
    view = draft(client, owner, operation=operation)
    assert draft(client, owner, operation=operation) == view
    assert view["sources"] == [{"path": "worker.py", "blob_sha": "b" * 40}]
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    read = {"Authorization": "Bearer " + agent["token"]}
    assert (
        client.get(f"/v1/repos/gh-101/teaching-sessions/{view['id']}", headers=read).status_code
        == 403
    )
    assert publish(client, read, view).status_code == 403
    publication_op = str(uuid4())
    response = publish(client, owner, view, operation=publication_op)
    assert response.status_code == 200, response.text
    result = response.json()
    assert publish(client, owner, view, operation=publication_op).json() == result
    assert publish(client, owner, view).json()["code"] == "already_published"
    with Session(database) as db:
        assert counts(db) == {
            "decision_revisions": 1,
            "snapshots": 1,
            "publication_receipts": 1,
            "outbox_events": 1,
        }
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.action == "knowledge.confirmed"))
            is not None
        )
    context = client.post(
        "/v1/repos/gh-101/context",
        headers=read,
        json={"snapshot_id": result["snapshot_id"], "source_commit": COMMIT},
    ).json()
    assert context["decisions"][0]["statement"] == view["statement"]
    assert context["evidence"][0]["kind"] == "text"
    assert context["usage"] == {"stt_calls": 0, "tts_calls": 0, "reasoning_calls": 0}
    changed = publish(client, owner, view, operation=publication_op, review_digest="0" * 64)
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"


def test_review_digest_stale_head_and_reused_draft_operation(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    op = str(uuid4())
    one = draft(client, owner, operation=op)
    two = draft(client, owner, statement="Another statement.")
    result = publish(client, owner, one, review_digest="0" * 64)
    assert result.json()["code"] == "review_conflict"
    assert publish(client, owner, one).status_code == 200
    assert publish(client, owner, two).json()["code"] == "snapshot_conflict"
    different = client.post(
        "/v1/repos/gh-101/teaching-sessions",
        headers=owner,
        json={
            "operation_id": op,
            "statement": "Changed.",
            "source_commit": COMMIT,
            "files": ["worker.py"],
            "expected_snapshot_id": None,
        },
    )
    assert different.json()["code"] == "idempotency_conflict"
    with Session(database) as db:
        assert counts(db)["decision_revisions"] == 1


def test_supersession_history_snapshot_preservation_and_pagination(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    first = draft(client, owner)
    a = publish(client, owner, first).json()
    second = draft(
        client,
        owner,
        parent=a["snapshot_id"],
        decision=a["decision_id"],
        statement="Use the authoritative clock for lease expiry.",
    )
    assert second["previous"]["statement"] == first["statement"]
    b = publish(client, owner, second).json()
    old = client.post(
        "/v1/repos/gh-101/context",
        headers=owner,
        json={"snapshot_id": a["snapshot_id"], "source_commit": COMMIT},
    ).json()
    new = client.post(
        "/v1/repos/gh-101/context",
        headers=owner,
        json={"snapshot_id": b["snapshot_id"], "source_commit": COMMIT},
    ).json()
    assert old["decisions"][0]["statement"] == first["statement"]
    assert new["decisions"][0]["statement"] == second["statement"]
    path = f"/v1/repos/gh-101/decisions/{a['decision_id']}/history"
    assert [
        d["revision_id"]
        for d in client.get(path, headers=owner, params={"snapshot_id": b["snapshot_id"]}).json()
    ] == [b["revision_id"], a["revision_id"]]
    assert [
        d["revision_id"]
        for d in client.get(path, headers=owner, params={"snapshot_id": a["snapshot_id"]}).json()
    ] == [a["revision_id"]]
    page = client.get(
        path, headers=owner, params={"snapshot_id": b["snapshot_id"], "limit": 1, "cursor": 1}
    ).json()
    assert page[0]["revision_id"] == a["revision_id"]
    assert (
        client.get(
            path, headers=owner, params={"snapshot_id": b["snapshot_id"], "limit": 101}
        ).status_code
        == 422
    )


def test_new_commit_does_not_relabel_retained_knowledge_as_applicable(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    rule = {"when": [{"field": "lease_expired", "equals": True}], "outcome": "forbidden"}
    first = publish(client, owner, draft(client, owner, rule=rule)).json()
    second = publish(
        client,
        owner,
        draft(
            client, owner, parent=first["snapshot_id"], statement="New rationale.", commit="c" * 40
        ),
    ).json()
    body = {"snapshot_id": second["snapshot_id"], "source_commit": "c" * 40}
    result = client.post("/v1/repos/gh-101/context", headers=owner, json=body).json()
    assert len(result["decisions"]) == 1 and result["decisions"][0]["statement"] == "New rationale."
    assert result["warnings"]
    result = client.post(
        "/v1/repos/gh-101/evaluate", headers=owner, json={**body, "facts": {"lease_expired": True}}
    ).json()
    assert result["decision"] == "unknown"


def test_invalid_sources_cross_tenant_references_and_revocation(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    request = {
        "operation_id": str(uuid4()),
        "statement": "Explanation",
        "source_commit": COMMIT,
        "files": ["missing.py"],
        "expected_snapshot_id": None,
    }
    result = client.post("/v1/repos/gh-101/teaching-sessions", headers=owner, json=request)
    assert result.json()["code"] == "source_unbound"
    result = client.post(
        "/v1/repos/gh-101/teaching-sessions",
        headers=owner,
        json={**request, "files": ["../secrets"]},
    )
    assert result.status_code == 422
    view = draft(client, owner)
    assert (
        client.get(f"/v1/repos/gh-202/teaching-sessions/{view['id']}", headers=owner).status_code
        == 403
    )
    assert (
        webhook(
            client, {"installation": {"id": 10}, "repositories_removed": [{"id": 101}]}
        ).status_code
        == 200
    )
    assert publish(client, owner, view).status_code == 404
    with Session(database) as db:
        assert counts(db)["snapshots"] == 0


def test_postgres_publication_race_atomicity_and_immutable_records(setup, monkeypatch):
    client, provider, database = setup
    if database.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks and immutable triggers")
    owner = login(client, provider, database)
    second = login(client, provider, database, 2)
    assert (
        client.post(
            "/v1/workspaces/gh-1/invitations",
            headers=owner,
            json={"github_id": "2", "repository_ids": ["gh-101"]},
        ).status_code
        == 200
    )
    assert client.get("/v1/workspaces", headers=second).status_code == 200
    assert (
        client.post(
            "/v1/workspaces/gh-1/roles",
            headers=owner,
            json={"user_id": "gh-2", "role": "maintainer", "repository_id": "gh-101"},
        ).status_code
        == 200
    )
    a, b = draft(client, owner), draft(client, second, statement="Another explanation.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda item: publish(client, item[0], item[1]), [(owner, a), (second, b)])
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    with Session(database) as db:
        assert counts(db) == {
            "decision_revisions": 1,
            "snapshots": 1,
            "publication_receipts": 1,
            "outbox_events": 1,
        }
    for table, column in [
        ("teaching_drafts", "request_digest"),
        ("publication_receipts", "request_digest"),
        ("decision_revisions", "decision_id"),
        ("snapshots", "digest"),
    ]:
        with pytest.raises(Exception, match="Tacit immutable record"):
            with database.begin() as conn:
                conn.execute(text(f"UPDATE {table} SET {column} = 'tampered'"))
    # Force failure after revisions/snapshot preparation: none of the publication may commit.
    from sqlalchemy import event

    head = client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"]
    view = draft(client, owner, parent=head)

    def fail_insert(mapper, connection, target):
        raise RuntimeError("simulated crash before confirmation receipt")

    event.listen(PublicationReceipt, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            publish(client, owner, view)
    finally:
        event.remove(PublicationReceipt, "before_insert", fail_insert)
    with Session(database) as db:
        assert counts(db) == {
            "decision_revisions": 1,
            "snapshots": 1,
            "publication_receipts": 1,
            "outbox_events": 1,
        }
    assert publish(client, owner, view).status_code == 200


def test_cli_review_cancel_confirm_and_mcp_read_same_snapshot(setup, monkeypatch, tmp_path):
    import asyncio
    import importlib
    import json

    from tacit_client.client import ClientError
    from typer.testing import CliRunner

    client, provider, database = setup
    owner = login(client, provider, database)
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    writes = []

    class Bridge:
        def __init__(self, *, agent=False):
            self.headers = {"Authorization": "Bearer " + agent_token} if agent else owner

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            if path.endswith("/publications"):
                writes.append(path)
            response = client.request(method, path, headers=self.headers, **kwargs)
            if response.status_code >= 400:
                raise ClientError(response.json()["code"])
            return response.json()

    agent_token = agent["token"]
    cli = importlib.import_module("tacit_cli.main")
    mcp = importlib.import_module("tacit_mcp.server")
    monkeypatch.setattr(cli, "TacitClient", Bridge)
    monkeypatch.setattr(mcp, "TacitClient", Bridge)
    explanation = tmp_path / "intent.txt"
    explanation.write_text("Expired workers must not publish.")
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "teach",
            "--repo",
            "gh-101",
            "--commit",
            COMMIT,
            "--file",
            "worker.py",
            "--text-file",
            str(explanation),
        ],
    )
    assert result.exit_code == 0, result.output
    view = json.loads(result.stdout)
    assert not writes
    result = runner.invoke(cli.app, ["review", view["id"], "--repo", "gh-101"])
    assert (
        result.exit_code == 0
        and json.loads(result.stdout)["review_digest"] == view["review_digest"]
    )
    result = runner.invoke(cli.app, ["confirm", view["id"], "--repo", "gh-101"], input="n\n")
    assert result.exit_code == 0, result.output
    assert not writes
    result = runner.invoke(cli.app, ["confirm", view["id"], "--repo", "gh-101"], input="y\n")
    assert result.exit_code == 0, result.output
    publication = json.loads(result.stdout)
    content = asyncio.run(
        mcp.create_server().call_tool(
            "get_context",
            {
                "repository_id": "gh-101",
                "snapshot_id": publication["snapshot_id"],
                "source_commit": COMMIT,
            },
        )
    )
    assert content[1]["decisions"][0]["statement"] == view["statement"]
    assert content[1]["usage"] == {"stt_calls": 0, "tts_calls": 0, "reasoning_calls": 0}
    assert len(writes) == 1


def test_learner_denied_drafts_and_role_revocation_before_confirmation(setup):
    client, provider, database = setup
    owner = login(client, provider, database)
    learner = login(client, provider, database, 2)
    assert (
        client.post(
            "/v1/workspaces/gh-1/invitations",
            headers=owner,
            json={"github_id": "2", "repository_ids": ["gh-101"]},
        ).status_code
        == 200
    )
    assert client.get("/v1/workspaces", headers=learner).status_code == 200
    view = draft(client, owner)
    assert (
        client.get(f"/v1/repos/gh-101/teaching-sessions/{view['id']}", headers=learner).status_code
        == 403
    )
    assert publish(client, learner, view).status_code == 403
    assert (
        client.post(
            "/v1/repos/gh-101/teaching-sessions",
            headers=learner,
            json={
                "operation_id": str(uuid4()),
                "statement": "Learner knowledge",
                "source_commit": COMMIT,
                "files": ["worker.py"],
                "expected_snapshot_id": None,
            },
        ).status_code
        == 403
    )
    grant = {"user_id": "gh-2", "role": "maintainer", "repository_id": "gh-101"}
    assert client.post("/v1/workspaces/gh-1/roles", headers=owner, json=grant).status_code == 200
    own = draft(client, learner)
    assert (
        client.post("/v1/workspaces/gh-1/roles/revoke", headers=owner, json=grant).status_code
        == 200
    )
    assert publish(client, learner, own).status_code == 403
    with Session(database) as db:
        assert counts(db)["decision_revisions"] == 0
