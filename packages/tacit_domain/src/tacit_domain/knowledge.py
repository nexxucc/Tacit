from dataclasses import dataclass
from typing import Protocol

from tacit_contracts.models import (
    ContextRequest,
    ContextResponse,
    Decision,
    EvaluationResponse,
    ScenarioRequest,
)

from tacit_domain.access import DomainError, Principal, authorize
from tacit_domain.rules import evaluate


@dataclass(frozen=True)
class Repository:
    id: str
    workspace_id: str
    full_name: str
    enabled: bool = True


@dataclass(frozen=True)
class Snapshot:
    id: str
    repository_id: str
    source_commit: str
    decisions: tuple[Decision, ...]
    parent_snapshot_id: str | None = None
    manifest_digest: str | None = None


class KnowledgeStore(Protocol):
    def repositories(self) -> tuple[Repository, ...]: ...
    def snapshot(self, repository: str, snapshot: str) -> Snapshot | None: ...
    def history(
        self, repository: str, snapshot: str, decision: str, cursor: int, limit: int
    ) -> tuple[Decision, ...]: ...


class KnowledgeService:
    def __init__(self, store: KnowledgeStore):
        self.store = store

    def repository(self, p: Principal, rid: str, *, write: bool = False) -> Repository:
        repo = next((r for r in self.store.repositories() if r.id == rid and r.enabled), None)
        if repo is None:
            raise DomainError("not_found", "Repository unavailable", 404)
        authorize(p, repo.workspace_id, rid, write=write)
        return repo

    def snapshot(self, p: Principal, rid: str, sid: str) -> tuple[Repository, Snapshot]:
        repo = self.repository(p, rid)
        snap = self.store.snapshot(rid, sid)
        if snap is None:
            raise DomainError("not_found", "Snapshot unavailable", 404)
        return repo, snap

    def context(self, p: Principal, rid: str, request: ContextRequest) -> ContextResponse:
        repo, snap = self.snapshot(p, rid, request.snapshot_id)
        if request.source_commit != snap.source_commit:
            return ContextResponse(
                workspace_id=repo.workspace_id,
                repository_id=rid,
                snapshot_id=snap.id,
                source_commit=snap.source_commit,
                applicability="unknown",
                status="unknown",
                warnings=("Commit applicability has not been verified",),
            )
        items = tuple(
            d
            for d in snap.decisions
            if d.source_commit == request.source_commit
            and (not request.files or set(d.files).intersection(request.files))
        )
        page = items[request.cursor : request.cursor + request.limit]
        more = request.cursor + len(page) < len(items)
        return ContextResponse(
            workspace_id=repo.workspace_id,
            repository_id=rid,
            snapshot_id=snap.id,
            source_commit=snap.source_commit,
            applicability="confirmed",
            status="answered" if page else "unknown",
            decisions=page,
            evidence=tuple(e for d in page for e in d.evidence),
            truncated=more,
            next_cursor=request.cursor + len(page) if more else None,
            warnings=("Retained knowledge from other commits needs applicability review",)
            if any(d.source_commit != request.source_commit for d in snap.decisions)
            else (),
        )

    def evaluate(self, p: Principal, rid: str, request: ScenarioRequest) -> EvaluationResponse:
        _, snap = self.snapshot(p, rid, request.snapshot_id)
        result, references = evaluate(
            tuple(d for d in snap.decisions if d.source_commit == request.source_commit)
            if request.source_commit == snap.source_commit
            else (),
            request.facts,
        )
        return EvaluationResponse(
            repository_id=rid, snapshot_id=snap.id, decision=result, rule_references=references
        )

    def history(
        self, p: Principal, rid: str, sid: str, did: str, cursor: int, limit: int
    ) -> tuple[Decision, ...]:
        _, snap = self.snapshot(p, rid, sid)
        if not any(d.id == did for d in snap.decisions):
            raise DomainError("not_found", "Decision unavailable", 404)
        return self.store.history(rid, sid, did, cursor, limit)
