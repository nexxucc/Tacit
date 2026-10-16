"""Human-owned grounded extraction and clarification; candidates never publish themselves."""

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from tacit_contracts.models import (
    CandidateSet,
    ClarificationAnswer,
    DraftRequest,
    PrepareDraft,
    Scalar,
    Selection,
    TeachingRequest,
    TeachingView,
    Usage,
    VoiceEvidence,
)
from tacit_domain.access import DomainError, Principal
from tacit_domain.rules import distinguishing, matches
from tacit_domain.scheduler import DOMAINS

from tacit_api.access import digest, now, valid_time
from tacit_api.db import ModelOperation, TeachingRun, User, VoiceTranscript
from tacit_api.persistence import PostgreSQLStore
from tacit_api.providers import ProviderUnavailable
from tacit_api.publication import PublicationService, fingerprint
from tacit_api.reasoning import Evon


class TeachingService:
    def __init__(self, publications: PublicationService, model: Evon):
        self.manager, self.model = publications, model

    def scoped(self, db: Session, p: Principal, rid: str, token: str, sid: str) -> TeachingRun:
        repo = self.manager.write_repository(db, p, rid, token)
        item = db.get(TeachingRun, (repo.workspace_id, rid, sid), with_for_update=True)
        if item is None or item.author_id != p.user_id:
            raise DomainError("not_found", "Teaching run unavailable", 404)
        if valid_time(item.expires_at) <= now():
            raise DomainError(
                "teaching_expired",
                "Transient teaching context expired; immutable drafts remain available",
                410,
            )
        return item

    def view(self, item: TeachingRun) -> TeachingView:
        return TeachingView.model_validate(
            {
                **item.payload["view"],
                "id": item.id,
                "repository_id": item.repository_id,
                "author_id": item.author_id,
                "version": item.version,
                "status": item.status,
            }
        )

    def list(self, p: Principal, rid: str, token: str) -> tuple[TeachingView, ...]:
        with Session(self.manager.engine) as db, db.begin():
            self.manager.write_repository(db, p, rid, token)
            return tuple(
                self.view(item)
                for item in db.scalars(
                    select(TeachingRun)
                    .where(
                        TeachingRun.repository_id == rid,
                        TeachingRun.author_id == p.user_id,
                        TeachingRun.expires_at > now(),
                    )
                    .order_by(TeachingRun.created_at.desc())
                    .limit(20)
                )
            )

    def get(self, p: Principal, rid: str, token: str, sid: str) -> TeachingView:
        with Session(self.manager.engine) as db, db.begin():
            return self.view(self.scoped(db, p, rid, token, sid))

    def reserve(
        self, p: Principal, rid: str, token: str, body: TeachingRequest
    ) -> tuple[str, dict[str, Any] | None]:
        hashed = fingerprint(body.model_dump(mode="json"))
        with Session(self.manager.engine) as db, db.begin():
            db.scalar(select(User).where(User.id == p.user_id).with_for_update(key_share=True))
            repo = self.manager.write_repository(db, p, rid, token)
            existing = db.scalar(
                select(TeachingRun).where(
                    TeachingRun.repository_id == rid,
                    TeachingRun.author_id == p.user_id,
                    TeachingRun.operation_id == body.operation_id,
                )
            )
            if existing:
                if existing.request_digest != hashed:
                    raise DomainError(
                        "idempotency_conflict", "Teaching operation input changed", 409
                    )
                if existing.status in {"processing", "uncertain"}:
                    raise DomainError(
                        "model_outcome_uncertain",
                        f"Extraction was attempted; retrieve teaching run {existing.id} rather than repeating it",
                        409,
                    )
                return existing.id, None
            self.manager.current(db, repo, body.expected_snapshot_id)
            if db.scalar(
                select(TeachingRun.id)
                .where(
                    TeachingRun.author_id == p.user_id,
                    TeachingRun.expires_at > now(),
                    TeachingRun.status.in_(
                        ("processing", "uncertain", "needs_clarification", "ready_for_review")
                    ),
                )
                .limit(20)
                .offset(19)
            ):
                raise DomainError(
                    "teaching_quota", "Cancel a pending teaching run before extracting another", 409
                )
            provider_token = self.manager.access.credential(db, digest(token)).provider_token
            if not provider_token:
                raise DomainError("unauthenticated", "GitHub credential unavailable", 401)
            full_name = repo.full_name
            transcript = body.statement
            if body.transcript_operation_id:
                original = db.get(
                    VoiceTranscript,
                    (repo.workspace_id, rid, p.user_id, body.transcript_operation_id),
                )
                if original is None:
                    raise DomainError("not_found", "Original transcript unavailable", 404)
                source = original.payload["request"]
                if not isinstance(source, dict):
                    raise DomainError(
                        "transcript_invalid", "Original transcript metadata is invalid", 409
                    )
                if source["source_commit"] != body.source_commit or set(source["files"]) != set(
                    body.files
                ):
                    raise DomainError(
                        "source_unbound", "Extraction must retain transcript source bindings", 409
                    )
                transcript = VoiceEvidence.model_validate(
                    original.payload["voice"]
                ).original_transcript
        assert isinstance(transcript, str)
        files = self.manager.access.github.source_text(
            self.manager.access.decrypt(provider_token), full_name, body.source_commit, body.files
        )
        selections = body.selections or tuple(
            Selection(path=f, captured_at=datetime.now(UTC).isoformat()) for f in body.files
        )
        selected: dict[str, dict[str, Any]] = {}
        for selection in selections:
            lines = files[selection.path]["lines"]
            end = min(selection.end_line, len(lines))
            if selection.start_line > end:
                raise DomainError("source_unbound", "Selected lines do not exist", 422)
            if selection.path in selected:
                raise DomainError("source_unbound", "Use one selection per file", 422)
            selected[selection.path] = {
                "blob_sha": files[selection.path]["blob_sha"],
                "start_line": selection.start_line,
                "end_line": end,
                "text": "\n".join(lines[selection.start_line - 1 : end]),
                "selection": selection.model_dump(mode="json"),
            }
        confirmed = []
        if body.expected_snapshot_id:
            snapshot = PostgreSQLStore(self.manager.engine).snapshot(rid, body.expected_snapshot_id)
            if snapshot:
                confirmed = [
                    d.model_dump(mode="json")
                    for d in snapshot.decisions
                    if d.source_commit == body.source_commit
                    and set(d.files).intersection(body.files)
                ]
        data: dict[str, Any] = {
            "transcript": transcript,
            "sources": selected,
            "domain": "scheduler-v1",
            "scheduler_facts": DOMAINS,
            "confirmed": confirmed,
        }
        if len(json.dumps(data)) > self.model.max_input_chars:
            raise DomainError(
                "context_budget",
                "Select smaller source ranges or split the teaching session; input will not be silently shortened",
                413,
            )
        with Session(self.manager.engine) as db, db.begin():
            db.scalar(select(User).where(User.id == p.user_id).with_for_update(key_share=True))
            repo = self.manager.write_repository(db, p, rid, token)
            existing = db.scalar(
                select(TeachingRun).where(
                    TeachingRun.repository_id == rid,
                    TeachingRun.author_id == p.user_id,
                    TeachingRun.operation_id == body.operation_id,
                )
            )
            if existing:
                raise DomainError(
                    "model_outcome_uncertain",
                    "Concurrent extraction already reserved this operation",
                    409,
                )
            self.manager.current(db, repo, body.expected_snapshot_id)
            if db.scalar(
                select(TeachingRun.id)
                .where(
                    TeachingRun.author_id == p.user_id,
                    TeachingRun.expires_at > now(),
                    TeachingRun.status.in_(
                        ("processing", "uncertain", "needs_clarification", "ready_for_review")
                    ),
                )
                .limit(20)
                .offset(19)
            ):
                raise DomainError("teaching_quota", "Cancel a pending teaching run first", 409)
            sid = str(uuid4())
            db.add(
                TeachingRun(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    id=sid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    request_digest=hashed,
                    version=1,
                    status="processing",
                    payload={"input": data, "view": {"source": body.model_dump(mode="json")}},
                )
            )
            return sid, data

    async def start(
        self, p: Principal, rid: str, token: str, body: TeachingRequest
    ) -> TeachingView:
        if not self.model.configured:
            raise DomainError(
                "provider_unconfigured", "Configure the Evon endpoint before extraction", 503
            )
        sid, data = await run_in_threadpool(self.reserve, p, rid, token, body)
        if data is None:
            return await run_in_threadpool(self.get, p, rid, token, sid)
        try:
            candidates, usage = await self.model.structured(
                "Extract the minimal faithful candidate set from the human intent. Use alternatives only for genuine ambiguity. Include exact transcript quotes and pinned source line citations. Code behavior is context, not proof of intent. Emit assumptions and unresolved questions explicitly. The scheduler_facts map gives the exact supported field names and values. For intent directly expressible using those facts, include a scheduler-v1 rule: when is an AND of field/equals predicates, action is compute, upload, publish_result or retry, and outcome is permitted or forbidden. For example, an expired-worker publication prohibition uses lease_expired=true, publish_result, forbidden. Use rule=null only when intent is outside this domain or insufficiently specified. Preserve negations; never infer permission from lack of prohibition.",
                data,
                CandidateSet,
            )
            assert isinstance(candidates, CandidateSet)
            for candidate in candidates.candidates:
                if candidate.transcript_quote not in data["transcript"]:
                    raise ValueError("Quote not grounded")
                if candidate.rule and candidate.rule.domain != "scheduler-v1":
                    raise ValueError("Unsupported candidate domain")
                for citation in candidate.citations:
                    source = data["sources"].get(citation.path)
                    if (
                        not source
                        or citation.blob_sha != source["blob_sha"]
                        or not source["start_line"]
                        <= citation.start_line
                        <= citation.end_line
                        <= source["end_line"]
                    ):
                        raise ValueError("Citation not grounded")
            fresh = await run_in_threadpool(self.manager.access.principal, token)
            return await run_in_threadpool(self.save, fresh, rid, token, sid, candidates, usage)
        except (ProviderUnavailable, ValueError) as exc:
            await run_in_threadpool(self.failed, p, rid, token, sid)
            raise DomainError(
                "extraction_failed",
                "Extraction failed; reserved model operation will not be automatically replayed",
                503,
            ) from exc

    def failed(self, p: Principal, rid: str, token: str, sid: str) -> None:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status == "processing":
                item.status = "uncertain"

    def save(
        self, p: Principal, rid: str, token: str, sid: str, result: CandidateSet, usage: Usage
    ) -> TeachingView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status != "processing":
                raise DomainError("teaching_state", "Teaching run was cancelled", 409)
            from tacit_contracts.models import Rule

            retained = tuple(
                Rule.model_validate(d["rule"])
                for d in item.payload["input"].get("confirmed", [])
                if d.get("rule")
            )
            scenarios = distinguishing(tuple(c.rule for c in result.candidates) + retained)
            questions = {
                f"{c.id}:{index}": q
                for c in result.candidates
                for index, q in enumerate(c.questions)
            }
            view = {
                **item.payload["view"],
                "candidates": result.model_dump(mode="json")["candidates"],
                "scenarios": scenarios,
                "questions": questions,
                "baseline": item.payload["input"].get("confirmed", []),
                "usage": usage.model_dump(mode="json"),
            }
            item.payload = {**item.payload, "view": view}
            item.status = "needs_clarification"
            return self.view(item)

    def answer(
        self, p: Principal, rid: str, token: str, sid: str, body: ClarificationAnswer
    ) -> TeachingView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            key = (item.workspace_id, rid, p.user_id, body.operation_id, "clarification")
            prior = db.get(ModelOperation, key)
            hashed = fingerprint({"run": sid, "body": body.model_dump(mode="json")})
            if prior:
                if prior.request_digest != hashed:
                    raise DomainError("idempotency_conflict", "Clarification input changed", 409)
                return self.view(item)
            if item.version != body.expected_version or item.status not in {
                "needs_clarification",
                "ready_for_review",
            }:
                raise DomainError("teaching_conflict", "Reload the current teaching version", 409)
            view = self.view(item)
            selected = next((c for c in view.candidates if c.id == body.candidate_id), None)
            if selected is None:
                raise DomainError("not_found", "Candidate unavailable", 404)
            rule = body.rule if body.replace_rule else selected.rule
            if rule and rule.domain != "scheduler-v1":
                raise DomainError(
                    "unsupported_rule", "Use scheduler-v1 for extracted executable rules", 422
                )
            answers = {**view.answers, **body.answers}
            required = {key for key in view.questions if key.startswith(selected.id + ":")}
            required |= {f"scenario:{i}" for i in range(len(view.scenarios))}
            if any(
                not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in answers.values()
            ) or set(answers) - (
                set(view.questions) | {f"scenario:{i}" for i in range(len(view.scenarios))}
            ):
                raise DomainError(
                    "invalid_answer", "Answer IDs/text must match bounded questions", 422
                )
            for index, scenario in enumerate(view.scenarios):
                answer = answers.get(f"scenario:{index}")
                if answer:
                    outcome = (
                        rule.outcome
                        if rule
                        and matches(
                            rule,
                            cast(dict[str, Scalar], scenario["facts"]),
                            str(scenario["action"]),
                        )
                        else "unknown"
                    )
                    if answer != outcome:
                        raise DomainError(
                            "clarification_conflict",
                            "Update the rule to match the intended scenario outcome",
                            409,
                        )
            selected = selected.model_copy(
                update={"statement": body.statement or selected.statement, "rule": rule}
            )
            candidates = tuple(selected if c.id == selected.id else c for c in view.candidates)
            ready = required.issubset(answers) and (
                not selected.assumptions or body.accept_assumptions
            )
            item.payload = {
                **item.payload,
                "view": {
                    **item.payload["view"],
                    "candidates": [c.model_dump(mode="json") for c in candidates],
                    "selected_candidate_id": selected.id,
                    "answers": answers,
                },
                "assumptions_accepted": body.accept_assumptions,
            }
            # A new clarification version requires a new preparation reservation.
            # The prior request remains stale even if its first attempt failed.
            item.payload.pop("prepare_digest", None)
            item.version += 1
            item.status = "ready_for_review" if ready else "needs_clarification"
            db.add(
                ModelOperation(
                    workspace_id=item.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    kind="clarification",
                    request_digest=hashed,
                    status="completed",
                    result={"version": item.version},
                )
            )
            return self.view(item)

    def prepare(
        self, p: Principal, rid: str, token: str, sid: str, body: PrepareDraft
    ) -> TeachingView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            hashed = fingerprint(body.model_dump(mode="json"))
            if item.payload.get("prepare_digest"):
                if item.payload["prepare_digest"] != hashed:
                    raise DomainError(
                        "idempotency_conflict", "Draft preparation input changed", 409
                    )
                if item.status == "drafted":
                    return self.view(item)
            if item.version != body.expected_version or item.status != "ready_for_review":
                raise DomainError(
                    "teaching_conflict",
                    "Clarification and current-version review are required",
                    409,
                )
            if item.status == "cancelled":
                raise DomainError("teaching_state", "Teaching run was cancelled", 409)
            view = self.view(item)
            candidate = next(c for c in view.candidates if c.id == view.selected_candidate_id)
            source = view.source
            request = DraftRequest(
                operation_id=body.operation_id,
                statement=candidate.statement,
                source_commit=source.source_commit,
                files=source.files,
                expected_snapshot_id=source.expected_snapshot_id,
                rule=candidate.rule,
                decision_id=body.decision_id,
                transcript_operation_id=source.transcript_operation_id,
            )
            provenance = {
                "run_id": sid,
                "version": item.version,
                "candidate": candidate.model_dump(mode="json"),
                "answers": view.answers,
                "assumptions_accepted": item.payload.get("assumptions_accepted", False),
                "selections": {
                    path: {key: value for key, value in source.items() if key != "text"}
                    for path, source in item.payload["input"]["sources"].items()
                },
            }
            item.payload = {**item.payload, "prepare_digest": hashed}
            item.status = "processing"
        try:
            fresh = self.manager.access.principal(token)
            draft = self.manager.create(fresh, rid, token, request, grounding=provenance)
        except Exception:
            with Session(self.manager.engine) as db, db.begin():
                item = self.scoped(db, p, rid, token, sid)
                if item.status == "processing":
                    item.status = "ready_for_review"
            raise
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status != "processing":
                raise DomainError("teaching_state", "Teaching run was cancelled", 409)
            item.payload = {
                **item.payload,
                "view": {**item.payload["view"], "draft": draft.model_dump(mode="json")},
            }
            item.status = "drafted"
            item.version += 1
            return self.view(item)

    def cancel(self, p: Principal, rid: str, token: str, sid: str) -> TeachingView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status != "drafted":
                item.status = "cancelled"
            return self.view(item)
