"""Private recovery bundles and interrupted local activation, without deployment access."""

import importlib.util
import io
import json
import os
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

SPEC = importlib.util.spec_from_file_location(
    "tacit_recovery", Path(__file__).parents[1] / "scripts/recovery.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return output.getvalue()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("TACIT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TACIT_RECOVERY_PASSPHRASE", "fixture-private-passphrase")
    monkeypatch.delenv("TACIT_ARTIFACT_BUCKET", raising=False)
    monkeypatch.setattr(recovery, "quiescent", lambda: None)
    payload = archive_bytes(
        [
            ("objects", tarfile.DIRTYPE, b""),
            ("objects/ciphertext", tarfile.REGTYPE, b"encrypted-object"),
        ]
    )

    def invoke(args, *, source=None, destination=None):
        if destination is not None:
            destination.write(b"database-fixture")
        return b""

    def volume(action, identity="", *, source=None, destination=None):
        assert action == "volume-export"
        destination.write(payload)
        return b""

    monkeypatch.setattr(recovery, "invoke", invoke)
    monkeypatch.setattr(recovery, "volume", volume)
    previous = os.umask(0o077)
    try:
        recovery.backup(tmp_path / "backups")
    finally:
        os.umask(previous)
    return next((tmp_path / "backups").iterdir())


def test_bundle_roundtrip_private_and_matching_key(bundle):
    manifest, key = recovery.read_bundle(bundle)
    assert manifest["artifact_entries"] == 2
    assert manifest["artifact_bytes"] == len(b"encrypted-object")
    assert key == recovery.configured_key()
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in bundle.iterdir())
    assert key not in (bundle / "credential-key.fernet").read_bytes()


@pytest.mark.parametrize("name", recovery.PAYLOADS)
def test_payload_tamper_rejected_before_restore(bundle, name):
    with (bundle / name).open("ab") as file:
        file.write(b"changed")
    with pytest.raises(recovery.RecoveryError, match="checksum"):
        recovery.read_bundle(bundle)


def test_manifest_tamper_and_wrong_passphrase(bundle, monkeypatch):
    path = bundle / "manifest.json"
    original = path.read_bytes()
    manifest = json.loads(original)
    manifest["artifact_bytes"] += 1
    path.write_text(json.dumps(manifest))
    with pytest.raises(recovery.RecoveryError, match="authentication"):
        recovery.read_bundle(bundle)
    path.write_bytes(original)
    monkeypatch.setenv("TACIT_RECOVERY_PASSPHRASE", "different-private-passphrase")
    with pytest.raises(recovery.RecoveryError, match="authentication"):
        recovery.read_bundle(bundle)


@pytest.mark.parametrize("change", ["symlink", "public", "extra"])
def test_bundle_rejects_unprivate_or_unexpected_files(bundle, change):
    if change == "symlink":
        target = bundle / "database.dump"
        target.rename(bundle.parent / "outside")
        target.symlink_to(bundle.parent / "outside")
    elif change == "public":
        (bundle / "database.dump").chmod(0o644)
    else:
        (bundle / "unexpected").touch()
    with pytest.raises(recovery.RecoveryError):
        recovery.read_bundle(bundle)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("a\\b", tarfile.REGTYPE),
        (".tacit-recovery-evil/a", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_archive_rejects_escape_links_and_special_objects(tmp_path, name, kind):
    path = tmp_path / "unsafe.tar"
    path.write_bytes(archive_bytes([(name, kind, b"x")]))
    with pytest.raises(recovery.RecoveryError):
        recovery.archive_inventory(path)


def test_archive_rejects_duplicates_and_bounds(tmp_path, monkeypatch):
    path = tmp_path / "data.tar"
    path.write_bytes(archive_bytes([("same", tarfile.REGTYPE, b"x")] * 2))
    with pytest.raises(recovery.RecoveryError, match="Duplicate"):
        recovery.archive_inventory(path)
    path.write_bytes(archive_bytes([("one", tarfile.REGTYPE, b"xx")]))
    monkeypatch.setattr(recovery, "MAX_BYTES", 1)
    with pytest.raises(recovery.RecoveryError, match="bounds"):
        recovery.archive_inventory(path)


@pytest.mark.parametrize(
    "services", [b"api\npostgres\n", b"worker\npostgres\n", b"migrate\npostgres\n", b""]
)
def test_quiescent_requires_only_database(monkeypatch, services):
    monkeypatch.setattr(recovery, "invoke", lambda *args, **kwargs: services)
    with pytest.raises(recovery.RecoveryError):
        recovery.quiescent()


@pytest.mark.parametrize("case", ["target", "key", "database"])
def test_restore_refuses_unsafe_targets_before_staging(bundle, monkeypatch, case):
    operations = []
    monkeypatch.setattr(recovery, "volume", lambda *args, **kwargs: operations.append(args))
    monkeypatch.setattr(
        recovery, "invoke", lambda *args, **kwargs: b"1" if case == "database" else b"0"
    )
    if case == "key":
        monkeypatch.setenv("TACIT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(recovery.RecoveryError):
        recovery.restore(bundle, "wrong-target" if case == "target" else "empty-development")
    assert not operations


def test_uncertain_database_restore_preserves_stage(bundle, monkeypatch):
    actions = []

    def invoke(args, **kwargs):
        if "psql" in args:
            return b"0"
        if "--single-transaction" in args:
            raise recovery.RecoveryError("lost connection")
        return b""

    monkeypatch.setattr(recovery, "invoke", invoke)
    monkeypatch.setattr(recovery, "volume", lambda action, *args, **kwargs: actions.append(action))
    with pytest.raises(recovery.RecoveryError, match="stage retained"):
        recovery.restore(bundle, "empty-development")
    assert actions == ["volume-stage"]


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setenv("TACIT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("TACIT_ARTIFACT_BUCKET", raising=False)
    path = tmp_path / "volume"
    monkeypatch.setattr(recovery, "VOLUME", path)
    identity = "a" * 32
    monkeypatch.setattr(
        recovery.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(archive_bytes([("objects/a", tarfile.REGTYPE, b"ciphertext")]))
        ),
    )
    recovery.container_action("volume-stage", identity)
    return path, identity


def test_uncommitted_stage_cannot_finish_and_can_discard(volume):
    path, identity = volume
    with pytest.raises(recovery.RecoveryError, match="unconfirmed"):
        recovery.container_action("volume-finish", identity)
    recovery.container_action("volume-discard", identity)
    assert list(path.iterdir()) == []


def test_committed_stage_cannot_discard_and_finishes(volume):
    path, identity = volume
    recovery.container_action("volume-committed", identity)
    with pytest.raises(recovery.RecoveryError, match="committed"):
        recovery.container_action("volume-discard", identity)
    recovery.container_action("volume-finish", identity)
    assert (path / "objects/a").read_bytes() == b"ciphertext"
    assert not (path / (".tacit-recovery-" + identity)).exists()


def test_finish_recovers_interruption_after_files_directory_removal(volume, monkeypatch):
    path, identity = volume
    recovery.container_action("volume-committed", identity)
    receipt = path / (".tacit-recovery-" + identity) / "receipt.json"
    original = recovery.shutil.rmtree
    failed = False

    def interrupt(target, *args, **kwargs):
        nonlocal failed
        if target == receipt.parent and not failed:
            failed = True
            (target / "files").rmdir()
            receipt.unlink()
            raise OSError("simulated crash after activation during metadata cleanup")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(recovery.shutil, "rmtree", interrupt)
    with pytest.raises(OSError):
        recovery.container_action("volume-finish", identity)
    recovery.container_action("volume-finish", identity)
    assert (path / "objects/a").read_bytes() == b"ciphertext"
    assert not receipt.parent.exists()


def test_completed_activation_retry_rejects_missing_or_changed_ciphertext(volume):
    path, identity = volume
    recovery.container_action("volume-committed", identity)
    recovery.container_action("volume-finish", identity)
    recovery.container_action("volume-finish", identity)
    (path / "objects/a").write_bytes(b"changed")
    with pytest.raises(recovery.RecoveryError, match="changed"):
        recovery.container_action("volume-finish", identity)


def test_commit_receipt_retry_ignores_stale_old_fixed_temp(volume):
    path, identity = volume
    stage = path / (".tacit-recovery-" + identity)
    (stage / "receipt.next").write_bytes(b"interrupted-old-write")
    recovery.container_action("volume-committed", identity)
    recovery.container_action("volume-committed", identity)
    recovery.container_action("volume-finish", identity)
    assert (path / "objects/a").read_bytes() == b"ciphertext"


def test_nonempty_volume_stage_refused_and_s3_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TACIT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("TACIT_ARTIFACT_BUCKET", raising=False)
    monkeypatch.setattr(recovery, "VOLUME", tmp_path)
    (tmp_path / "existing").write_bytes(b"keep")
    with pytest.raises(recovery.RecoveryError, match="empty"):
        recovery.container_action("volume-stage", "a" * 32)
    assert (tmp_path / "existing").read_bytes() == b"keep"
    monkeypatch.setenv("TACIT_ARTIFACT_BUCKET", "fixture-bucket")
    with pytest.raises(recovery.RecoveryError, match="S3"):
        recovery.configured_key()


def test_completed_receipt_excluded_from_future_artifact_export(volume, monkeypatch):
    path, identity = volume
    recovery.container_action("volume-committed", identity)
    recovery.container_action("volume-finish", identity)
    output = io.BytesIO()
    monkeypatch.setattr(recovery.sys, "stdout", SimpleNamespace(buffer=output))
    recovery.container_action("volume-export", None)
    with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
        assert set(archive.getnames()) == {"objects", "objects/a"}


def test_deployment_command_failure_never_echoes_private_stderr(monkeypatch):
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"private-database-password"
        ),
    )
    with pytest.raises(recovery.RecoveryError) as error:
        recovery.invoke(["fixture-command"])
    assert "private-database-password" not in str(error.value)
