"""Deduplicated webhook ingestion and immediate conservative access revocation."""

import hashlib
import json
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tacit_domain.access import DomainError

from tacit_api.db import Credential, Installation, Repository, User, WebhookDelivery


def ingest(engine: Engine, delivery: str, event: str, body: bytes) -> dict[str, str]:
    if not delivery or len(delivery) > 128 or not event or len(event) > 64:
        raise DomainError("unsupported_schema", "Missing webhook delivery metadata", 422)
    try:
        payload: Any = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise DomainError("unsupported_schema", "Invalid webhook payload", 422) from None
    body_hash = hashlib.sha256(body).hexdigest()
    try:
        with Session(engine) as db, db.begin():
            existing = db.get(WebhookDelivery, delivery)
            if existing:
                if existing.body_hash != body_hash or existing.event != event:
                    raise DomainError("conflict", "Webhook delivery ID reused", 409)
                return {"status": "duplicate"}
            db.add(WebhookDelivery(id=delivery, event=event, body_hash=body_hash))
            db.flush()
            action = payload.get("action")
            installation_data = payload.get("installation", {})
            if not isinstance(installation_data, dict):
                raise DomainError("unsupported_schema", "Invalid installation payload", 422)
            install = db.scalar(
                select(Installation)
                .where(Installation.github_id == str(installation_data.get("id", "")))
                .with_for_update()
            )
            if install and event == "installation" and action in {"deleted", "suspend"}:
                install.active = False
            if install and event == "installation" and action == "unsuspend":
                install.active = True
            if install and event == "installation_repositories":
                removed = payload.get("repositories_removed", [])
                if not isinstance(removed, list) or not all(isinstance(r, dict) for r in removed):
                    raise DomainError("unsupported_schema", "Invalid repository payload", 422)
                for item in removed:
                    repo = db.scalar(
                        select(Repository).where(
                            Repository.workspace_id == install.workspace_id,
                            Repository.github_id == str(item.get("id", "")),
                        )
                    )
                    if repo:
                        repo.enabled = False
                added = payload.get("repositories_added", [])
                if not isinstance(added, list) or not all(isinstance(r, dict) for r in added):
                    raise DomainError("unsupported_schema", "Invalid repository payload", 422)
                for item in added:
                    github_id = str(item.get("id", ""))
                    if not github_id.isdigit() or not isinstance(item.get("full_name"), str):
                        raise DomainError("unsupported_schema", "Invalid repository payload", 422)
                    repo = db.scalar(select(Repository).where(Repository.github_id == github_id))
                    if repo and repo.workspace_id == install.workspace_id:
                        repo.enabled = True
                    elif repo is None:
                        db.add(
                            Repository(
                                id="gh-" + github_id,
                                github_id=github_id,
                                workspace_id=install.workspace_id,
                                full_name=item["full_name"],
                                enabled=True,
                            )
                        )
            if event == "github_app_authorization" and action == "revoked":
                sender = payload.get("sender", {})
                if not isinstance(sender, dict):
                    raise DomainError("unsupported_schema", "Invalid sender payload", 422)
                user = db.scalar(select(User).where(User.github_id == str(sender.get("id", ""))))
                if user:
                    for credential in db.scalars(
                        select(Credential).where(Credential.user_id == user.id)
                    ):
                        credential.revoked = True
            return {"status": "accepted"}
    except IntegrityError:
        # Concurrent delivery deduplication must verify the persisted digest too.
        with Session(engine) as db:
            existing = db.get(WebhookDelivery, delivery)
            if existing and existing.body_hash == body_hash and existing.event == event:
                return {"status": "duplicate"}
        raise DomainError("conflict", "Webhook delivery could not be committed", 409) from None
