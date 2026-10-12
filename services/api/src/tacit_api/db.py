"""PostgreSQL authority for access and confirmed knowledge."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    github_id: Mapped[str] = mapped_column(String(32), unique=True)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    github_owner_id: Mapped[str] = mapped_column(String(32), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Repository(Base):
    __tablename__ = "repositories"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    github_id: Mapped[str] = mapped_column(String(32), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("id", name="repository_global_id"),)


class Membership(Base):
    __tablename__ = "memberships"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    invited: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    repository_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        CheckConstraint("role IN ('workspace_admin', 'maintainer', 'learner')", name="valid_role"),
        CheckConstraint(
            "role <> 'workspace_admin' OR repository_id IS NULL", name="admin_workspace_scope"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "user_id"], ["memberships.workspace_id", "memberships.user_id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )


class DecisionRevision(Base):
    __tablename__ = "decision_revisions"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64))
    parent_revision_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    content: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "parent_revision_id"],
            [
                "decision_revisions.workspace_id",
                "decision_revisions.repository_id",
                "decision_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_commit: Mapped[str] = mapped_column(String(64))
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "parent_snapshot_id"],
            ["snapshots.workspace_id", "snapshots.repository_id", "snapshots.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )


class SnapshotMember(Base):
    __tablename__ = "snapshot_members"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "snapshot_id"],
            ["snapshots.workspace_id", "snapshots.repository_id", "snapshots.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "revision_id"],
            [
                "decision_revisions.workspace_id",
                "decision_revisions.repository_id",
                "decision_revisions.id",
            ],
        ),
    )


class RepositoryHead(Base):
    __tablename__ = "repository_heads"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "snapshot_id"],
            ["snapshots.workspace_id", "snapshots.repository_id", "snapshots.id"],
        ),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Installation(Base):
    __tablename__ = "installations"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    github_id: Mapped[str] = mapped_column(String(32), unique=True)
    owner_login: Mapped[str] = mapped_column(String(255))
    owner_type: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Credential(Base):
    __tablename__ = "credentials"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    agent: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_hash: Mapped[str | None] = mapped_column(ForeignKey("credentials.token_hash"))
    repository_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider_token: Mapped[str | None] = mapped_column(String(4096))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class DevicePairing(Base):
    __tablename__ = "device_pairings"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_code: Mapped[str] = mapped_column(String(4096))
    interval: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Invitation(Base):
    __tablename__ = "invitations"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    github_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    repository_ids: Mapped[list[str]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    body_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeachingDraft(Base):
    __tablename__ = "teaching_drafts"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    operation_id: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("repository_id", "author_id", "operation_id", name="draft_operation"),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )


class PublicationReceipt(Base):
    __tablename__ = "publication_receipts"
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("repository_id", "draft_id", name="draft_published_once"),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id", "draft_id"],
            ["teaching_drafts.workspace_id", "teaching_drafts.repository_id", "teaching_drafts.id"],
        ),
    )
