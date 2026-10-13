"""Private, atomically acknowledged local audio; tokens are never written to disk."""

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tacit_cli.audio import AudioError

MAX_PCM = 895 * 32000


def root() -> Path:
    path = Path(os.environ.get("TACIT_VOICE_SPOOL", str(Path.home() / ".local/state/tacit/voice")))
    if path.is_symlink():
        raise AudioError("Voice spool must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def capacity_lock() -> Iterator[None]:
    try:
        import fcntl
    except ImportError:
        raise AudioError("Durable audio capture currently requires Linux") from None
    fd = os.open(root() / ".capacity.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic(path: Path, content: bytes) -> None:
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Spool:
    def __init__(self, sid: str):
        self.id = str(UUID(sid))
        self.path = root() / self.id
        if self.path.is_symlink():
            raise AudioError("Voice spool must not be a symlink")
        self.meta: dict[str, Any] = json.loads((self.path / "manifest.json").read_text())
        if self.meta["id"] != self.id:
            raise AudioError("Invalid spool manifest")

    @classmethod
    def create(cls, *, repo: str, source: dict[str, object], server: str, author: str) -> "Spool":
        with capacity_lock():
            return cls._create(repo=repo, source=source, server=server, author=author)

    @classmethod
    def _create(cls, *, repo: str, source: dict[str, object], server: str, author: str) -> "Spool":
        sessions()
        existing = list(root().glob("*/manifest.json"))
        if len(existing) >= 3:
            raise AudioError("Three local recordings are retained; resume or forget one first")
        sid = str(uuid4())
        path = root() / sid
        path.mkdir(mode=0o700)
        atomic(
            path / "manifest.json",
            json.dumps(
                {
                    "id": sid,
                    "repo": repo,
                    "source": source,
                    "server": server,
                    "author": author,
                    "created_at": time.time(),
                    "expires_at": time.time() + 86400,
                    "status": "recording",
                    "capture_complete": False,
                    "chunks": 0,
                    "bytes": 0,
                    "hashes": [],
                    "backend_id": None,
                }
            ).encode(),
        )
        return cls(sid)

    def save(self) -> None:
        atomic(self.path / "manifest.json", json.dumps(self.meta).encode())

    def append(self, pcm: bytes) -> None:
        sequence = self.meta["chunks"]
        if not pcm or len(pcm) > 32000 or len(pcm) % 2 or self.meta["bytes"] + len(pcm) > MAX_PCM:
            raise AudioError("Invalid or over-limit recording chunk")
        atomic(self.path / f"{sequence:04d}.pcm", pcm)
        self.meta["hashes"].append(hashlib.sha256(pcm).hexdigest())
        self.meta["chunks"] += 1
        self.meta["bytes"] += len(pcm)
        self.save()

    def chunk(self, sequence: int) -> bytes:
        if not 0 <= sequence < self.meta["chunks"]:
            raise AudioError("Missing recording chunk")
        path = self.path / f"{sequence:04d}.pcm"
        if path.is_symlink():
            raise AudioError("Invalid recording chunk")
        with path.open("rb") as file:
            pcm = file.read(32001)
        if (
            not pcm
            or len(pcm) > 32000
            or len(pcm) % 2
            or hashlib.sha256(pcm).hexdigest() != self.meta["hashes"][sequence]
        ):
            raise AudioError("Recording chunk failed integrity validation")
        return pcm

    def digest(self) -> str:
        hashed = hashlib.sha256()
        size = 0
        for i in range(self.meta["chunks"]):
            data = self.chunk(i)
            hashed.update(data)
            size += len(data)
        if size != self.meta["bytes"] or size > MAX_PCM:
            raise AudioError("Recording manifest is inconsistent")
        return hashed.hexdigest()

    def forget(self) -> None:
        # Target is one validated UUID child, never an arbitrary caller-supplied path.
        shutil.rmtree(self.path)


def sessions() -> list[dict[str, object]]:
    found = []
    for manifest in root().glob("*/manifest.json"):
        spool = Spool(manifest.parent.name)
        if spool.meta["expires_at"] <= time.time():
            spool.forget()
            continue
        found.append(
            {k: spool.meta[k] for k in ("id", "repo", "status", "chunks", "bytes", "backend_id")}
        )
    return found
