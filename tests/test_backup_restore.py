"""Failure-safe database backup/restore scripts, with no live deployment access."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scripts(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['DOCKER_CALLS'],'a') as out: out.write(json.dumps(args)+'\\n')\n"
        "if 'pg_dump' in args:\n"
        " sys.stdout.buffer.write(b'archive')\n"
        " sys.exit(int(os.environ.get('DUMP_EXIT','0')))\n"
        "if 'pg_restore' in args:\n"
        " assert sys.stdin.buffer.read()==b'archive'\n"
        " if '--list' in args: sys.exit(int(os.environ.get('LIST_EXIT','0')))\n"
        " Path(os.environ['RESTORED']).write_text('restored')\n"
        " sys.exit(0)\n"
        "if 'psql' in args: print(os.environ.get('EXISTING','0'));sys.exit(0)\n"
        "sys.exit(99)\n"
    )
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": str(tools) + ":/usr/bin:/bin",
        "DOCKER_CALLS": str(calls),
        "RESTORED": str(tmp_path / "restored"),
        "TACIT_BACKUP_DIR": str(tmp_path / "private backups"),
        "TACIT_RESTORE_DUMP": str(tmp_path / "input.dump"),
        "TACIT_RESTORE_TARGET": "empty-development",
    }
    Path(env["TACIT_RESTORE_DUMP"]).write_bytes(b"archive")

    def run(name, **overrides):
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / name)],
            cwd=tmp_path,
            env={**env, **overrides},
            capture_output=True,
            text=True,
        )

    return run, env, calls


def test_backup_is_validated_private_unique_and_independent_of_cwd(scripts):
    run, env, calls = scripts
    for _ in range(2):
        result = run("backup.sh")
        assert result.returncode == 0, result.stderr
    folder = Path(env["TACIT_BACKUP_DIR"])
    archives = list(folder.glob("*.dump"))
    assert len(archives) == 2
    assert all(p.read_bytes() == b"archive" for p in archives)
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in archives)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert not list(folder.glob("*.partial"))
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(str(ROOT / "deploy/compose.yaml") in args for args in operations)
    assert all(str(ROOT / ".env") in args for args in operations)
    assert sum("--list" in args for args in operations) == 2


@pytest.mark.parametrize("failure", [{"DUMP_EXIT": "7"}, {"LIST_EXIT": "9"}])
def test_backup_failure_removes_partial_and_never_publishes_dump(scripts, failure):
    run, env, _ = scripts
    result = run("backup.sh", **failure)
    assert result.returncode != 0
    assert list(Path(env["TACIT_BACKUP_DIR"]).iterdir()) == []
    assert "Database backup:" not in result.stdout


def test_backup_refuses_unprivate_directory_without_changing_it(scripts):
    run, env, calls = scripts
    folder = Path(env["TACIT_BACKUP_DIR"])
    folder.mkdir(mode=0o755)
    result = run("backup.sh")
    assert result.returncode != 0
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
    assert not calls.exists()


def test_restore_is_transactional_and_checks_all_non_system_objects(scripts):
    run, env, calls = scripts
    result = run("restore.sh")
    assert result.returncode == 0, result.stderr
    assert Path(env["RESTORED"]).exists()
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert "--list" in operations[0]
    query = next(args[-1] for args in operations if "psql" in args)
    assert all(table in query for table in ["pg_class", "pg_namespace", "pg_proc", "pg_type"])
    assert "--single-transaction" in operations[-1]
    assert "--exit-on-error" in operations[-1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"EXISTING": "1"},
        {"EXISTING": ""},
        {"LIST_EXIT": "2"},
        {"TACIT_RESTORE_TARGET": "production"},
    ],
)
def test_restore_refuses_existing_target_invalid_archive_and_wrong_ack(scripts, overrides):
    run, env, _ = scripts
    result = run("restore.sh", **overrides)
    assert result.returncode != 0
    assert not Path(env["RESTORED"]).exists()
