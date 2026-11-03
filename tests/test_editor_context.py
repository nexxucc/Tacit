"""Local editor capture and source-binding regressions; no providers."""

import hashlib
import json
import subprocess

import pytest
from tacit_cli.editor import app, load_context
from typer.testing import CliRunner


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root).decode().strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (root / "a.py").write_text("def example():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    return root, git("rev-parse", "HEAD")


def capture(checkout, tmp_path, *extra):
    root, commit = checkout
    output = tmp_path / "selection.json"
    result = CliRunner().invoke(
        app,
        [
            "--root",
            str(root),
            "--commit",
            commit,
            "--file",
            "a.py",
            "--output",
            str(output),
            "--end-line",
            "2",
            *extra,
        ],
    )
    return result, output


def test_clean_roundtrip(checkout, tmp_path):
    result, output = capture(checkout, tmp_path, "--symbol", "example", "--start-line", "1")
    assert result.exit_code == 0, result.output
    selections = load_context(output, checkout[0], checkout[1], ["a.py"])
    assert selections[0].symbol == "example"
    assert selections[0].dirty_sha256 is None
    assert selections[0].end_line == 2
    assert output.stat().st_mode & 0o077 == 0


def test_dirty_and_stale(checkout, tmp_path):
    source = checkout[0] / "a.py"
    source.write_text("def example():\n    return 2\n")
    result, output = capture(checkout, tmp_path)
    assert result.exit_code == 0
    selections = load_context(output, *checkout, ["a.py"])
    assert selections[0].dirty_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text("def example():\n    return 3\n")
    with pytest.raises(ValueError, match="changed"):
        load_context(output, *checkout, ["a.py"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--start-line", "3"],
        ["--end-line", "401"],
        ["--end-line", "3"],
        ["--expected-sha256", "0" * 64],
        ["--file", "../outside.py"],
        ["--commit", "HEAD"],
        ["--symbol", "x" * 201],
    ],
)
def test_invalid_export(checkout, tmp_path, extra):
    result, output = capture(checkout, tmp_path, *extra)
    assert result.exit_code != 0
    assert not output.exists()


def test_no_overwrite(checkout, tmp_path):
    result, output = capture(checkout, tmp_path)
    original = output.read_bytes()
    result, _ = capture(checkout, tmp_path)
    assert result.exit_code != 0
    assert output.read_bytes() == original


def test_commit_files_and_evidence_bound(checkout, tmp_path):
    _, output = capture(checkout, tmp_path)
    with pytest.raises(ValueError, match="different source commit"):
        load_context(output, checkout[0], "0" * 40, ["a.py"])
    with pytest.raises(ValueError, match="files differ"):
        load_context(output, *checkout, ["b.py"])
    data = json.loads(output.read_text())
    data["selections"][0]["dirty_sha256"] = "0" * 64
    output.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="dirty-file"):
        load_context(output, *checkout, ["a.py"])


def test_symlink_and_subdirectory(checkout, tmp_path):
    root, commit = checkout
    external = tmp_path / "outside.py"
    external.write_text("secret")
    (root / "link.py").symlink_to(external)
    result, output = capture(checkout, tmp_path, "--file", "link.py")
    assert result.exit_code != 0
    assert not output.exists()
    sub = root / "sub"
    sub.mkdir()
    result, _ = capture(checkout, tmp_path, "--root", str(sub))
    assert result.exit_code != 0


def test_teaching_rejects_stale_before_network(checkout, tmp_path, monkeypatch):
    from tacit_cli.teaching import app as teaching

    _, output = capture(checkout, tmp_path)
    (checkout[0] / "a.py").write_text("changed")

    def forbidden(*args, **kwargs):
        pytest.fail("Stale editor context must be rejected before any network call")

    monkeypatch.setattr("tacit_cli.teaching.request", forbidden)
    result = CliRunner().invoke(
        teaching,
        [
            "extract",
            "--repo",
            "example",
            "--commit",
            checkout[1],
            "--file",
            "a.py",
            "--source-root",
            str(checkout[0]),
            "--editor-context",
            str(output),
        ],
    )
    assert result.exit_code == 1
    assert "changed since" in result.output


def test_teaching_passes_exact_editor_selection_after_confirmation(checkout, tmp_path, monkeypatch):
    from tacit_cli.teaching import app as teaching

    _, output = capture(checkout, tmp_path, "--symbol", "example", "--start-line", "2")
    text = tmp_path / "intent.txt"
    text.write_text("Keep this behavior.")
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"snapshot_id": None} if method == "GET" else {"status": "fixture"}

    monkeypatch.setattr("tacit_cli.teaching.request", request)
    result = CliRunner().invoke(
        teaching,
        [
            "extract",
            "--repo",
            "example",
            "--commit",
            checkout[1],
            "--file",
            "a.py",
            "--source-root",
            str(checkout[0]),
            "--editor-context",
            str(output),
            "--text-file",
            str(text),
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    selection = calls[-1][2]["selections"][0]
    assert selection["symbol"] == "example"
    assert selection["start_line"] == 2
    assert selection["end_line"] == 2
    assert selection["dirty_sha256"] is None


def test_package_contains_only_runtime_files(tmp_path):
    import importlib.util
    from pathlib import Path
    from zipfile import ZipFile

    spec = importlib.util.spec_from_file_location(
        "package_editor", Path(__file__).parents[1] / "scripts/package_editor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "test.vsix"
    module.build(output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "extension.vsixmanifest",
            "[Content_Types].xml",
            "extension/package.json",
            "extension/extension.js",
        }
    with pytest.raises(FileExistsError):
        module.build(output)


def test_default_range_captures_actual_file(checkout, tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "--root",
            str(checkout[0]),
            "--commit",
            checkout[1],
            "--file",
            "a.py",
            "--output",
            str(tmp_path / "default.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    context = json.loads((tmp_path / "default.json").read_text())
    assert context["selections"][0]["end_line"] == 2
