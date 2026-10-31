"""Offline local-volume recovery bundles; run through recovery.sh on the operator host.

No operation starts or stops deployment writers. All actions fail closed for S3.
Bundle creation/restore are explicit operator actions, never API or worker jobs.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ROOT = Path(__file__).resolve().parents[1]
VOLUME = Path("/var/lib/tacit/artifacts")
FORMAT = "tacit-local-recovery-v1"
PAYLOADS = ("database.dump", "artifacts.tar", "credential-key.fernet")
MAX_FILES = 100_000
MAX_BYTES = 100 * 1024**3
EMPTY_SQL = """
SELECT
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
 (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema')) +
 (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
 (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
  WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema');
"""


class RecoveryError(RuntimeError):
    pass


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RecoveryError("Private directories must not be symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RecoveryError("Directory must be owned by the current user with mode 700")


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def passphrase_key(salt: bytes) -> bytes:
    value = os.environ.get("TACIT_RECOVERY_PASSPHRASE", "")
    if len(value) < 16:
        raise RecoveryError("Set a recovery passphrase of at least 16 characters in the shell")
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(value.encode())


def configured_key() -> bytes:
    if os.environ.get("TACIT_ARTIFACT_BUCKET", ""):
        raise RecoveryError(
            "S3 storage requires an external versioned object/key recovery procedure"
        )
    value = os.environ.get("TACIT_CREDENTIAL_ENCRYPTION_KEY", "").encode()
    if not value:
        raise RecoveryError("Configure the stable credential encryption key before recovery")
    Fernet(value)
    return value


def compose(*args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "deploy/compose.yaml"),
    ]
    project = os.environ.get("TACIT_COMPOSE_PROJECT", "")
    if project:
        command += ["--project-name", project]
    return command + list(args)


def invoke(args: list[str], *, source=None, destination=None) -> bytes:
    result = subprocess.run(
        args, stdin=source, stdout=destination or subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode:
        # Docker/psql errors may reflect configuration; never echo their payload.
        raise RecoveryError("Deployment recovery command failed; writers must remain stopped")
    return result.stdout or b""


def quiescent() -> None:
    services = invoke(compose("ps", "--status", "running", "--services")).decode().splitlines()
    if {"api", "worker", "migrate"}.intersection(services):
        raise RecoveryError("Stop API, worker and migration writers before recovery operations")
    if "postgres" not in services:
        raise RecoveryError("Start only the intended target PostgreSQL service first")


def volume(action: str, identity: str = "", *, source=None, destination=None) -> bytes:
    arguments = [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "/app/.venv/bin/python",
        "api",
        "/app/scripts/recovery.py",
        action,
    ]
    if identity:
        arguments += [identity]
    return invoke(compose(*arguments), source=source, destination=destination)


def safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = PurePosixPath(member.name)
    if (
        name.is_absolute()
        or ".." in name.parts
        or "\\" in member.name
        or not name.parts
        or name.parts[0].startswith(".tacit-recovery-")
        or not (member.isfile() or member.isdir())
    ):
        raise RecoveryError("Artifact archive contains unsafe paths or nonregular objects")
    return name


def archive_inventory(path: Path) -> tuple[int, int]:
    count = total = 0
    names = set()
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = str(safe_member(member))
            if name in names:
                raise RecoveryError("Duplicate artifact archive entry")
            names.add(name)
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_BYTES or member.size > 64 * 1024**2:
                raise RecoveryError("Artifact archive exceeds recovery bounds")
    return count, total


def activation_inventory(directory: Path) -> dict[str, dict[str, object]]:
    """Bind staged files so a retry cannot confuse activation with lost audio."""
    inventory = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise RecoveryError("Recovery stage contains unsafe objects")
        relative = path.relative_to(directory).as_posix()
        inventory[relative] = (
            {"kind": "directory"}
            if path.is_dir()
            else {"kind": "file", "bytes": path.stat().st_size, "sha256": file_digest(path)}
        )
    return inventory


def update_receipt(receipt: Path, state: dict) -> None:
    pending = receipt.with_name("receipt-" + uuid4().hex + ".next")
    try:
        write_private(pending, canonical(state))
        pending.replace(receipt)
    finally:
        pending.unlink(missing_ok=True)


def verify_activation(inventory: dict, directory: Path) -> None:
    for relative, expected in inventory.items():
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts or not parts.parts:
            raise RecoveryError("Invalid recovery activation inventory")
        path = directory.joinpath(*parts.parts)
        if path.is_symlink():
            raise RecoveryError("Activated recovery objects changed")
        if expected == {"kind": "directory"}:
            if not path.is_dir():
                raise RecoveryError("Activated recovery objects are missing")
        elif (
            expected.get("kind") != "file"
            or not path.is_file()
            or path.stat().st_size != expected.get("bytes")
            or file_digest(path) != expected.get("sha256")
        ):
            raise RecoveryError("Activated recovery objects changed or are missing")


def backup(directory: Path) -> None:
    key = configured_key()
    derived = passphrase_key(salt := os.urandom(16))
    quiescent()
    private_directory(directory)
    identity = uuid4().hex
    name = "tacit-recovery-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + identity
    pending = directory / ("." + name + ".partial")
    pending.mkdir(mode=0o700)
    try:
        with (pending / "database.dump").open("xb") as dump:
            invoke(
                compose("exec", "-T", "postgres", "pg_dump", "-U", "tacit", "-d", "tacit", "-Fc"),
                destination=dump,
            )
        with (pending / "database.dump").open("rb") as dump:
            invoke(compose("exec", "-T", "postgres", "pg_restore", "--list"), source=dump)
        with (pending / "artifacts.tar").open("xb") as archive:
            volume("volume-export", destination=archive)
        entries, size = archive_inventory(pending / "artifacts.tar")
        write_private(
            pending / "credential-key.fernet",
            Fernet(base64.urlsafe_b64encode(derived)).encrypt(key),
        )
        manifest = {
            "format": FORMAT,
            "id": identity,
            "created_at": datetime.now(UTC).isoformat(),
            "storage": "compose-local-artifacts",
            "scrypt_salt": base64.b64encode(salt).decode(),
            "key_sha256": hashlib.sha256(key).hexdigest(),
            "artifact_entries": entries,
            "artifact_bytes": size,
            "payloads": {
                name: {
                    "sha256": file_digest(pending / name),
                    "bytes": (pending / name).stat().st_size,
                }
                for name in PAYLOADS
            },
        }
        manifest["authentication"] = hmac.new(derived, canonical(manifest), "sha256").hexdigest()
        write_private(pending / "manifest.json", canonical(manifest))
        quiescent()
        # No overwrite: the destination includes an unpredictable operation UUID.
        destination = directory / name
        if destination.exists():
            raise RecoveryError("Recovery bundle destination already exists")
        pending.rename(destination)
        print("Local recovery bundle created:", destination)
        print("Keep its passphrase separately. This bundle contains private database knowledge.")
    except BaseException:
        shutil.rmtree(pending)
        raise


def read_bundle(directory: Path) -> tuple[dict, bytes]:
    private_directory(directory)
    if {item.name for item in directory.iterdir()} != {*PAYLOADS, "manifest.json"}:
        raise RecoveryError("Bundle contents do not match the recovery format")
    for name in (*PAYLOADS, "manifest.json"):
        path = directory / name
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RecoveryError("Bundle files must be private regular files")
    if (directory / "manifest.json").stat().st_size > 65536:
        raise RecoveryError("Recovery manifest exceeds its size bound")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest.get("format") != FORMAT or not re.fullmatch(
        r"[0-9a-f]{32}", manifest.get("id", "")
    ):
        raise RecoveryError("Unknown recovery bundle format")
    salt = base64.b64decode(manifest["scrypt_salt"], validate=True)
    if len(salt) != 16:
        raise RecoveryError("Invalid recovery salt")
    derived = passphrase_key(salt)
    authentication = manifest.pop("authentication")
    if not hmac.compare_digest(
        authentication, hmac.new(derived, canonical(manifest), "sha256").hexdigest()
    ):
        raise RecoveryError("Recovery manifest authentication failed")
    for name in PAYLOADS:
        path = directory / name
        expected = manifest["payloads"][name]
        if path.stat().st_size != expected["bytes"] or not hmac.compare_digest(
            file_digest(path), expected["sha256"]
        ):
            raise RecoveryError("Recovery payload checksum mismatch")
    key = Fernet(base64.urlsafe_b64encode(derived)).decrypt(
        (directory / "credential-key.fernet").read_bytes()
    )
    Fernet(key)
    if not hmac.compare_digest(hashlib.sha256(key).hexdigest(), manifest["key_sha256"]):
        raise RecoveryError("Recovered encryption key does not match the manifest")
    if archive_inventory(directory / "artifacts.tar") != (
        manifest["artifact_entries"],
        manifest["artifact_bytes"],
    ):
        raise RecoveryError("Artifact inventory does not match its manifest")
    return manifest, key


def restore(
    directory: Path,
    acknowledgement: str,
    *,
    finish: bool = False,
    discard: bool = False,
    committed_confirmation: str | None = None,
) -> None:
    if acknowledgement != "empty-development":
        raise RecoveryError("Explicit --target empty-development acknowledgement is required")
    key = configured_key()
    manifest, recovered = read_bundle(directory)
    if not hmac.compare_digest(key, recovered):
        raise RecoveryError("Target encryption key differs; recover it privately before restoring")
    quiescent()
    identity = manifest["id"]
    if finish:
        if committed_confirmation is not None:
            if not hmac.compare_digest(committed_confirmation, identity):
                raise RecoveryError(
                    "Database-commit acknowledgement must match the bundle manifest id"
                )
            # Explicit operator recovery after independently inspecting an
            # uncertain commit; this is never inferred from a nonempty DB.
            volume("volume-committed", identity)
        volume("volume-finish", identity)
        print("Previously committed recovery finished. Writers remain stopped.")
        return
    existing = invoke(
        compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "tacit",
            "-d",
            "tacit",
            "-Atc",
            EMPTY_SQL,
        )
    ).strip()
    if existing != b"0":
        raise RecoveryError("Refusing to restore over an existing database schema")
    if discard:
        volume("volume-discard", identity)
        print("Uncommitted recovery stage discarded after confirming the database is empty.")
        return
    with (directory / "database.dump").open("rb") as dump:
        invoke(compose("exec", "-T", "postgres", "pg_restore", "--list"), source=dump)
    with (directory / "artifacts.tar").open("rb") as archive:
        volume("volume-stage", identity, source=archive)
    try:
        quiescent()
        with (directory / "database.dump").open("rb") as dump:
            invoke(
                compose(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "tacit",
                    "-d",
                    "tacit",
                    "--single-transaction",
                    "--exit-on-error",
                ),
                source=dump,
            )
    except RecoveryError:
        # A lost docker connection may leave the database outcome uncertain.
        # Never delete the matching encrypted objects on an uncertain commit.
        raise RecoveryError(
            "Database restore did not confirm success; stage retained. Keep writers stopped and inspect the database before retrying."
        ) from None
    # A separate receipt keeps a failed filesystem activation recoverable with
    # `finish`. This is deliberately not a cross-store atomicity claim.
    volume("volume-committed", identity)
    volume("volume-finish", identity)
    print("Database and local encrypted artifacts restored. Writers remain stopped.")


def container_action(action: str, identity: str | None) -> None:
    configured_key()
    VOLUME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if action == "volume-export":
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            for path in sorted(VOLUME.rglob("*")):
                relative = path.relative_to(VOLUME)
                if len(relative.parts) == 1 and re.fullmatch(
                    r"\.tacit-recovery-completed-[0-9a-f]{32}\.json", path.name
                ):
                    if path.is_symlink() or not path.is_file():
                        raise RecoveryError("Invalid completed recovery receipt")
                    continue  # Local activation receipts are not application artifacts.
                if (
                    path.is_symlink()
                    or not (path.is_file() or path.is_dir())
                    or relative.parts[0].startswith(".tacit-recovery-")
                ):
                    raise RecoveryError("Volume contains unsafe objects or an unfinished restore")
                archive.add(path, arcname=relative.as_posix(), recursive=False)
        return
    if not identity or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise RecoveryError("Invalid recovery operation identifier")
    stage = VOLUME / (".tacit-recovery-" + identity)
    receipt = stage / "receipt.json"
    completed = VOLUME / (".tacit-recovery-completed-" + identity + ".json")
    if completed.exists() or completed.is_symlink():
        if completed.is_symlink() or not completed.is_file():
            raise RecoveryError("Invalid completed recovery receipt")
        state = json.loads(completed.read_bytes())
        if state.get("id") != identity or state.get("database_restored") is not True:
            raise RecoveryError("Completed recovery receipt identity mismatch")
        if action != "volume-finish":
            raise RecoveryError("Recovery is already committed and activated")
        verify_activation(state["inventory"], VOLUME)
        if stage.is_symlink():
            raise RecoveryError("Recovery stage must not be a symlink")
        if stage.exists():
            shutil.rmtree(stage)
        return
    if action == "volume-stage":
        if any(VOLUME.iterdir()):
            raise RecoveryError("Artifact target must be completely empty")
        stage.mkdir(mode=0o700)
        files = stage / "files"
        files.mkdir(mode=0o700)
        try:
            count = total = 0
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
                for member in archive:
                    name = safe_member(member)
                    count += 1
                    total += member.size
                    if count > MAX_FILES or total > MAX_BYTES or member.size > 64 * 1024**2:
                        raise RecoveryError("Artifact archive exceeds recovery bounds")
                    target = files.joinpath(*name.parts)
                    if member.isdir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        source = archive.extractfile(member)
                        if source is None:
                            raise RecoveryError("Missing artifact payload")
                        with source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
            write_private(
                receipt,
                canonical(
                    {
                        "id": identity,
                        "database_restored": False,
                        "inventory": activation_inventory(files),
                    }
                ),
            )
        except BaseException:
            shutil.rmtree(stage)
            raise
        return
    if stage.is_symlink() or receipt.is_symlink() or not receipt.is_file():
        raise RecoveryError("Matching recovery stage is unavailable")
    state = json.loads(receipt.read_bytes())
    if state.get("id") != identity:
        raise RecoveryError("Recovery stage identity mismatch")
    if action == "volume-discard":
        if state.get("database_restored") or set(VOLUME.iterdir()) != {stage}:
            raise RecoveryError("Cannot discard a committed or externally modified stage")
        shutil.rmtree(stage)
    elif action == "volume-committed":
        update_receipt(receipt, {**state, "database_restored": True})
    elif action == "volume-finish":
        if state.get("database_restored") is not True:
            raise RecoveryError("Database commit is unconfirmed; operator inspection is required")
        files = stage / "files"
        for item in list(files.iterdir()):
            destination = VOLUME / item.name
            if destination.exists() or destination.is_symlink():
                raise RecoveryError("Refusing to replace an existing artifact target")
            item.rename(destination)
        verify_activation(state["inventory"], VOLUME)
        # A durable receipt outside the directory being deleted makes activation
        # retryable even when cleanup stops after deleting its internal metadata.
        write_private(completed, canonical(state))
        shutil.rmtree(stage)
    else:
        raise RecoveryError("Unknown volume recovery action")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("backup").add_argument("--directory", type=Path, required=True)
    for action in ("restore", "finish", "discard"):
        command = commands.add_parser(action)
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--target", required=True)
        if action == "finish":
            command.add_argument(
                "--acknowledge-database-committed",
                dest="committed_confirmation",
                help="Exact bundle id, only after independently confirming the database restore committed",
            )
    command = commands.add_parser("recover-key")
    command.add_argument("--bundle", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    for action in (
        "volume-export",
        "volume-stage",
        "volume-discard",
        "volume-committed",
        "volume-finish",
    ):
        command = commands.add_parser(action)
        if action != "volume-export":
            command.add_argument("identity")
    args = parser.parse_args()
    try:
        if args.action.startswith("volume-"):
            container_action(args.action, getattr(args, "identity", None))
        elif args.action == "backup":
            backup(args.directory)
        elif args.action == "recover-key":
            if os.environ.get("TACIT_ARTIFACT_BUCKET", ""):
                raise RecoveryError("S3 recovery requires its external object/key procedure")
            _, key = read_bundle(args.bundle)
            private_directory(args.output.parent)
            write_private(args.output, b"TACIT_CREDENTIAL_ENCRYPTION_KEY=" + key + b"\n")
            print(
                "Stable key written to the requested private file; merge it privately into target configuration."
            )
        else:
            restore(
                args.bundle,
                args.target,
                finish=args.action == "finish",
                discard=args.action == "discard",
                committed_confirmation=getattr(args, "committed_confirmation", None),
            )
    except (Exception, KeyboardInterrupt) as error:
        message = (
            str(error)
            if isinstance(error, RecoveryError)
            else "Recovery interrupted or invalid input; keep writers stopped and inspect the stage before retrying"
        )
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
