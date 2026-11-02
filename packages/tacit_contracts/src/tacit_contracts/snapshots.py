"""Portable published snapshots. Digests establish integrity, not signed authenticity."""

import hashlib
import json
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from tacit_contracts.models import Contract, Decision
from tacit_contracts.versions import API_VERSION, EVALUATOR_VERSION, SCHEMA_VERSION

MAX_EXPORT_BYTES = 4_000_000


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class PortableVersions(Contract):
    api_version: str = API_VERSION
    schema_version: str = SCHEMA_VERSION
    evaluator_version: str = EVALUATOR_VERSION

    @model_validator(mode="after")
    def compatible_versions(self) -> Self:
        if (self.api_version, self.schema_version, self.evaluator_version) != (
            API_VERSION,
            SCHEMA_VERSION,
            EVALUATOR_VERSION,
        ):
            raise ValueError("Incompatible snapshot contract versions")
        return self


class SnapshotExport(PortableVersions):
    format: Literal["tacit-snapshot-v1"] = "tacit-snapshot-v1"
    workspace_id: str = Field(min_length=1, max_length=128)
    repository_id: str = Field(min_length=1, max_length=128)
    repository_name: str = Field(min_length=1, max_length=255)
    snapshot_id: str = Field(min_length=1, max_length=128)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    parent_snapshot_id: str | None
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: tuple[Decision, ...] = Field(max_length=10000)

    @model_validator(mode="after")
    def integrity(self) -> "SnapshotExport":
        revisions = [d.revision_id for d in self.decisions]
        if revisions != sorted(set(revisions)):
            raise ValueError("Export revisions must be unique and sorted")
        if len({d.id for d in self.decisions}) != len(self.decisions):
            raise ValueError("Export contains duplicate decisions")
        expected = digest(
            {
                "schema_version": 1,
                "repository_id": self.repository_id,
                "parent_snapshot_id": self.parent_snapshot_id,
                "source_commit": self.source_commit,
                "revision_ids": revisions,
            }
        )
        if self.manifest_digest != expected:
            raise ValueError("Snapshot manifest digest mismatch")
        if len(canonical(self.model_dump(mode="json"))) > MAX_EXPORT_BYTES:
            raise ValueError("Snapshot exceeds export size limit")
        return self

    def content_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class SnapshotLock(PortableVersions):
    format: Literal["tacit-lock-v1"] = "tacit-lock-v1"
    api_url: str = Field(max_length=2048)
    workspace_id: str = Field(min_length=1, max_length=128)
    repository_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    export_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("api_url")
    @classmethod
    def safe_origin(cls, value: str) -> str:
        u = urlsplit(value)
        if (
            any(ord(c) < 32 or ord(c) == 127 for c in value)
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.port == 0
            or value != value.rstrip("/")
        ):
            raise ValueError("Lockfile API URL is invalid")
        if u.scheme != "https" and not (
            u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("Lockfile credentials require HTTPS except on loopback")
        return value

    def check(self, export: SnapshotExport) -> None:
        for field in (
            "workspace_id",
            "repository_id",
            "snapshot_id",
            "source_commit",
            "manifest_digest",
        ):
            if getattr(self, field) != getattr(export, field):
                raise ValueError("Export does not match lockfile scope")
        if export.content_digest() != self.export_sha256:
            raise ValueError("Export content digest does not match lockfile")
