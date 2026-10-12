"""Request-scoped database reads, with no synthetic fallback."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from tacit_contracts.models import Decision
from tacit_domain.knowledge import Repository as DomainRepository
from tacit_domain.knowledge import Snapshot as DomainSnapshot

from tacit_api.db import (
    DecisionRevision,
    Installation,
    Repository,
    Snapshot,
    SnapshotMember,
    Workspace,
)


class PostgreSQLStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def repositories(self) -> tuple[DomainRepository, ...]:
        with Session(self.engine) as db:
            return tuple(
                DomainRepository(r.id, r.workspace_id, r.full_name, r.enabled)
                for r in db.scalars(
                    select(Repository)
                    .join(Workspace)
                    .join(Installation, Installation.workspace_id == Workspace.id)
                    .where(Workspace.active.is_(True), Installation.active.is_(True))
                )
            )

    def snapshot(self, repository: str, snapshot: str) -> DomainSnapshot | None:
        with Session(self.engine) as db:
            snap = db.scalar(
                select(Snapshot).where(
                    Snapshot.repository_id == repository, Snapshot.id == snapshot
                )
            )
            if snap is None:
                return None
            revisions = db.scalars(
                select(DecisionRevision)
                .join(
                    SnapshotMember,
                    (SnapshotMember.workspace_id == DecisionRevision.workspace_id)
                    & (SnapshotMember.repository_id == DecisionRevision.repository_id)
                    & (SnapshotMember.revision_id == DecisionRevision.id),
                )
                .where(
                    SnapshotMember.workspace_id == snap.workspace_id,
                    SnapshotMember.repository_id == repository,
                    SnapshotMember.snapshot_id == snapshot,
                )
                .order_by(DecisionRevision.id)
            )
            return DomainSnapshot(
                snap.id,
                repository,
                snap.source_commit,
                tuple(Decision.model_validate(r.content) for r in revisions),
                parent_snapshot_id=snap.parent_snapshot_id,
                manifest_digest=snap.digest,
            )

    def history(
        self, repository: str, snapshot: str, decision: str, cursor: int, limit: int
    ) -> tuple[Decision, ...]:
        with Session(self.engine) as db:
            revision = db.scalar(
                select(DecisionRevision)
                .join(
                    SnapshotMember,
                    (SnapshotMember.workspace_id == DecisionRevision.workspace_id)
                    & (SnapshotMember.repository_id == DecisionRevision.repository_id)
                    & (SnapshotMember.revision_id == DecisionRevision.id),
                )
                .where(
                    SnapshotMember.repository_id == repository,
                    SnapshotMember.snapshot_id == snapshot,
                    DecisionRevision.decision_id == decision,
                )
            )
            result = []
            visited: set[str] = set()
            for index in range(cursor + limit):
                if revision is None or revision.id in visited or revision.decision_id != decision:
                    break
                visited.add(revision.id)
                if index >= cursor:
                    result.append(Decision.model_validate(revision.content))
                revision = (
                    db.get(
                        DecisionRevision,
                        (revision.workspace_id, repository, revision.parent_revision_id),
                    )
                    if revision.parent_revision_id
                    else None
                )
            return tuple(result)
