"""Checkout installer smoke checks without mutating the user's environment."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from tacit_cli import config


def installer(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    checkout = tmp_path / "Tacit ü checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts/install-arch.sh"
    shutil.copyfile(source, scripts / "install-arch.sh")
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    uv.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >> "$UV_CALLS"\n')
    uv.chmod(0o755)
    calls = tmp_path / "uv-calls"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_BIN_HOME": str(tmp_path / "bin"),
        "PATH": str(tools) + ":/usr/bin:/bin",
        "UV_CALLS": str(calls),
    }
    return scripts / "install-arch.sh", env, calls


@pytest.mark.parametrize("audio", [False, True])
def test_arch_installer_launcher_and_collision(tmp_path, audio):
    script, env, calls = installer(tmp_path)
    args = ["bash", str(script)] + (["--audio"] if audio else [])
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["sync", "--frozen"] + (
        ["--extra", "audio"] if audio else []
    )
    launcher = Path(env["XDG_BIN_HOME"]) / "tacit-intent"
    original = launcher.read_bytes()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
    calls.write_text("")
    run = subprocess.run(
        [str(launcher), "ask", "argument with spaces"], env=env, capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
    assert calls.read_text().splitlines() == [
        "run",
        "--project",
        str(script.parent.parent),
        "--no-sync",
        "tacit",
        "ask",
        "argument with spaces",
    ]
    calls.write_text("")
    retry = subprocess.run(args, env=env, capture_output=True, text=True)
    assert retry.returncode == 1
    assert "already exists" in retry.stderr
    assert launcher.read_bytes() == original
    assert calls.read_text() == ""


def test_arch_installer_refuses_dangling_symlink_before_sync(tmp_path):
    script, env, calls = installer(tmp_path)
    launcher = Path(env["XDG_BIN_HOME"]) / "tacit-intent"
    launcher.parent.mkdir()
    launcher.symlink_to(tmp_path / "missing-target")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert launcher.is_symlink()
    assert not calls.exists()


def test_arch_installer_rejects_unknown_options_without_sync(tmp_path):
    script, env, calls = installer(tmp_path)
    result = subprocess.run(
        ["bash", str(script), "--audoi"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not calls.exists()


def test_arch_installer_refuses_another_launcher_on_path(tmp_path):
    script, env, calls = installer(tmp_path)
    existing = Path(env["PATH"].split(":")[0]) / "tacit-intent"
    existing.write_text("#!/bin/bash\nexit 0\n")
    existing.chmod(0o755)
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert existing.read_text() == "#!/bin/bash\nexit 0\n"
    assert not calls.exists()


def test_arch_installer_does_not_install_after_sync_failure(tmp_path):
    script, env, calls = installer(tmp_path)
    uv = Path(env["PATH"].split(":")[0]) / "uv"
    uv.write_text("#!/bin/bash\nexit 7\n")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 7
    assert not (Path(env["XDG_BIN_HOME"]) / "tacit-intent").exists()


def test_selection_config_preserves_parent_permissions_and_scopes_identity(tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    path = parent / "selection.json"
    monkeypatch.setenv("TACIT_CONFIG", str(path))
    monkeypatch.setattr(config, "profile", lambda: ("https://one.example", "owner"))
    config.save("workspace-one", "repository-one")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.repository() == "repository-one"
    monkeypatch.setattr(config, "profile", lambda: ("https://one.example", "learner"))
    assert config.repository() is None
    config.save("workspace-two", "repository-two")
    monkeypatch.setattr(config, "profile", lambda: ("https://two.example", "learner"))
    assert config.repository() is None
    monkeypatch.setattr(config, "profile", lambda: ("https://one.example", "owner"))
    assert config.repository() == "repository-one"
