"""Opt-in, snapshot-scoped model answers with validated revision citations."""

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from tacit_contracts.models import CitedAnswer, ContextResponse, QuestionRequest
from tacit_domain.access import DomainError, Principal, authorize
from tacit_domain.knowledge import KnowledgeService

from tacit_api.db import ModelOperation
from tacit_api.providers import ProviderUnavailable
from tacit_api.publication import PublicationService, fingerprint
from tacit_api.reasoning import Evon


class Answers:
    def __init__(self, manager: PublicationService, knowledge: KnowledgeService, model: Evon):
        self.manager, self.knowledge, self.model = manager, knowledge, model

    def reserve(
        self, p: Principal, rid: str, request: QuestionRequest, context: ContextResponse
    ) -> ContextResponse | None:
        repo = self.knowledge.repository(p, rid)
        kind = "question-agent" if p.agent else "question-human"
        hashed = fingerprint(request.model_dump(mode="json"))
        with Session(self.manager.engine) as db, db.begin():
            key = (repo.workspace_id, rid, p.user_id, request.operation_id, kind)
            prior = db.get(ModelOperation, key, with_for_update=True)
            if prior:
                if prior.request_digest != hashed:
                    raise DomainError("idempotency_conflict", "Question input changed", 409)
                if prior.status != "completed":
                    raise DomainError(
                        "model_outcome_uncertain",
                        "Question was attempted; it will not be replayed automatically",
                        409,
                    )
                return ContextResponse.model_validate(prior.result)
            db.add(
                ModelOperation(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=request.operation_id,
                    kind=kind,
                    request_digest=hashed,
                    status="processing",
                )
            )
        return None

    async def answer(
        self, p: Principal, rid: str, token: str, body: QuestionRequest
    ) -> ContextResponse:
        if not self.model.configured:
            raise DomainError("provider_unconfigured", "Evon endpoint is not configured", 503)
        context = await run_in_threadpool(self.knowledge.context, p, rid, body)
        if (
            body.cursor != 0
            or context.truncated
            or not context.decisions
            or context.applicability != "confirmed"
        ):
            return context.model_copy(
                update={
                    "answer": None,
                    "answer_kind": "none",
                    "warnings": context.warnings
                    + (
                        "Synthesis needs complete, applicable context starting at cursor zero; narrow the query or increase the limit.",
                    ),
                }
            )
        prior = await run_in_threadpool(self.reserve, p, rid, body, context)
        if prior is not None:
            return prior
        try:
            result, usage = await self.model.structured(
                "Answer only from these confirmed decisions. Cite supporting revision IDs. Do not invent constraints, infer missing permissions, or resolve conflicting rules. State uncertainty when the evidence is insufficient.",
                {
                    "question": body.question,
                    "decisions": [d.model_dump(mode="json") for d in context.decisions],
                },
                CitedAnswer,
            )
            assert isinstance(result, CitedAnswer)
            available = {d.revision_id for d in context.decisions}
            if not set(result.revision_ids).issubset(available):
                raise ValueError("Unknown citation")
            fresh = await run_in_threadpool(self.manager.access.principal, token)
            current = await run_in_threadpool(self.knowledge.context, fresh, rid, body)
            if current != context:
                raise DomainError("context_changed", "Context changed during synthesis", 409)
            response = context.model_copy(
                update={
                    "answer": result.answer + "\n\nRevisions: " + ", ".join(result.revision_ids),
                    "answer_kind": "cited_reasoning",
                    "usage": usage,
                }
            )
            await run_in_threadpool(self.save, fresh, rid, body, response)
            return response
        except (ProviderUnavailable, ValueError) as exc:
            raise DomainError(
                "answer_failed",
                "Answer generation failed; it will not be replayed automatically",
                503,
            ) from exc

    def save(self, p: Principal, rid: str, body: QuestionRequest, result: ContextResponse) -> None:
        repo = self.knowledge.repository(p, rid)
        authorize(p, repo.workspace_id, rid)
        with Session(self.manager.engine) as db, db.begin():
            item = db.get(
                ModelOperation,
                (
                    repo.workspace_id,
                    rid,
                    p.user_id,
                    body.operation_id,
                    "question-agent" if p.agent else "question-human",
                ),
                with_for_update=True,
            )
            assert item is not None
            item.status, item.result = "completed", result.model_dump(mode="json")
