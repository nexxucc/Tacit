"""Offline queue safety and explicit synchronization; no live provider calls."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from tacit_cli import offline
from tacit_client.client import ClientError
from test_access_management import engine as engine
from test_access_management import login
from test_access_management import setup as setup
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("TACIT_OFFLINE_QUEUE", str(tmp_path / "queue"))
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "worker.py").write_text('print("fixture")\n')
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    intent = tmp_path / "intent.txt"
    intent.write_text("Only current workers may publish.")
    args = [
        "prepare",
        "--repo",
        "repo",
        "--author",
        "human",
        "--commit",
        commit,
        "--file",
        "worker.py",
        "--text-file",
        str(intent),
        "--source-root",
        str(source),
        "--expected-snapshot",
        "none",
    ]
    result = runner.invoke(offline.app, args)
    assert result.exit_code == 0, result.output
    sid = json.loads(result.output)["id"]
    return source, sid, args


class FakeClient:
    calls = []
    identity = {"user_id": "human", "agent": False}
    head = None
    fail_post = False
    drafts = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, route, **kwargs):
        self.calls.append((method, route, kwargs))
        if route.endswith("/me"):
            return self.identity
        if route.endswith("/head"):
            return {"snapshot_id": self.head}
        body = kwargs["json"]
        operation = body["operation_id"]
        self.drafts.setdefault(
            operation,
            dict(
                id=str(uuid4()),
                workspace_id="workspace",
                repository_id="repo",
                author_id="human",
                decision_id=body["decision_id"] or str(uuid4()),
                expected_snapshot_id=body["expected_snapshot_id"],
                statement=body["statement"],
                source_commit=body["source_commit"],
                sources=[{"path": "worker.py", "blob_sha": "b" * 40}],
                rule=body["rule"],
                review_digest="c" * 64,
            ),
        )
        if self.fail_post:
            raise ClientError("Cannot reach the Tacit API")
        return self.drafts[operation]


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    FakeClient.identity = {"user_id": "human", "agent": False}
    FakeClient.head = None
    FakeClient.fail_post = False
    FakeClient.drafts = {}
    monkeypatch.setattr(offline, "TacitClient", FakeClient)
    return FakeClient


def sync(local, answer="y\n"):
    source, sid, _ = local
    return runner.invoke(offline.app, ["sync", sid, "--source-root", str(source)], input=answer)


def test_prepare_never_connects(local, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline preparation must not create an HTTP client")

    monkeypatch.setattr(offline, "TacitClient", forbidden)
    result = runner.invoke(offline.app, local[2])
    assert result.exit_code == 0
    entry = offline.read(local[1])
    assert entry.state == "queued"
    assert entry.body.expected_snapshot_id is None
    assert offline.path(local[1]).stat().st_mode & 0o077 == 0


def test_success_and_local_saved_result(local, fake):
    result = sync(local)
    assert result.exit_code == 0, result.output
    entry = offline.read(local[1])
    assert entry.state == "synced"
    assert entry.draft.statement == entry.body.statement
    assert [c[0] for c in fake.calls] == ["GET", "GET", "POST"]
    fake.calls.clear()
    assert sync(local).exit_code == 0
    assert fake.calls == []


def test_uncertain_retry_uses_same_body_even_after_source_and_head_change(local, fake):
    fake.fail_post = True
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "attempted"
    first = fake.calls[-1][2]["json"]
    (local[0] / "worker.py").write_text("changed after upload")
    fake.head = "new-head"
    fake.fail_post = False
    fake.calls.clear()
    result = sync(local)
    assert result.exit_code == 0, result.output
    assert fake.calls[-1][2]["json"] == first
    assert len(fake.drafts) == 1
    assert all(not c[1].endswith("/head") for c in fake.calls)


@pytest.mark.parametrize(
    "identity", [{"user_id": "other", "agent": False}, {"user_id": "human", "agent": True}]
)
def test_identity_ceiling(local, fake, identity):
    fake.identity = identity
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "queued"
    assert all(c[0] != "POST" for c in fake.calls)


def test_server_and_source_fail_before_network(local, fake, monkeypatch):
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8002")
    assert sync(local).exit_code == 1
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    (local[0] / "worker.py").write_text("changed")
    assert sync(local).exit_code == 1
    assert fake.calls == []


def test_conflict_explicit_rebase_and_cancel(local, fake):
    fake.head = "new-head"
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "queued"
    old = offline.read(local[1]).body.operation_id
    result = runner.invoke(
        offline.app, ["rebase", local[1], "--expected-snapshot", "new-head"], input="y\n"
    )
    assert result.exit_code == 0
    assert offline.read(local[1]).body.operation_id != old
    assert sync(local, answer="n\n").exit_code == 0
    assert all(c[0] != "POST" for c in fake.calls)
    assert sync(local).exit_code == 0


def test_attempted_immutable_and_forget(local, fake):
    fake.fail_post = True
    sync(local)
    original = offline.path(local[1]).read_bytes()
    result = runner.invoke(
        offline.app, ["rebase", local[1], "--expected-snapshot", "new"], input="y\n"
    )
    assert result.exit_code == 1
    assert offline.path(local[1]).read_bytes() == original
    assert runner.invoke(offline.app, ["forget", local[1]], input="n\n").exit_code == 0
    assert offline.path(local[1]).exists()
    assert runner.invoke(offline.app, ["forget", local[1]], input="y\n").exit_code == 0
    assert not offline.path(local[1]).exists()


def test_bound_quota_symlinks_and_corruption(local, fake, tmp_path):
    for _ in range(19):
        item = offline.read(local[1]).model_copy(update={"id": uuid4()})
        offline.write(item)
    assert runner.invoke(offline.app, local[2]).exit_code == 1
    assert runner.invoke(offline.app, ["show", "../escape"]).exit_code == 1
    target = offline.path(local[1])
    target.unlink()
    external = tmp_path / "private"
    external.write_text("private text that must not be printed")
    target.symlink_to(external)
    result = runner.invoke(offline.app, ["show", local[1]])
    assert result.exit_code == 1
    assert external.read_text() not in result.output


def test_post_race_head_conflict_can_be_rebased(local, fake, monkeypatch):
    original = fake.request

    def request(self, method, route, **kwargs):
        if method == "POST":
            raise ClientError("API error: snapshot_conflict (409)")
        return original(self, method, route, **kwargs)

    monkeypatch.setattr(fake, "request", request)
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "queued"


def test_attempt_digest_and_result_state_corruption(local, fake):
    fake.fail_post = True
    sync(local)
    file = offline.path(local[1])
    original = file.read_text()
    data = json.loads(original)
    data["body"]["statement"] = "tampered attempted payload"
    file.write_text(json.dumps(data))
    assert runner.invoke(offline.app, ["show", local[1]]).exit_code == 1
    file.write_text(original)
    data = json.loads(original)
    data["state"] = "synced"
    file.write_text(json.dumps(data))
    assert runner.invoke(offline.app, ["show", local[1]]).exit_code == 1


def test_prepare_with_valid_lock_and_wrong_scope(local, monkeypatch, tmp_path):
    from tacit_contracts.snapshots import SnapshotLock

    lock = SnapshotLock(
        api_url="http://127.0.0.1:8000",
        workspace_id="workspace",
        repository_id="repo",
        snapshot_id="snapshot",
        source_commit="a" * 40,
        manifest_digest="b" * 64,
        export_sha256="c" * 64,
    )
    file = tmp_path / "lock.json"
    file.write_text(lock.model_dump_json())
    args = local[2][:-2] + ["--lock", str(file)]
    result = runner.invoke(offline.app, args)
    assert result.exit_code == 0, result.output
    entry = offline.read(json.loads(result.output)["id"])
    assert entry.body.expected_snapshot_id == "snapshot"
    file.write_text(lock.model_copy(update={"repository_id": "different"}).model_dump_json())
    assert runner.invoke(offline.app, args).exit_code == 1


# Actual HTTP/service/database behavior, synthetic identity and source-provider responses.


def test_api_sync_recovery_never_publishes(local, setup, monkeypatch):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from tacit_api.db import PublicationReceipt, Snapshot, TeachingDraft

    client, provider, database = setup
    headers = login(client, provider, database)
    entry = offline.read(local[1])
    entry = entry.model_copy(update={"author_id": "gh-1", "repository_id": "gh-101"})
    offline.write(entry)
    lose_response = True

    class APIClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            kwargs.pop("timeout", None)
            response = client.request(method, path, headers=headers, **kwargs)
            if response.status_code >= 400:
                raise ClientError("fixture HTTP failure")
            if method == "POST" and lose_response:
                raise ClientError("Cannot reach the Tacit API")
            return response.json()

    monkeypatch.setattr(offline, "TacitClient", APIClient)
    assert sync(local).exit_code == 1
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 1
        assert db.scalar(select(func.count()).select_from(PublicationReceipt)) == 0
    lose_response = False
    result = sync(local)
    assert result.exit_code == 0, result.output
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 1
        assert db.scalar(select(func.count()).select_from(Snapshot)) == 0
    assert offline.read(local[1]).state == "synced"


def test_local_ack_failure_recovers_same_server_operation(local, fake, monkeypatch):
    original = offline.write

    def disk_full(entry):
        if entry.state == "synced":
            raise OSError("Fixture disk full")
        original(entry)

    monkeypatch.setattr(offline, "write", disk_full)
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "attempted"
    assert len(fake.drafts) == 1
    monkeypatch.setattr(offline, "write", original)
    assert sync(local).exit_code == 0
    assert len(fake.drafts) == 1


def test_attempt_record_must_persist_before_post(local, fake, monkeypatch):
    def disk_full(entry):
        raise OSError("Fixture disk full")

    monkeypatch.setattr(offline, "write", disk_full)
    assert sync(local).exit_code == 1
    assert all(call[0] != "POST" for call in fake.calls)
    assert offline.read(local[1]).state == "queued"


def test_invalid_server_and_oversized_statement(local, monkeypatch):
    monkeypatch.setenv("TACIT_API_URL", "https://example.invalid/path?token=secret")
    assert runner.invoke(offline.app, local[2]).exit_code == 1
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    intent = Path(local[2][local[2].index("--text-file") + 1])
    intent.write_text("x" * 32001)
    assert runner.invoke(offline.app, local[2]).exit_code == 1


def test_different_api_result_retains_attempt(local, fake, monkeypatch):
    original = fake.request

    def request(self, method, path, **kwargs):
        result = original(self, method, path, **kwargs)
        if method == "POST":
            result = dict(result, statement="Unexpected replacement")
        return result

    monkeypatch.setattr(fake, "request", request)
    assert sync(local).exit_code == 1
    assert offline.read(local[1]).state == "attempted"


def test_concurrent_preparation_respects_capacity(local):
    import sys
    from concurrent.futures import ThreadPoolExecutor

    for _ in range(18):
        offline.write(offline.read(local[1]).model_copy(update={"id": uuid4()}))

    def prepare_once(_):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from tacit_cli.main import main; main();",
                "offline",
                *local[2],
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare_once, range(2)))
    assert sorted(results) == [0, 1]
    assert len(list(offline.root().glob("*.json"))) == 20


def test_forget_corrupt_regular_record_but_never_follow_link(local, tmp_path):
    target = offline.path(local[1])
    target.write_text("{broken")
    assert runner.invoke(offline.app, ["forget", local[1]], input="n\n").exit_code == 0
    assert target.exists()
    assert runner.invoke(offline.app, ["forget", local[1]], input="y\n").exit_code == 0
    assert not target.exists()
    external = tmp_path / "external"
    external.write_text("unchanged")
    target.symlink_to(external)
    assert runner.invoke(offline.app, ["forget", local[1]], input="y\n").exit_code == 1
    assert external.read_text() == "unchanged"


def test_prepare_uuid_collision_preserves_existing_entry(local, monkeypatch):
    from uuid import UUID

    target = offline.path(local[1])
    original = target.read_bytes()
    monkeypatch.setattr(offline, "uuid4", lambda: UUID(local[1]))
    assert runner.invoke(offline.app, local[2]).exit_code == 1
    assert target.read_bytes() == original
