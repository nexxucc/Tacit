"""Bounded transactional outbox relay, retention and fresh access reconciliation."""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_domain.access import DomainError

from tacit_api.access import AccessService, now
from tacit_api.app import Settings
from tacit_api.artifacts import Artifacts, Objects
from tacit_api.db import (
    Credential,
    DecisionRevision,
    ModelOperation,
    OutboxEvent,
    PublicationIndex,
    TeachingRun,
)
from tacit_api.publication import PublicationService
from tacit_api.voice import Prisma
from tacit_api.voice_sessions import VoiceSessions

logger = logging.getLogger("tacit.worker")


def relay(access: AccessService, limit: int = 50) -> int:
    processed = 0
    with Session(access.engine) as db, db.begin():
        events = list(
            db.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.processed_at.is_(None),
                    OutboxEvent.next_attempt_at <= now(),
                    OutboxEvent.attempts < 8,
                )
                .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        for item in events:
            try:
                with db.begin_nested():
                    event = item.payload
                    if event.get("event") != "knowledge.confirmed":
                        raise ValueError("unsupported_event")
                    rid, revision_id = str(event["repository_id"]), str(event["revision_id"])
                    revision = db.get(DecisionRevision, (item.workspace_id, rid, revision_id))
                    if revision is None:
                        raise ValueError("missing_revision")
                    # This projection stays in the same tenant DB; it never delivers externally.
                    # Exact snapshot reads remain authoritative even before projection catch-up.
                    key = (item.workspace_id, rid, revision_id)
                    if db.get(PublicationIndex, key) is None:
                        db.add(
                            PublicationIndex(
                                workspace_id=item.workspace_id,
                                repository_id=rid,
                                revision_id=revision_id,
                                statement=str(revision.content["statement"]),
                                source_commit=str(revision.content["source_commit"]),
                            )
                        )
                    db.flush()
                    item.processed_at, item.error_code = now(), None
                processed += 1
            except Exception:
                item.attempts += 1
                item.next_attempt_at = now() + timedelta(seconds=min(3600, 2**item.attempts))
                item.error_code = "outbox_handler_failed"
                logger.warning(
                    "Outbox handler failed: event_id=%s attempt=%d", item.id, item.attempts
                )
    return processed


def reconcile(access: AccessService, limit: int = 20) -> int:
    with Session(access.engine) as db:
        tokens = [
            (c.token_hash, c.provider_token)
            for c in db.scalars(
                select(Credential)
                .where(
                    Credential.agent.is_(False),
                    Credential.revoked.is_(False),
                    Credential.expires_at > now(),
                    Credential.next_reconcile_at <= now(),
                )
                .order_by(Credential.next_reconcile_at)
                .limit(limit)
            )
        ]
    for token_hash, provider_token in tokens:
        if not provider_token:
            continue
        try:
            eligible = access.github.eligibility(access.decrypt(provider_token))
            with Session(access.engine) as db, db.begin():
                credential = access.credential(db, token_hash)
                from tacit_api.db import User

                user = db.get(User, credential.user_id)
                if user:
                    access.sync(db, user.id, user.github_id, eligible)
                    credential.next_reconcile_at = now() + timedelta(seconds=60)
        except DomainError as exc:
            with Session(access.engine) as db, db.begin():
                failed_credential = db.get(Credential, token_hash)
                if failed_credential:
                    if exc.status == 401:
                        failed_credential.revoked = True
                    failed_credential.next_reconcile_at = now() + timedelta(seconds=60)
    return len(tokens)


def prune_transient(access: AccessService) -> int:
    with Session(access.engine) as db, db.begin():
        runs = list(
            db.scalars(
                select(TeachingRun)
                .where(TeachingRun.expires_at <= now(), TeachingRun.status != "expired")
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for run in runs:
            run.payload, run.status = {}, "expired"
        operations = list(
            db.scalars(
                select(ModelOperation)
                .where(
                    ModelOperation.created_at <= now() - timedelta(days=1),
                    ModelOperation.status != "expired",
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for operation in operations:
            operation.result, operation.status = None, "expired"
        return len(runs) + len(operations)


def cycle(access: AccessService, settings: Settings) -> dict[str, int]:
    manager = PublicationService(access)
    return {
        "outbox": relay(access),
        "transient_expired": prune_transient(access),
        "voice_expired": VoiceSessions(
            manager, Prisma(settings.gnani_api_key), settings.voice_retention_seconds
        ).prune(),
        "artifacts_expired": Artifacts(
            manager,
            Objects(
                settings.artifact_directory, settings.artifact_bucket, settings.artifact_endpoint
            ),
            settings.voice_retention_seconds,
        ).prune(),
        "credentials_reconciled": reconcile(access),
    }
