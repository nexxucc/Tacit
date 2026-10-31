"""Read-only adapters and local source selection with fixture credentials only."""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from tacit_cli import config, source_context
from tacit_cli.main import app
from tacit_client.client import TacitClient
from tacit_mcp.server import create_server
from typer.testing import CliRunner


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source ü checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "worker.py").write_text('print("committed")\n', encoding="utf-8")
    subprocess.run(["git", "add", "worker.py"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, commit


def test_source_capture_rejects_git_option_commit_before_side_effect(checkout, tmp_path):
    root, _ = checkout
    output = tmp_path / "should-not-be-written"
    with pytest.raises(ValueError, match="commit"):
        source_context.capture(root, f"--output={output}", ["worker.py"])
    assert not Path(str(output) + ":worker.py").exists()


def test_source_capture_tracks_dirty_bytes_and_rejects_path_escape(checkout, tmp_path):
    root, commit = checkout
    assert source_context.capture(root, commit, ["worker.py"])[0].dirty_sha256 is None
    dirty = b'print("dirty")\n'
    (root / "worker.py").write_bytes(dirty)
    selection = source_context.capture(root, commit, ["worker.py"])[0]
    assert selection.dirty_sha256 == hashlib.sha256(dirty).hexdigest()
    outside = tmp_path / "outside.py"
    outside.write_text("outside")
    (root / "escape.py").symlink_to(outside)
    for path in ["escape.py", "../outside.py", "/outside.py", r"..\outside.py"]:
        with pytest.raises(ValueError):
            source_context.capture(root, commit, [path])


def test_source_capture_bounds_local_input_before_git(checkout, monkeypatch):
    root, commit = checkout
    (root / "worker.py").write_bytes(b"x" * 65537)
    monkeypatch.setattr(
        source_context.subprocess, "run", lambda *a, **kw: pytest.fail("git called")
    )
    with pytest.raises(ValueError, match="smaller"):
        source_context.capture(root, commit, ["worker.py"])


def test_selections_are_account_and_backend_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("TACIT_CONFIG", str(tmp_path / "config.json"))
    identity = ["https://one.example", "human-one"]
    monkeypatch.setattr(config, "profile", lambda: tuple(identity))
    config.save("workspace-one", "repo-one")
    assert config.repository() == "repo-one"
    identity[1] = "human-two"
    assert config.repository() is None
    config.save("workspace-two", "repo-two")
    assert config.repository() == "repo-two"
    identity[0] = "https://other.example"
    assert config.repository() is None
    identity[:] = ["https://one.example", "human-one"]
    assert config.repository() == "repo-one"


def test_selections_symlink_cannot_read_or_replace_other_file(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text('{"private":"unchanged"}')
    link = tmp_path / "config.json"
    link.symlink_to(target)
    monkeypatch.setenv("TACIT_CONFIG", str(link))
    monkeypatch.setattr(config, "profile", lambda: ("https://one.example", "human"))
    assert config.repository() is None
    with pytest.raises(Exception, match="symlink"):
        config.save("workspace", "repo")
    assert target.read_text() == '{"private":"unchanged"}'


@pytest.mark.parametrize(
    "args",
    [
        ["context", "--repo", "repo", "--snapshot", "snap", "--commit", "commit", "--cursor", "-1"],
        ["context", "--repo", "repo", "--snapshot", "snap", "--commit", "commit", "--limit", "0"],
        ["context", "--repo", "repo", "--snapshot", "snap", "--commit", "commit", "--limit", "101"],
    ],
)
def test_cli_invalid_pagination_never_calls_api(monkeypatch, args):
    import tacit_cli.main as cli

    monkeypatch.setattr(cli, "run", lambda *a, **kw: pytest.fail("Invalid input reached API"))
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0
    assert not result.stdout


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "get_context",
            {"repository_id": "r", "snapshot_id": "s", "source_commit": "c", "cursor": -1},
        ),
        (
            "get_context",
            {"repository_id": "r", "snapshot_id": "s", "source_commit": "c", "limit": 101},
        ),
        (
            "evaluate_scenario",
            {"repository_id": "r", "snapshot_id": "s", "source_commit": "c", "facts": []},
        ),
        (
            "evaluate_scenario",
            {
                "repository_id": "r",
                "snapshot_id": "s",
                "source_commit": "c",
                "facts": {"lease_expired": 0.5},
            },
        ),
        (
            "ask_repository",
            {
                "repository_id": "r",
                "snapshot_id": "s",
                "source_commit": "c",
                "question": "why",
                "reason": True,
            },
        ),
        ("confirm", {"draft_id": "not-a-tool"}),
    ],
)
def test_mcp_malformed_requests_are_errors_before_client(monkeypatch, name, arguments):
    import tacit_mcp.server as mcp

    monkeypatch.setattr(
        mcp, "TacitClient", lambda *a, **kw: pytest.fail("Invalid tool reached client")
    )
    with pytest.raises(ToolError):
        asyncio.run(create_server().call_tool(name, arguments))


def test_source_capture_bounds_committed_blob_and_normalizes_git_failure(checkout):
    root, commit = checkout
    (root / "worker.py").write_bytes(b"x" * 65537)
    subprocess.run(["git", "add", "worker.py"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "large committed blob",
        ],
        cwd=root,
        check=True,
    )
    large = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    (root / "worker.py").write_bytes(b"small dirty local copy")
    with pytest.raises(ValueError, match="large committed"):
        source_context.capture(root, large, ["worker.py"])
    with pytest.raises(ValueError, match="Cannot read"):
        source_context.capture(root, "f" * 40, ["worker.py"])
    assert source_context.capture(root, commit, ["worker.py"])[0].dirty_sha256 is not None


READ_TOOLS = [
    ("list_workspaces", {}),
    ("list_repositories", {"workspace_id": "demo"}),
    (
        "get_context",
        {
            "repository_id": "scheduler",
            "snapshot_id": "snapshot-1",
            "source_commit": "demo-commit-1",
        },
    ),
    (
        "ask_repository",
        {
            "repository_id": "scheduler",
            "snapshot_id": "snapshot-1",
            "source_commit": "demo-commit-1",
            "question": "Why?",
        },
    ),
    (
        "evaluate_scenario",
        {
            "repository_id": "scheduler",
            "snapshot_id": "snapshot-1",
            "source_commit": "demo-commit-1",
            "facts": {"lease_expired": True},
        },
    ),
    (
        "get_decision_history",
        {
            "repository_id": "scheduler",
            "snapshot_id": "snapshot-1",
            "decision_id": "lease-publication",
        },
    ),
]


@pytest.mark.parametrize("name,arguments", READ_TOOLS)
def test_all_mcp_reads_use_delegation_and_never_speech(monkeypatch, name, arguments):
    import tacit_mcp.server as mcp
    from fastapi.testclient import TestClient
    from tacit_api.app import Settings, create_app

    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("TACIT_AGENT_TOKEN", "demo-agent")
    monkeypatch.setenv("TACIT_TOKEN", "must-not-be-used")
    calls = []
    with TestClient(create_app(Settings(demo=True))) as backend:

        def respond(request):
            calls.append((request.method, request.url.path, request.headers.get("Authorization")))
            response = backend.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        def client(*, agent):
            assert agent is True
            return TacitClient(agent=agent, transport=httpx.MockTransport(respond))

        monkeypatch.setattr(mcp, "TacitClient", client)
        _, result = asyncio.run(create_server().call_tool(name, arguments))
    assert result is not None
    assert all(token == "Bearer demo-agent" for _, _, token in calls)
    assert all(
        not any(word in path for word in ("voice", "audio", "teaching")) for _, path, _ in calls
    )
    encoded = json.dumps(result)
    assert "must-not-be-used" not in encoded
    if "usage" in result:
        assert all(value == 0 for value in result["usage"].values())


@pytest.mark.parametrize("name,arguments", READ_TOOLS)
def test_all_mcp_reads_cannot_fall_back_to_human(monkeypatch, name, arguments):
    import keyring

    monkeypatch.delenv("TACIT_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("TACIT_TOKEN", "human-must-not-be-used")
    monkeypatch.setattr(keyring, "get_password", lambda *a: pytest.fail("Human keyring accessed"))
    with pytest.raises(ToolError, match="TACIT_AGENT_TOKEN"):
        asyncio.run(create_server().call_tool(name, arguments))
