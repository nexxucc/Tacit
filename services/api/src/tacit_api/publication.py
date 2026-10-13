"""Human-reviewed publication, serialized per repository and committed atomically."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_contracts.models import (
    Decision,
    DraftRequest,
    DraftView,
    Evidence,
    PublicationRequest,
    PublicationResponse,
    Role,
    VoiceEvidence,
)
from tacit_domain.access import DomainError, Grant, Principal, authorize

from tacit_api.access import AccessService, digest
from tacit_api.db import (
    AuditEvent,
    Credential,
    DecisionRevision,
    Installation,
    Membership,
    OutboxEvent,
    PublicationReceipt,
    Repository,
    RepositoryHead,
    RoleAssignment,
    Snapshot,
    SnapshotMember,
    TeachingDraft,
    VoiceAttempt,
    VoiceSession,
    VoiceTranscript,
    Workspace,
)


def fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class PublicationService:
    def __init__(self, access: AccessService):
        self.access = access
        self.engine = access.engine

    def write_repository(self, db: Session, p: Principal, rid: str, token: str) -> Repository:
        if p.agent:
            raise DomainError("forbidden", "Human maintainer permission is required")
        repo = db.scalar(select(Repository).where(Repository.id == rid))
        if repo is None:
            raise DomainError("not_found", "Repository unavailable", 404)
        # Shared workspace lock serializes role revocation with publication. Lock installation
        # and repository too, so webhook revocation cannot race past this final local check.
        workspace = db.scalar(
            select(Workspace).where(Workspace.id == repo.workspace_id).with_for_update()
        )
        installation = db.scalar(
            select(Installation)
            .where(Installation.workspace_id == repo.workspace_id)
            .with_for_update()
        )
        repo = db.scalar(
            select(Repository)
            .where(Repository.id == rid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        credential = db.scalar(
            select(Credential).where(Credential.token_hash == digest(token)).with_for_update()
        )
        self.access.credential(db, digest(token))
        if credential is None or credential.agent or credential.user_id != p.user_id:
            raise DomainError("forbidden", "Human maintainer permission is required")
        if (
            repo is None
            or not repo.enabled
            or not workspace
            or not workspace.active
            or not installation
            or not installation.active
        ):
            raise DomainError("not_found", "Repository unavailable", 404)
        member = db.get(Membership, (repo.workspace_id, p.user_id))
        grants = tuple(
            Grant(g.workspace_id, Role(g.role), g.repository_id)
            for g in db.scalars(select(RoleAssignment).where(RoleAssignment.user_id == p.user_id))
        )
        fresh = Principal(
            p.user_id,
            False,
            frozenset({repo.workspace_id}) if member and member.active else frozenset(),
            p.github_repositories,
            grants,
        )
        authorize(fresh, repo.workspace_id, rid, write=True)
        return repo

    def head(self, p: Principal, rid: str) -> dict[str, object]:
        with Session(self.engine) as db:
            repo = db.scalar(select(Repository).where(Repository.id == rid))
            if repo is None:
                raise DomainError("not_found", "Repository unavailable", 404)
            authorize(p, repo.workspace_id, rid)
            head = db.get(RepositoryHead, (repo.workspace_id, rid))
            snapshot = (
                db.get(Snapshot, (repo.workspace_id, rid, head.snapshot_id)) if head else None
            )
            return {
                "repository_id": rid,
                "snapshot_id": head.snapshot_id if head else None,
                "source_commit": snapshot.source_commit if snapshot else None,
                "manifest_digest": snapshot.digest if snapshot else None,
            }

    def current(self, db: Session, repo: Repository, expected: str | None) -> RepositoryHead | None:
        head = db.get(RepositoryHead, (repo.workspace_id, repo.id))
        if (head.snapshot_id if head else None) != expected:
            raise DomainError(
                "snapshot_conflict",
                "Knowledge head changed; review a new draft against the current snapshot",
                409,
            )
        return head

    def existing_draft(
        self, db: Session, rid: str, uid: str, operation: str, request_digest: str
    ) -> TeachingDraft | None:
        draft = db.scalar(
            select(TeachingDraft).where(
                TeachingDraft.repository_id == rid,
                TeachingDraft.author_id == uid,
                TeachingDraft.operation_id == operation,
            )
        )
        if draft and draft.request_digest != request_digest:
            raise DomainError(
                "idempotency_conflict", "Operation ID was used with different draft content", 409
            )
        return draft

    def view(self, db: Session, draft: TeachingDraft) -> DraftView:
        view = DraftView.model_validate(draft.payload)
        receipt = db.scalar(
            select(PublicationReceipt).where(
                PublicationReceipt.repository_id == draft.repository_id,
                PublicationReceipt.draft_id == draft.id,
            )
        )
        if receipt:
            return view.model_copy(
                update={
                    "status": "published",
                    "published_snapshot_id": receipt.result["snapshot_id"],
                }
            )
        return view

    def create(
        self,
        p: Principal,
        rid: str,
        token: str,
        body: DraftRequest,
        *,
        voice: VoiceEvidence | None = None,
    ) -> DraftView:
        if body.transcript_draft_id and body.transcript_operation_id:
            raise DomainError("invalid_correction", "Select one original transcript reference", 422)
        if body.transcript_operation_id:
            with Session(self.engine) as db, db.begin():
                repo = self.write_repository(db, p, rid, token)
                transcript = db.get(
                    VoiceTranscript,
                    (repo.workspace_id, rid, p.user_id, body.transcript_operation_id),
                )
                if transcript is None:
                    raise DomainError("not_found", "Original transcript unavailable", 404)
                source = transcript.payload["request"]
                assert isinstance(source, dict)
                if (
                    body.source_commit != source["source_commit"]
                    or list(body.files) != source["files"]
                ):
                    raise DomainError(
                        "source_unbound", "Corrections must retain original source bindings", 422
                    )
                voice = VoiceEvidence.model_validate(transcript.payload["voice"]).model_copy(
                    update={
                        "corrected": body.statement
                        != VoiceEvidence.model_validate(
                            transcript.payload["voice"]
                        ).original_transcript
                    }
                )
        if body.transcript_draft_id:
            original = self.review(p, rid, body.transcript_draft_id)
            if original.voice is None or original.author_id != p.user_id:
                raise DomainError(
                    "forbidden", "Only the recording author may correct its transcript"
                )
            if body.source_commit != original.source_commit or body.files != tuple(
                s.path for s in original.sources
            ):
                raise DomainError(
                    "source_unbound",
                    "Transcript corrections must retain their source bindings",
                    422,
                )
            voice = original.voice.model_copy(
                update={
                    "original_draft_id": original.voice.original_draft_id or original.id,
                    "corrected": body.statement != original.voice.original_transcript,
                }
            )
        request_digest = fingerprint(
            {
                "request": body.model_dump(mode="json"),
                "voice": voice.model_dump(mode="json") if voice else None,
            }
        )
        if (
            voice is None
            and body.transcript_draft_id is None
            and body.transcript_operation_id is None
        ):
            request_digest = fingerprint(
                body.model_dump(
                    mode="json", exclude={"transcript_draft_id", "transcript_operation_id"}
                )
            )
        # Authorize before reading drafts or making any provider call.
        with Session(self.engine) as db, db.begin():
            repo = self.write_repository(db, p, rid, token)
            if voice is None or body.transcript_draft_id or body.transcript_operation_id:
                for model in (VoiceSession, VoiceAttempt):
                    reserved = db.scalar(
                        select(model).where(
                            model.repository_id == rid,
                            model.author_id == p.user_id,
                            model.operation_id == body.operation_id,
                        )
                    )
                    if reserved is not None:
                        raise DomainError(
                            "idempotency_conflict",
                            "Operation ID belongs to a voice recording; use a new ID for text or corrections",
                            409,
                        )
            prior = self.existing_draft(db, rid, p.user_id, body.operation_id, request_digest)
            if prior:
                return self.view(db, prior)
            full_name = repo.full_name
            provider = self.access.credential(db, digest(token)).provider_token
            if not provider:
                raise DomainError("unauthenticated", "GitHub credential unavailable", 401)
        sources = self.access.github.source_bindings(
            self.access.decrypt(provider), full_name, body.source_commit, body.files
        )
        # Source verification does not hold a database lock. Recheck all local authority now.
        with Session(self.engine) as db, db.begin():
            repo = self.write_repository(db, p, rid, token)
            if voice is None or body.transcript_draft_id or body.transcript_operation_id:
                for model in (VoiceSession, VoiceAttempt):
                    reserved = db.scalar(
                        select(model).where(
                            model.repository_id == rid,
                            model.author_id == p.user_id,
                            model.operation_id == body.operation_id,
                        )
                    )
                    if reserved is not None:
                        raise DomainError(
                            "idempotency_conflict",
                            "Operation ID belongs to a voice recording; use a new ID for text or corrections",
                            409,
                        )
            prior = self.existing_draft(db, rid, p.user_id, body.operation_id, request_digest)
            if prior:
                return self.view(db, prior)
            if (
                voice is not None
                and voice.session_id is not None
                and body.transcript_draft_id is None
                and body.transcript_operation_id is None
            ):
                session = db.get(VoiceSession, (repo.workspace_id, rid, voice.session_id))
                if (
                    session is None
                    or session.author_id != p.user_id
                    or session.status not in {"processing", "completed", "uncertain"}
                ):
                    raise DomainError("voice_state", "Voice session was cancelled or expired", 409)
            self.current(db, repo, body.expected_snapshot_id)
            previous = None
            if body.decision_id:
                revision = db.scalar(
                    select(DecisionRevision)
                    .join(
                        SnapshotMember,
                        (SnapshotMember.workspace_id == DecisionRevision.workspace_id)
                        & (SnapshotMember.repository_id == DecisionRevision.repository_id)
                        & (SnapshotMember.revision_id == DecisionRevision.id),
                    )
                    .where(
                        SnapshotMember.workspace_id == repo.workspace_id,
                        SnapshotMember.repository_id == rid,
                        SnapshotMember.snapshot_id == body.expected_snapshot_id,
                        DecisionRevision.decision_id == body.decision_id,
                    )
                )
                if revision is None:
                    raise DomainError(
                        "not_found", "Decision is not present in the selected snapshot", 404
                    )
                previous = Decision.model_validate(revision.content)
            view = DraftView(
                id=str(uuid4()),
                workspace_id=repo.workspace_id,
                repository_id=rid,
                author_id=p.user_id,
                decision_id=body.decision_id or str(uuid4()),
                expected_snapshot_id=body.expected_snapshot_id,
                statement=body.statement,
                source_commit=body.source_commit,
                sources=sources,
                rule=body.rule,
                previous=previous,
                voice=voice,
                review_digest="0" * 64,
            )
            payload = view.model_dump(
                mode="json", exclude={"review_digest", "status", "published_snapshot_id"}
            )
            view = view.model_copy(update={"review_digest": fingerprint(payload)})
            db.add(
                TeachingDraft(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    id=view.id,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    request_digest=request_digest,
                    payload=view.model_dump(mode="json"),
                )
            )
            db.add(
                AuditEvent(
                    id=str(uuid4()),
                    workspace_id=repo.workspace_id,
                    actor_id=p.user_id,
                    action="teaching.draft_created",
                    details={"repository_id": rid, "draft_id": view.id},
                )
            )
            return view

    def review(self, p: Principal, rid: str, draft_id: str) -> DraftView:
        with Session(self.engine) as db:
            repo = db.scalar(select(Repository).where(Repository.id == rid))
            if repo is None:
                raise DomainError("not_found", "Repository unavailable", 404)
            authorize(p, repo.workspace_id, rid, write=True)
            draft = db.get(TeachingDraft, (repo.workspace_id, rid, draft_id))
            if draft is None:
                raise DomainError("not_found", "Teaching draft unavailable", 404)
            return self.view(db, draft)

    def publish(
        self, p: Principal, rid: str, token: str, body: PublicationRequest
    ) -> PublicationResponse:
        request_digest = fingerprint(body.model_dump(mode="json"))
        with Session(self.engine) as db, db.begin():
            repo = self.write_repository(db, p, rid, token)
            receipt = db.get(
                PublicationReceipt, (repo.workspace_id, rid, p.user_id, body.operation_id)
            )
            if receipt:
                if receipt.request_digest != request_digest:
                    raise DomainError(
                        "idempotency_conflict",
                        "Operation ID was used with a different confirmation",
                        409,
                    )
                return PublicationResponse.model_validate(receipt.result)
            draft = db.get(TeachingDraft, (repo.workspace_id, rid, body.draft_id))
            if draft is None:
                raise DomainError("not_found", "Teaching draft unavailable", 404)
            view = self.view(db, draft)
            if view.status == "published":
                raise DomainError("already_published", "This draft has already been confirmed", 409)
            if (
                body.review_digest != view.review_digest
                or body.expected_snapshot_id != view.expected_snapshot_id
            ):
                raise DomainError(
                    "review_conflict",
                    "Confirmation must match the exact reviewed draft and base snapshot",
                    409,
                )
            head = self.current(db, repo, body.expected_snapshot_id)
            revision_id, snapshot_id = str(uuid4()), str(uuid4())
            decision = Decision(
                id=view.decision_id,
                revision_id=revision_id,
                statement=view.statement,
                files=tuple(s.path for s in view.sources),
                source_commit=view.source_commit,
                sources=view.sources,
                evidence=(
                    Evidence(
                        decision_id=view.decision_id,
                        revision_id=revision_id,
                        segment_id=view.id,
                        kind="transcript" if view.voice else "text",
                    ),
                ),
                rule=view.rule,
            )
            members = (
                list(
                    db.scalars(
                        select(SnapshotMember).where(
                            SnapshotMember.workspace_id == repo.workspace_id,
                            SnapshotMember.repository_id == rid,
                            SnapshotMember.snapshot_id == body.expected_snapshot_id,
                        )
                    )
                )
                if head
                else []
            )
            retained = []
            for member in members:
                revision = db.get(DecisionRevision, (repo.workspace_id, rid, member.revision_id))
                if revision and revision.decision_id != view.decision_id:
                    retained.append(member.revision_id)
            revision_ids = sorted([*retained, revision_id])
            manifest_digest = fingerprint(
                {
                    "schema_version": 1,
                    "repository_id": rid,
                    "parent_snapshot_id": body.expected_snapshot_id,
                    "source_commit": view.source_commit,
                    "revision_ids": revision_ids,
                }
            )
            db.add(
                DecisionRevision(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    id=revision_id,
                    decision_id=view.decision_id,
                    parent_revision_id=view.previous.revision_id if view.previous else None,
                    confirmed_by=p.user_id,
                    content=decision.model_dump(mode="json"),
                )
            )
            db.add(
                Snapshot(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    id=snapshot_id,
                    parent_snapshot_id=body.expected_snapshot_id,
                    source_commit=view.source_commit,
                    digest=manifest_digest,
                )
            )
            db.flush()
            for member_revision_id in revision_ids:
                db.add(
                    SnapshotMember(
                        workspace_id=repo.workspace_id,
                        repository_id=rid,
                        snapshot_id=snapshot_id,
                        revision_id=member_revision_id,
                    )
                )
            if head:
                head.snapshot_id = snapshot_id
                head.version += 1
            else:
                db.add(
                    RepositoryHead(
                        workspace_id=repo.workspace_id,
                        repository_id=rid,
                        snapshot_id=snapshot_id,
                        version=1,
                    )
                )
            result = PublicationResponse(
                repository_id=rid,
                decision_id=view.decision_id,
                revision_id=revision_id,
                snapshot_id=snapshot_id,
                source_commit=view.source_commit,
                manifest_digest=manifest_digest,
                parent_snapshot_id=body.expected_snapshot_id,
            )
            db.add(
                PublicationReceipt(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    actor_id=p.user_id,
                    operation_id=body.operation_id,
                    draft_id=view.id,
                    request_digest=request_digest,
                    result=result.model_dump(mode="json"),
                )
            )
            db.add(
                AuditEvent(
                    id=str(uuid4()),
                    workspace_id=repo.workspace_id,
                    actor_id=p.user_id,
                    action="knowledge.confirmed",
                    details={
                        "repository_id": rid,
                        "draft_id": view.id,
                        "review_digest": view.review_digest,
                        "snapshot_id": snapshot_id,
                        "revision_id": revision_id,
                        "operation_id": body.operation_id,
                    },
                )
            )
            db.add(
                OutboxEvent(
                    id=str(uuid4()),
                    workspace_id=repo.workspace_id,
                    idempotency_key="publication:"
                    + fingerprint([rid, p.user_id, body.operation_id]),
                    payload={"event": "knowledge.confirmed", **result.model_dump(mode="json")},
                )
            )
            return result
