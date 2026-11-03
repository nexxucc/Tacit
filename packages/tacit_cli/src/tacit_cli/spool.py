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
from tacit_cli.local_storage import (
    create_private_file,
    make_private,
    private_directory,
    reject_link,
    reject_redirected_path,
    voice_path,
)

MAX_PCM = 895 * 32000


def root() -> Path:
    path = voice_path()
    try:
        private_directory(path)
    except OSError:
        raise AudioError(
            "Cannot secure the local voice spool; use a private local filesystem directory"
        ) from None
    return path


@contextmanager
def capacity_lock(path: Path | None = None) -> Iterator[None]:
    path = path if path is not None else root() / ".capacity.lock"
    reject_redirected_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        make_private(path)
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                windows_crt: Any = msvcrt
                windows_crt.locking(fd, windows_crt.LK_LOCK, 1)
            except OSError:
                raise AudioError(
                    "Another Tacit process is updating local recordings; retry shortly"
                ) from None
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic(path: Path, content: bytes) -> None:
    reject_link(path)
    reject_redirected_path(path.parent)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with create_private_file(temp) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            windows: Any = ctypes
            kernel = windows.WinDLL("kernel32", use_last_error=True)
            kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            kernel.MoveFileExW.restype = wintypes.BOOL
            # Same-directory replace plus write-through: Windows has no POSIX directory fsync.
            for attempt in range(20):
                if kernel.MoveFileExW(str(temp.absolute()), str(path.absolute()), 0x1 | 0x8):
                    break
                error = windows.get_last_error()
                # An uploader may briefly have the manifest open without Windows
                # delete sharing. Retry only that local handle contention.
                if error not in {32, 33} or attempt == 19:
                    raise windows.WinError(error)
                time.sleep(0.025)
        else:
            os.replace(temp, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class Spool:
    def __init__(self, sid: str):
        self.id = str(UUID(sid))
        self.path = root() / self.id
        manifest = self.path / "manifest.json"
        try:
            reject_link(self.path)
            reject_link(manifest)
        except OSError:
            raise AudioError("Voice spool must not use a symlink or reparse point") from None
        self.meta: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))
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
        private_directory(path)
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
        try:
            reject_link(path)
        except OSError:
            raise AudioError("Invalid recording chunk") from None
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
