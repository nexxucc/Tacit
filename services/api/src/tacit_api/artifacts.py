"""Encrypted local/S3 artifacts, author-scoped proxy delivery and deletion tombstones."""

import hashlib
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_domain.access import DomainError, Principal

from tacit_api.access import now, valid_time
from tacit_api.db import Artifact, User
from tacit_api.publication import PublicationService


class Objects:
    def __init__(self, directory: str, bucket: str = "", endpoint: str = ""):
        self.root = Path(directory).expanduser().resolve()
        self.bucket, self.endpoint = bucket, endpoint

    def client(self) -> Any:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint or None,
            config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}),
        )

    def local(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise ValueError("Invalid object key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid object path")
        return path

    def put(self, key: str, data: bytes) -> None:
        if self.bucket:
            self.client().put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType="application/octet-stream"
            )
        else:
            path = self.local(key)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.parent.chmod(0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())

    def get(self, key: str) -> bytes:
        if self.bucket:
            stream = self.client().get_object(Bucket=self.bucket, Key=key)["Body"]
            try:
                data = stream.read(40000001)
            finally:
                stream.close()
        else:
            with self.local(key).open("rb") as file:
                data = file.read(40000001)
        if not isinstance(data, bytes):
            raise ValueError("Object storage returned invalid bytes")
        if len(data) > 40000000:
            raise ValueError("Artifact exceeds bound")
        return data

    def delete(self, key: str) -> None:
        if self.bucket:
            self.client().delete_object(Bucket=self.bucket, Key=key)
        else:
            self.local(key).unlink(missing_ok=True)


class Artifacts:
    def __init__(self, manager: PublicationService, objects: Objects, retention: int):
        self.manager, self.objects, self.retention = manager, objects, retention

    def scoped(self, db: Session, p: Principal, rid: str, token: str, aid: str) -> Artifact:
        repo = self.manager.write_repository(db, p, rid, token)
        item = db.get(Artifact, (repo.workspace_id, rid, aid), with_for_update=True)
        if item is None or item.author_id != p.user_id:
            raise DomainError("not_found", "Artifact unavailable", 404)
        return item

    def create(
        self, p: Principal, rid: str, token: str, data: bytes, content_type: str = "audio/wav"
    ) -> dict[str, object]:
        if not 0 < len(data) <= 28640044:
            raise DomainError("artifact_limit", "Artifact is too large", 413)
        aid, key = str(uuid4()), uuid4().hex
        encrypted = self.manager.access.cipher.encrypt(data)
        # A committed pending record makes interrupted uploads discoverable for cleanup.
        with Session(self.manager.engine) as db, db.begin():
            db.scalar(select(User).where(User.id == p.user_id).with_for_update(key_share=True))
            repo = self.manager.write_repository(db, p, rid, token)
            count = len(
                list(
                    db.scalars(
                        select(Artifact.id)
                        .where(
                            Artifact.author_id == p.user_id,
                            Artifact.deleted_at.is_(None),
                            Artifact.expires_at > now(),
                        )
                        .limit(10)
                    )
                )
            )
            if count >= 10:
                raise DomainError("artifact_quota", "Delete a retained artifact first", 409)
            db.add(
                Artifact(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    id=aid,
                    author_id=p.user_id,
                    object_key=key,
                    sha256=hashlib.sha256(data).hexdigest(),
                    content_type=content_type,
                    bytes=len(data),
                    status="pending",
                    expires_at=now() + timedelta(seconds=300),
                )
            )
        self.objects.put(key, encrypted)
        fresh = self.manager.access.principal(token)
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, fresh, rid, token, aid)
            if item.deleted_at:
                raise DomainError("artifact_deleted", "Artifact upload was cancelled", 410)
            item.status = "ready"
            item.expires_at = now() + timedelta(seconds=self.retention)
        return {
            "artifact_id": aid,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "expires_in": self.retention,
        }

    def read(self, p: Principal, rid: str, token: str, aid: str) -> bytes:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, aid)
            if item.deleted_at or item.status != "ready" or valid_time(item.expires_at) <= now():
                raise DomainError("artifact_deleted", "Artifact expired or deleted", 410)
            data = self.manager.access.cipher.decrypt(self.objects.get(item.object_key))
            if len(data) != item.bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                raise DomainError("artifact_corrupt", "Artifact failed integrity validation", 409)
            return data

    def delete(self, p: Principal, rid: str, token: str, aid: str) -> None:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, aid)
            self.objects.delete(item.object_key)
            item.deleted_at = now()

    def prune(self) -> int:
        with Session(self.manager.engine) as db, db.begin():
            items = list(
                db.scalars(
                    select(Artifact)
                    .where(Artifact.expires_at <= now(), Artifact.deleted_at.is_(None))
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            )
            for item in items:
                self.objects.delete(item.object_key)
                item.deleted_at = now()
            return len(items)
