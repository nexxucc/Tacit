"""Installer adversarial checks with fake tools: no user installation or policy edits."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def fixture_install(tmp_path):
    checkout = tmp_path / "Tacit ü ' __PROJECT__ checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "install-macos.sh"
    shutil.copyfile(ROOT / "scripts/install-macos.sh", script)
    tools = tmp_path / "tool ' directory"
    tools.mkdir()
    calls = tmp_path / "calls.jsonl"
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "with open(os.environ['UV_CALLS'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1]=='sync': sys.exit(int(os.environ.get('SYNC_EXIT','0')))\n"
        "sys.exit(int(os.environ.get('RUN_EXIT','0')))\n"
    )
    uv.chmod(0o755)
    uname = tools / "uname"
    uname.write_text('#!/bin/sh\nprintf "%s\\n" "${FIXTURE_OS:-Darwin}"\n')
    uname.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TACIT_")}
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_BIN_HOME=str(tmp_path / "launch ü directory"),
        PATH=str(tools) + ":/usr/bin:/bin",
        UV_CALLS=str(calls),
    )
    return script, env, calls, tools


def invoke(script, env, *args):
    return subprocess.run(
        ["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, timeout=10
    )


@pytest.mark.parametrize("audio", [False, True])
def test_macos_launchers_preserve_literal_paths_empty_unicode_and_exit(tmp_path, audio):
    script, env, calls, tools = fixture_install(tmp_path)
    result = invoke(script, env, *(["--audio"] if audio else []))
    assert result.returncode == 0, result.stderr
    assert json.loads(calls.read_text().splitlines()[0]) == [
        "sync",
        "--frozen",
        "--package",
        "tacit-cli",
        "--package",
        "tacit-mcp",
        "--no-default-groups",
        *(["--extra", "audio"] if audio else []),
    ]
    # A different cwd and hostile shell-looking arguments remain literal argv.
    values = ["ask", "", "two words", "quote'\"", "$(touch NEVER)", "ü", "trail\\"]
    for name, package, entrypoint in [
        ("tacit-intent", "tacit-cli", "tacit"),
        ("tacit-mcp", "tacit-mcp", "tacit-mcp"),
    ]:
        launcher = Path(env["XDG_BIN_HOME"]) / name
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
        run = subprocess.run(
            [str(launcher), *values],
            cwd=tmp_path,
            env={**env, "RUN_EXIT": "7"},
            capture_output=True,
            timeout=10,
        )
        assert run.returncode == 7
        assert json.loads(calls.read_text().splitlines()[-1]) == [
            "run",
            "--project",
            str(script.parent.parent),
            "--package",
            package,
            "--no-sync",
            entrypoint,
            *values,
        ]
    assert not (tmp_path / "NEVER").exists()
    assert (tools / "uv").exists()


@pytest.mark.parametrize("name", ["tacit-intent", "tacit-mcp"])
@pytest.mark.parametrize("kind", ["file", "symlink", "path"])
def test_macos_collisions_refused_before_sync(tmp_path, name, kind):
    script, env, calls, tools = fixture_install(tmp_path)
    target = (tools if kind == "path" else Path(env["XDG_BIN_HOME"])) / name
    target.parent.mkdir(exist_ok=True)
    if kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    else:
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    result = invoke(script, env)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not calls.exists()
    assert os.path.lexists(target)


@pytest.mark.parametrize("args", [["--unknown"], ["--audio", "unexpected"]])
def test_macos_unknown_flags_do_not_sync(tmp_path, args):
    script, env, calls, _ = fixture_install(tmp_path)
    result = invoke(script, env, *args)
    assert result.returncode == 2 and "Usage:" in result.stderr
    assert not calls.exists()


def test_macos_refuses_wrong_platform_and_missing_uv(tmp_path):
    script, env, calls, tools = fixture_install(tmp_path)
    result = invoke(script, {**env, "FIXTURE_OS": "Linux"})
    assert result.returncode == 1 and "native macOS" in result.stderr
    assert not calls.exists()
    (tools / "uv").unlink()
    (tools / "dirname").symlink_to("/usr/bin/dirname")
    result = invoke(script, {**env, "PATH": str(tools)})
    assert result.returncode == 1 and "Install uv" in result.stderr
    assert not calls.exists()


def test_macos_failed_sync_leaves_no_launchers(tmp_path):
    script, env, _, _ = fixture_install(tmp_path)
    result = invoke(script, {**env, "SYNC_EXIT": "8"})
    assert result.returncode == 8
    assert not Path(env["XDG_BIN_HOME"]).exists()


def test_macos_second_launcher_failure_rolls_back_only_owned_files(tmp_path):
    script, env, calls, tools = fixture_install(tmp_path)
    chmod = tools / "chmod"
    chmod.write_text(
        '#!/bin/sh\ncase "$3" in */tacit-mcp) exit 9;; esac\nexec /usr/bin/chmod "$@"\n'
    )
    chmod.chmod(0o755)
    output = Path(env["XDG_BIN_HOME"])
    output.mkdir()
    unrelated = output / "keep-me"
    unrelated.write_text("user content")
    result = invoke(script, env)
    assert result.returncode == 9
    assert calls.exists()
    assert unrelated.read_text() == "user content"
    assert not (output / "tacit-intent").exists()
    assert not (output / "tacit-mcp").exists()


def test_macos_post_preflight_collision_preserves_competing_file(tmp_path):
    script, env, _, tools = fixture_install(tmp_path)
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os\nfrom pathlib import Path\n"
        "p=Path(os.environ['XDG_BIN_HOME']); p.mkdir(parents=True)\n"
        "(p/'tacit-mcp').write_text('competing installation')\n"
    )
    result = invoke(script, env)
    assert result.returncode != 0
    output = Path(env["XDG_BIN_HOME"])
    assert not (output / "tacit-intent").exists()
    assert (output / "tacit-mcp").read_text() == "competing installation"
