"""Exact-draft approvals. Provider checks precede locks; local checks repeat under them."""

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_contracts.models import (
    DraftApprovalRequest,
    DraftView,
    PublicationPolicyRequest,
    PublicationRequest,
)
from tacit_domain.access import DomainError, Principal, authorize

from tacit_api.access import digest
from tacit_api.db import (
    DraftApproval,
    PublicationPolicy,
    PublicationReceipt,
    Repository,
    TeachingDraft,
)

if TYPE_CHECKING:
    from tacit_api.publication import PublicationService

MAX_APPROVALS = 20


class Quorum:
    def __init__(self, publication: "PublicationService"):
        self.publication = publication
        self.access = publication.access
        self.engine = publication.engine

    def policy(self, db: Session, repo: Repository) -> tuple[int, int]:
        policy = db.get(PublicationPolicy, (repo.workspace_id, repo.id))
        return (policy.required_maintainers, policy.version) if policy else (1, 0)

    def repository(self, db: Session, rid: str) -> Repository:
        repo = db.scalar(select(Repository).where(Repository.id == rid))
        if repo is None or not repo.enabled:
            raise DomainError("not_found", "Repository unavailable", 404)
        return repo

    def policy_view(self, p: Principal, rid: str) -> dict[str, object]:
        with Session(self.engine) as db:
            repo = self.repository(db, rid)
            self.access.admin(p, repo.workspace_id)
            required, version = self.policy(db, repo)
            return {"repository_id": rid, "required_maintainers": required, "version": version}

    def configure(
        self, p: Principal, rid: str, body: PublicationPolicyRequest
    ) -> dict[str, object]:
        with Session(self.engine) as db, db.begin():
            repo = self.repository(db, rid)
            self.access.admin(p, repo.workspace_id)
            self.access.lock_admin(db, p, repo.workspace_id)
            _, version = self.policy(db, repo)
            if body.expected_version != version:
                raise DomainError("policy_conflict", "Publication policy changed; reload it", 409)
            policy = db.get(PublicationPolicy, (repo.workspace_id, rid))
            if policy is None:
                policy = PublicationPolicy(workspace_id=repo.workspace_id, repository_id=rid)
                db.add(policy)
            policy.required_maintainers = body.required_maintainers
            policy.version = version + 1
            self.access.audit(
                db,
                p.user_id,
                repo.workspace_id,
                "publication.policy_changed",
                {
                    "repository_id": rid,
                    "version": policy.version,
                    "required_maintainers": policy.required_maintainers,
                },
            )
            return {
                "repository_id": rid,
                "required_maintainers": policy.required_maintainers,
                "version": policy.version,
            }

    def approvals(self, db: Session, repo: Repository, draft: str) -> list[DraftApproval]:
        return list(
            db.scalars(
                select(DraftApproval)
                .where(
                    DraftApproval.workspace_id == repo.workspace_id,
                    DraftApproval.repository_id == repo.id,
                    DraftApproval.draft_id == draft,
                )
                .order_by(DraftApproval.user_id)
                .limit(MAX_APPROVALS + 1)
            )
        )

    def view(self, p: Principal, rid: str, draft: str) -> dict[str, object]:
        with Session(self.engine) as db:
            repo = self.repository(db, rid)
            authorize(p, repo.workspace_id, rid, write=True)
            row = db.get(TeachingDraft, (repo.workspace_id, rid, draft))
            if row is None:
                raise DomainError("not_found", "Teaching draft unavailable", 404)
            required, version = self.policy(db, repo)
            return {
                "draft_id": draft,
                "required_maintainers": required,
                "policy_version": version,
                "review_digest": DraftView.model_validate(row.payload).review_digest,
                "approvals": [
                    {
                        "user_id": a.user_id,
                        "policy_version": a.policy_version,
                        "review_digest": a.review_digest,
                    }
                    for a in self.approvals(db, repo, draft)
                ],
                "note": "Recorded approvals only; current authority is revalidated at publication. The confirmer counts once.",
            }

    def approve(
        self, p: Principal, rid: str, draft: str, token: str, body: DraftApprovalRequest
    ) -> dict[str, object]:
        with Session(self.engine) as db, db.begin():
            repo = self.publication.write_repository(db, p, rid, token)
            row = db.get(TeachingDraft, (repo.workspace_id, rid, draft))
            if row is None:
                raise DomainError("not_found", "Teaching draft unavailable", 404)
            view = self.publication.view(db, row)
            if view.status == "published":
                raise DomainError("already_published", "Draft already published", 409)
            self.publication.current(db, repo, body.expected_snapshot_id)
            _, version = self.policy(db, repo)
            if body.policy_version != version:
                raise DomainError(
                    "policy_conflict", "Publication policy changed; review again", 409
                )
            if (
                body.review_digest != view.review_digest
                or body.expected_snapshot_id != view.expected_snapshot_id
            ):
                raise DomainError(
                    "review_conflict", "Approval must match the exact reviewed draft", 409
                )
            key = (repo.workspace_id, rid, draft, p.user_id)
            approval = db.get(DraftApproval, key)
            if approval is None:
                count = (
                    db.scalar(
                        select(func.count())
                        .select_from(DraftApproval)
                        .where(
                            DraftApproval.workspace_id == repo.workspace_id,
                            DraftApproval.repository_id == rid,
                            DraftApproval.draft_id == draft,
                        )
                    )
                    or 0
                )
                if count >= MAX_APPROVALS:
                    raise DomainError(
                        "approval_quota", "At most twenty recorded reviewers per draft", 409
                    )
                approval = DraftApproval(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    draft_id=draft,
                    user_id=p.user_id,
                )
                db.add(approval)
            approval.credential_hash = digest(token)
            approval.review_digest = view.review_digest
            approval.policy_version = version
            self.access.audit(
                db,
                p.user_id,
                repo.workspace_id,
                "publication.approved",
                {
                    "repository_id": rid,
                    "draft_id": draft,
                    "review_digest": view.review_digest,
                    "policy_version": version,
                },
            )
        return self.view(p, rid, draft)

    def withdraw(self, p: Principal, rid: str, draft: str, token: str) -> dict[str, object]:
        with Session(self.engine) as db, db.begin():
            repo = self.publication.write_repository(db, p, rid, token)
            row = db.get(TeachingDraft, (repo.workspace_id, rid, draft))
            if row is None:
                raise DomainError("not_found", "Teaching draft unavailable", 404)
            if self.publication.view(db, row).status == "published":
                raise DomainError("already_published", "Draft already published", 409)
            approval = db.get(DraftApproval, (repo.workspace_id, rid, draft, p.user_id))
            if approval:
                db.delete(approval)
                self.access.audit(
                    db,
                    p.user_id,
                    repo.workspace_id,
                    "publication.approval_withdrawn",
                    {"repository_id": rid, "draft_id": draft},
                )
        return self.view(p, rid, draft)

    def preflight(
        self, p: Principal, rid: str, body: PublicationRequest
    ) -> dict[str, tuple[str, Principal]]:
        # Never perform provider calls while holding the publication workspace lock.
        with Session(self.engine) as db:
            repo = self.repository(db, rid)
            authorize(p, repo.workspace_id, rid, write=True)
            if db.get(PublicationReceipt, (repo.workspace_id, rid, p.user_id, body.operation_id)):
                return {}
            required, version = self.policy(db, repo)
            if required == 1:
                return {}
            draft = db.get(TeachingDraft, (repo.workspace_id, rid, body.draft_id))
            if draft is None:
                return {}
            reviewed = self.publication.view(db, draft)
            if (
                reviewed.status == "published"
                or reviewed.review_digest != body.review_digest
                or reviewed.expected_snapshot_id != body.expected_snapshot_id
            ):
                return {}
            rows = [
                (a.user_id, a.credential_hash)
                for a in self.approvals(db, repo, body.draft_id)
                if a.user_id != p.user_id
                and a.policy_version == version
                and a.review_digest == body.review_digest
            ]
        proofs = {}
        for uid, token_hash in rows:
            try:
                fresh = self.access.principal_hash(token_hash, fresh=True)
                if fresh.user_id == uid and not fresh.agent:
                    authorize(fresh, repo.workspace_id, rid, write=True)
                    proofs[uid] = (token_hash, fresh)
                    if len(proofs) >= required - 1:
                        break
            except DomainError as exc:
                if exc.status not in {401, 403, 404}:
                    raise
        return proofs

    def enforce(
        self,
        db: Session,
        repo: Repository,
        p: Principal,
        view: DraftView,
        proofs: dict[str, tuple[str, Principal]],
    ) -> None:
        required, version = self.policy(db, repo)
        voters = {p.user_id}
        for approval in self.approvals(db, repo, view.id):
            proof = proofs.get(approval.user_id)
            if (
                proof is None
                or proof[0] != approval.credential_hash
                or approval.policy_version != version
                or approval.review_digest != view.review_digest
            ):
                continue
            try:
                self.publication.write_repository_hash(db, proof[1], repo.id, proof[0])
                voters.add(approval.user_id)
            except DomainError as exc:
                if exc.status not in {401, 403, 404}:
                    raise
        if len(voters) < required:
            raise DomainError(
                "quorum_required",
                f"Publication needs {required} distinct current human maintainers; {len(voters)} verified. Review approvals and retry.",
                409,
            )
        self.access.audit(
            db,
            p.user_id,
            repo.workspace_id,
            "publication.quorum_satisfied",
            {
                "repository_id": repo.id,
                "draft_id": view.id,
                "review_digest": view.review_digest,
                "policy_version": version,
                "required_maintainers": required,
                "approvers": sorted(voters),
            },
        )
