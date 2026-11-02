"""Snapshot portability, immutable scope and offline trust boundaries; fixture providers only."""

import json

import httpx
import pytest
from pydantic import ValidationError
from tacit_cli.main import app
from tacit_cli.snapshots import locked_context, read_lock
from tacit_contracts.models import Decision, Evidence
from tacit_contracts.snapshots import SnapshotExport, SnapshotLock, digest
from test_access_management import engine as engine
from test_access_management import login
from test_access_management import setup as setup
from test_publication import draft, publish
from typer.testing import CliRunner


def exported():
    decision = Decision(
        id="decision",
        revision_id="revision",
        statement="Do not guess.",
        files=("route.py",),
        source_commit="a" * 40,
        evidence=(
            Evidence(
                decision_id="decision", revision_id="revision", segment_id="published", kind="text"
            ),
        ),
    )
    return SnapshotExport(
        workspace_id="workspace",
        repository_id="repo",
        repository_name="owner/repo",
        snapshot_id="snapshot",
        source_commit="a" * 40,
        parent_snapshot_id=None,
        manifest_digest=digest(
            {
                "schema_version": 1,
                "repository_id": "repo",
                "parent_snapshot_id": None,
                "source_commit": "a" * 40,
                "revision_ids": ["revision"],
            }
        ),
        decisions=(decision,),
    )


def pinned(value):
    return SnapshotLock(
        api_url="https://tacit.example",
        workspace_id=value.workspace_id,
        repository_id=value.repository_id,
        snapshot_id=value.snapshot_id,
        source_commit=value.source_commit,
        manifest_digest=value.manifest_digest,
        export_sha256=value.content_digest(),
    )


@pytest.fixture
def files(tmp_path):
    value = exported()
    lock = tmp_path / "tacit.lock.json"
    export = tmp_path / "snapshot.json"
    lock.write_text(pinned(value).model_dump_json())
    export.write_text(value.model_dump_json())
    return lock, export, value


def test_offline_roundtrip_needs_no_credentials_or_network(files, monkeypatch):
    lock, export, value = files
    monkeypatch.delenv("TACIT_TOKEN", raising=False)
    monkeypatch.delenv("TACIT_AGENT_TOKEN", raising=False)

    def forbidden(*a, **kw):
        raise AssertionError("Offline read attempted network")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    result = locked_context(lock, export=export, files=("route.py",), limit=1, cursor=0)
    assert result.decisions == value.decisions and result.applicability == "confirmed"
    assert any("Offline export" in w for w in result.warnings)
    assert all(v == 0 for v in result.usage.model_dump().values())
    output = CliRunner().invoke(app, ["context", "--lock", str(lock), "--export", str(export)])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["decisions"][0]["statement"] == "Do not guess."


@pytest.mark.parametrize("change", [{"statement": "Guess freely."}, {"source_commit": "b" * 40}])
def test_tampering_is_rejected_against_lock(files, change):
    lock, export, value = files
    data = value.model_dump(mode="json")
    data["decisions"][0].update(change)
    export.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        locked_context(lock, export=export, files=(), limit=20, cursor=0)


def test_scope_mismatch_and_duplicate_keys_fail_before_network(files, monkeypatch):
    lock, export, _ = files

    def forbidden(*a, **kw):
        raise AssertionError("Unexpected network")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    result = CliRunner().invoke(app, ["context", "--lock", str(lock), "--repo", "wrong"])
    assert result.exit_code != 0
    lock.write_text('{"format":"tacit-lock-v1","format":"tacit-lock-v1"}')
    with pytest.raises(ValueError):
        read_lock(lock)


@pytest.mark.parametrize(
    "url",
    [
        "http://private.example",
        "https://user:secret@host",
        "https://host?token=secret",
        "https://host/#fragment",
        "file:///tmp/a",
    ],
)
def test_lock_rejects_credentials_and_unsafe_servers(url):
    data = pinned(exported()).model_dump()
    data["api_url"] = url
    with pytest.raises(ValidationError):
        SnapshotLock.model_validate(data)


def test_export_requires_complete_unique_manifest():
    value = exported().model_dump(mode="json")
    value["decisions"] = []
    with pytest.raises(ValueError):
        SnapshotExport.model_validate(value)
    value = exported().model_dump(mode="json")
    value["decisions"] *= 2
    with pytest.raises(ValueError):
        SnapshotExport.model_validate(value)


def test_server_origin_mismatch_cannot_redirect_credentials(files, monkeypatch):
    lock, _, _ = files
    monkeypatch.setenv("TACIT_API_URL", "https://different.example")

    def forbidden(*a, **kw):
        raise AssertionError("Credentials sent to mismatched server")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    result = CliRunner().invoke(app, ["snapshot", "verify", "--lock", str(lock)])
    assert result.exit_code != 0


def test_api_exports_published_content_only_and_revocation_applies(setup):
    client, provider, db = setup
    owner = login(client, provider, db)
    view = draft(client, owner)
    publication = publish(client, owner, view).json()
    sid = publication["snapshot_id"]
    route = f"/v1/repos/gh-101/snapshots/{sid}/export"
    # A later private draft must never leak into an existing export.
    draft(client, owner, parent=sid, statement="PRIVATE unpublished proposal")
    response = client.get(route, headers=owner)
    assert response.status_code == 200, response.text
    value = SnapshotExport.model_validate(response.json())
    assert value.decisions[0].statement == view["statement"]
    assert "PRIVATE" not in response.text and "provider_token" not in response.text
    assert client.get(route.replace("gh-101", "gh-202"), headers=owner).status_code == 404
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    headers = {"Authorization": "Bearer " + agent["token"]}
    assert client.get(route, headers=headers).status_code == 200
    assert client.post("/v1/auth/logout", headers=owner).status_code == 200
    assert client.get(route, headers=headers).status_code == 401


def test_cli_online_lock_export_and_locked_context(setup, monkeypatch, tmp_path):
    client, provider, db = setup
    owner = login(client, provider, db)
    view = draft(client, owner)
    publication = publish(client, owner, view).json()
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("TACIT_TOKEN", owner["Authorization"].removeprefix("Bearer "))
    from tacit_client.client import TacitClient

    def response(request):
        r = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(r.status_code, json=r.json())

    monkeypatch.setattr(
        "tacit_cli.snapshots.TacitClient",
        lambda **kw: TacitClient(**kw, transport=httpx.MockTransport(response)),
    )
    runner = CliRunner()
    lock = tmp_path / "tacit.lock.json"
    export = tmp_path / "snapshot.json"
    for args in (
        ["snapshot", "lock", "--repo", "gh-101", "--output", str(lock)],
        ["snapshot", "export", "--lock", str(lock), "--output", str(export)],
        ["snapshot", "verify", "--lock", str(lock), "--export", str(export)],
        ["context", "--lock", str(lock)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (result.output, result.exception)
    assert read_lock(lock).snapshot_id == publication["snapshot_id"]
    before = lock.read_bytes()
    result = runner.invoke(app, ["snapshot", "lock", "--repo", "gh-101", "--output", str(lock)])
    assert result.exit_code != 0 and lock.read_bytes() == before
    result = runner.invoke(app, ["context", "--lock", str(lock), "--commit", "b" * 40])
    assert result.exit_code != 0


def test_locked_queries_do_not_move_when_head_advances(setup, monkeypatch, tmp_path):
    from tacit_client.client import TacitClient

    client, provider, db = setup
    owner = login(client, provider, db)
    first = publish(client, owner, draft(client, owner)).json()
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("TACIT_TOKEN", owner["Authorization"].removeprefix("Bearer "))
    value = SnapshotExport.model_validate(
        client.get(
            f"/v1/repos/gh-101/snapshots/{first['snapshot_id']}/export", headers=owner
        ).json()
    )
    lock = SnapshotLock(**{**pinned(value).model_dump(), "api_url": "http://127.0.0.1:8000"})
    path = tmp_path / "tacit.lock.json"
    path.write_text(lock.model_dump_json())
    later = draft(client, owner, parent=first["snapshot_id"], statement="Newer knowledge.")
    publish(client, owner, later)

    def response(request):
        result = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(result.status_code, json=result.json())

    monkeypatch.setattr(
        "tacit_cli.snapshots.TacitClient",
        lambda **kw: TacitClient(**kw, transport=httpx.MockTransport(response)),
    )
    monkeypatch.setattr(
        "tacit_cli.main.TacitClient",
        lambda **kw: TacitClient(**kw, transport=httpx.MockTransport(response)),
    )
    result = CliRunner().invoke(app, ["ask", "What is the rule?", "--lock", str(path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["snapshot_id"] == first["snapshot_id"]
    assert "Newer knowledge" not in data["answer"]
    assert all(v == 0 for v in data["usage"].values())


def test_offline_export_retains_but_does_not_apply_other_commits(tmp_path):
    from tacit_contracts.models import ContextRequest, Role
    from tacit_domain.access import Grant, Principal
    from tacit_domain.knowledge import KnowledgeService, Repository, Snapshot

    initial = exported()
    older = initial.decisions[0].model_copy(
        update={"id": "older", "revision_id": "older", "source_commit": "b" * 40}
    )
    decisions = (older, *initial.decisions)
    data = initial.model_dump(mode="json")
    data["decisions"] = [d.model_dump(mode="json") for d in decisions]
    data["manifest_digest"] = digest(
        {
            "schema_version": 1,
            "repository_id": "repo",
            "parent_snapshot_id": None,
            "source_commit": "a" * 40,
            "revision_ids": ["older", "revision"],
        }
    )
    value = SnapshotExport.model_validate(data)
    lock = tmp_path / "lock.json"
    export = tmp_path / "snapshot.json"
    lock.write_text(pinned(value).model_dump_json())
    export.write_text(value.model_dump_json())
    result = locked_context(lock, export=export, files=(), limit=1, cursor=0)
    assert [d.id for d in result.decisions] == ["decision"]
    assert "Retained knowledge" in result.warnings[0]
    empty = locked_context(lock, export=export, files=("other.py",), limit=1, cursor=0)
    assert empty.status == "unknown" and empty.decisions == ()

    # Same applicability/filtering semantics as the online domain service.
    class Store:
        def repositories(self):
            return (Repository("repo", "workspace", "owner/repo"),)

        def snapshot(self, *args):
            return Snapshot("snapshot", "repo", "a" * 40, decisions)

    principal = Principal(
        "reader",
        False,
        frozenset({"workspace"}),
        frozenset({"repo"}),
        (Grant("workspace", Role.LEARNER, None),),
    )
    online = KnowledgeService(Store()).context(
        principal, "repo", ContextRequest(snapshot_id="snapshot", source_commit="a" * 40, limit=1)
    )
    assert result.decisions == online.decisions and result.warnings[:-1] == online.warnings


def test_offline_pagination_and_size_limit(files):
    from tacit_cli.snapshots import read_json

    lock, export, value = files
    decisions = [
        value.decisions[0].model_copy(update={"id": str(i), "revision_id": str(i)})
        for i in range(3)
    ]
    data = value.model_dump(mode="json")
    data["decisions"] = [d.model_dump(mode="json") for d in decisions]
    data["manifest_digest"] = digest(
        {
            "schema_version": 1,
            "repository_id": "repo",
            "parent_snapshot_id": None,
            "source_commit": "a" * 40,
            "revision_ids": ["0", "1", "2"],
        }
    )
    updated = SnapshotExport.model_validate(data)
    lock.write_text(pinned(updated).model_dump_json())
    export.write_text(updated.model_dump_json())
    first = locked_context(lock, export=export, files=(), limit=2, cursor=0)
    last = locked_context(lock, export=export, files=(), limit=2, cursor=first.next_cursor)
    assert first.truncated and first.next_cursor == 2
    assert not last.truncated and last.next_cursor is None
    assert [d.id for d in (*first.decisions, *last.decisions)] == ["0", "1", "2"]
    with pytest.raises(ValueError):
        read_json(export, 20)


@pytest.mark.parametrize("field", ["format", "api_version", "schema_version", "evaluator_version"])
def test_incompatible_files_fail_closed(files, field):
    lock, export, value = files
    data = pinned(value).model_dump()
    data[field] = "future-version"
    lock.write_text(json.dumps(data))
    result = CliRunner().invoke(
        app, ["snapshot", "verify", "--lock", str(lock), "--export", str(export), "--offline"]
    )
    assert result.exit_code != 0


def test_export_file_cannot_overwrite_or_follow_symlink(files, tmp_path):
    from tacit_cli.snapshots import write_json

    existing = tmp_path / "original"
    existing.write_bytes(b"keep")
    redirected = tmp_path / "symlink"
    redirected.symlink_to(existing)
    for path in (existing, redirected):
        with pytest.raises(OSError):
            write_json(path, {"new": "value"})
    assert existing.read_bytes() == b"keep"


def test_export_budget_never_yields_a_partial_snapshot():
    value = exported()
    data = value.model_dump(mode="json")
    data["decisions"][0]["statement"] = "x" * 4_000_000
    with pytest.raises(ValueError, match="export size limit"):
        SnapshotExport.model_validate(data)


def test_context_export_requires_explicit_lock(files):
    _, export, _ = files
    result = CliRunner().invoke(app, ["context", "--export", str(export)])
    assert result.exit_code != 0
