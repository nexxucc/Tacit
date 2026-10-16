"""Transactional access management. Provider access is checked fresh, never encoded in roles."""

import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from tacit_contracts.models import AgentTokenRequest, InvitationRequest, Role, RoleChange
from tacit_domain.access import DomainError, Grant, Principal, authorize

from tacit_api.db import (
    AuditEvent,
    Credential,
    DevicePairing,
    Installation,
    Invitation,
    Membership,
    Repository,
    RoleAssignment,
    User,
    Workspace,
)
from tacit_api.github import EligibleInstallation, GitHub


def now() -> datetime:
    return datetime.now(UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def valid_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class AccessService:
    def __init__(
        self, engine: Engine, github: GitHub, encryption_key: str, read_cache_seconds: int = 0
    ):
        self.engine, self.github = engine, github
        self.cipher = Fernet(encryption_key.encode())
        self.read_cache_seconds = max(0, min(60, read_cache_seconds))
        self._eligibility_cache: OrderedDict[
            str, tuple[float, tuple[EligibleInstallation, ...]]
        ] = OrderedDict()
        self._cache_lock = threading.Lock()

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except InvalidToken:
            raise DomainError("service_unavailable", "Credential decryption failed", 503) from None

    def start(self) -> dict[str, object]:
        code = self.github.start()
        token = secrets.token_urlsafe(32)
        with Session(self.engine) as db, db.begin():
            db.add(
                DevicePairing(
                    token_hash=digest(token),
                    device_code=self.encrypt(code.device_code),
                    interval=code.interval,
                    expires_at=now() + timedelta(seconds=code.expires_in),
                    next_poll_at=now() + timedelta(seconds=code.interval),
                    consumed=False,
                )
            )
        return {
            "pairing_token": token,
            "user_code": code.user_code,
            "verification_uri": code.verification_uri,
            "expires_in": code.expires_in,
            "interval": code.interval,
        }

    def poll(self, token: str) -> dict[str, object]:
        # The row lock serializes polls and consumes a pairing exactly once.
        with Session(self.engine) as db, db.begin():
            pairing = db.scalar(
                select(DevicePairing)
                .where(DevicePairing.token_hash == digest(token))
                .with_for_update()
            )
            if pairing is None or pairing.consumed or valid_time(pairing.expires_at) <= now():
                raise DomainError("unauthenticated", "Pairing expired or consumed", 401)
            if valid_time(pairing.next_poll_at) > now():
                return {"status": "pending", "interval": pairing.interval}
            result = self.github.poll(self.decrypt(pairing.device_code))
            error = result.get("error")
            if error in {"authorization_pending", "slow_down"}:
                if error == "slow_down":
                    pairing.interval += 5
                pairing.next_poll_at = now() + timedelta(seconds=pairing.interval)
                return {"status": "pending", "interval": pairing.interval}
            if error:
                pairing.consumed = True
                return {"status": "denied"}
            provider_token = result.get("access_token")
            if not isinstance(provider_token, str) or not provider_token:
                raise DomainError("provider_unavailable", "GitHub did not return a credential", 503)
            identity = self.github.identity(provider_token)
            eligibility = self.github.eligibility(provider_token)
            uid = f"gh-{identity.id}"
            if db.get(User, uid) is None:
                db.add(User(id=uid, github_id=str(identity.id)))
                db.flush()
            self.sync(db, uid, str(identity.id), eligibility)
            expiry = result.get("expires_in", 28800)
            if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry <= 0:
                raise DomainError("provider_unavailable", "Invalid GitHub credential expiry", 503)
            lifetime = min(expiry, 28800)
            credential = secrets.token_urlsafe(32)
            db.add(
                Credential(
                    token_hash=digest(credential),
                    user_id=uid,
                    agent=False,
                    repository_ids=[],
                    provider_token=self.encrypt(provider_token),
                    expires_at=now() + timedelta(seconds=lifetime),
                    revoked=False,
                )
            )
            pairing.consumed = True
            pairing.device_code = "consumed"
            return {"status": "authorized", "token": credential, "expires_in": lifetime}

    def add_role(self, db: Session, uid: str, wid: str, role: Role, rid: str | None = None) -> None:
        existing = db.scalar(
            select(RoleAssignment).where(
                RoleAssignment.user_id == uid,
                RoleAssignment.workspace_id == wid,
                RoleAssignment.role == role.value,
                RoleAssignment.repository_id == rid,
            )
        )
        if existing is None:
            db.add(
                RoleAssignment(
                    id=str(uuid4()),
                    user_id=uid,
                    workspace_id=wid,
                    repository_id=rid,
                    role=role.value,
                )
            )
            db.flush()

    def audit(
        self, db: Session, uid: str, wid: str, action: str, details: dict[str, object]
    ) -> None:
        db.add(
            AuditEvent(
                id=str(uuid4()), actor_id=uid, workspace_id=wid, action=action, details=details
            )
        )

    def sync(
        self, db: Session, uid: str, github_id: str, eligible: tuple[EligibleInstallation, ...]
    ) -> None:
        for item in eligible:
            install = item.installation
            wid = f"gh-{install.account.id}"
            workspace = db.get(Workspace, wid)
            if workspace is None:
                db.add(Workspace(id=wid, github_owner_id=str(install.account.id), active=True))
                db.flush()
            installation = db.get(Installation, wid)
            if installation is None:
                installation = Installation(
                    workspace_id=wid,
                    github_id=str(install.id),
                    owner_login=install.account.login,
                    owner_type=install.account.type,
                    active=True,
                )
                db.add(installation)
            else:
                if installation.github_id != str(install.id):
                    installation.active = True
                installation.github_id = str(install.id)
                installation.owner_login = install.account.login
            for repo in item.repositories:
                # Numeric repository IDs are globally unique and survive renames.
                if repo.owner.id != install.account.id:
                    continue
                rid = f"gh-{repo.id}"
                row = db.get(Repository, (wid, rid))
                if row is None:
                    # A transferred repository requires explicit reconciliation, not tenant reassignment.
                    prior = db.scalar(
                        select(Repository).where(Repository.github_id == str(repo.id))
                    )
                    if prior is not None:
                        continue
                    db.add(
                        Repository(
                            workspace_id=wid,
                            id=rid,
                            github_id=str(repo.id),
                            full_name=repo.full_name,
                            enabled=True,
                        )
                    )
                else:
                    row.full_name = repo.full_name
            personal_owner = install.account.type == "User" and str(install.account.id) == github_id
            invitation = db.get(Invitation, (wid, github_id))
            invited = invitation is not None and valid_time(invitation.expires_at) > now()
            membership = db.get(Membership, (wid, uid))
            if membership is None and (personal_owner or item.member or invited):
                membership = Membership(workspace_id=wid, user_id=uid, active=True, invited=invited)
                db.add(membership)
                db.flush()
                self.audit(db, uid, wid, "membership.enrolled", {"invited": invited})
                if personal_owner or item.member:
                    self.add_role(db, uid, wid, Role.LEARNER)
                elif invitation is not None:
                    for rid in invitation.repository_ids:
                        self.add_role(db, uid, wid, Role.LEARNER, rid)
                if personal_owner or item.owner:
                    self.add_role(db, uid, wid, Role.ADMIN)
                    self.audit(db, uid, wid, "role.bootstrap", {"role": Role.ADMIN.value})
                if personal_owner:
                    self.add_role(db, uid, wid, Role.MAINTAINER)
            # A local suspension stays suspended, even after a new GitHub sign-in.

    def credential(self, db: Session, token_hash: str) -> Credential:
        row = db.get(Credential, token_hash)
        if row is None or row.revoked or valid_time(row.expires_at) <= now():
            raise DomainError("unauthenticated", "Credential expired or revoked", 401)
        return row

    def verified_eligibility(
        self, provider_token: str, *, fresh: bool = True
    ) -> tuple[EligibleInstallation, ...]:
        key = digest(provider_token)
        if not fresh and self.read_cache_seconds:
            with self._cache_lock:
                entry = self._eligibility_cache.get(key)
                if entry and entry[0] > time.monotonic():
                    self._eligibility_cache.move_to_end(key)
                    return entry[1]
        verified = self.github.eligibility(provider_token)
        with self._cache_lock:
            self._eligibility_cache[key] = (time.monotonic() + self.read_cache_seconds, verified)
            self._eligibility_cache.move_to_end(key)
            while len(self._eligibility_cache) > 256:
                self._eligibility_cache.popitem(last=False)
        return verified

    def principal(self, token: str, *, fresh: bool = True) -> Principal:
        with Session(self.engine) as db, db.begin():
            row = self.credential(db, digest(token))
            parent = self.credential(db, row.parent_hash) if row.agent and row.parent_hash else row
            if parent.agent or not parent.provider_token:
                raise DomainError("unauthenticated", "Invalid credential delegation", 401)
            user = db.get(User, row.user_id)
            if user is None or parent.user_id != row.user_id:
                raise DomainError("unauthenticated", "Invalid credential subject", 401)
            eligible = self.verified_eligibility(self.decrypt(parent.provider_token), fresh=fresh)
            self.sync(db, user.id, user.github_id, eligible)
            db.flush()
            memberships: set[str] = set()
            repositories: set[str] = set()
            for item in eligible:
                wid = f"gh-{item.installation.account.id}"
                member = db.get(Membership, (wid, user.id))
                workspace = db.get(Workspace, wid)
                installation = db.get(Installation, wid)
                personal_owner = (
                    item.installation.account.type == "User"
                    and str(item.installation.account.id) == user.github_id
                )
                if not (
                    member
                    and member.active
                    and workspace
                    and workspace.active
                    and installation
                    and installation.active
                    and (personal_owner or item.member or member.invited)
                ):
                    continue
                memberships.add(wid)
                for repo in item.repositories:
                    rid = f"gh-{repo.id}"
                    stored = db.get(Repository, (wid, rid))
                    if stored and stored.enabled and (not row.agent or rid in row.repository_ids):
                        repositories.add(rid)
            grants = tuple(
                Grant(g.workspace_id, Role(g.role), g.repository_id)
                for g in db.scalars(
                    select(RoleAssignment).where(RoleAssignment.user_id == row.user_id)
                )
                if g.workspace_id in memberships
            )
            if row.agent:
                scoped_workspaces = {
                    r.workspace_id
                    for r in db.scalars(select(Repository).where(Repository.id.in_(repositories)))
                }
                memberships &= scoped_workspaces
            return Principal(
                row.user_id, row.agent, frozenset(memberships), frozenset(repositories), grants
            )

    def admin(self, p: Principal, wid: str) -> None:
        if wid not in p.memberships:
            raise DomainError("not_found", "Workspace unavailable", 404)
        if p.agent or not any(g.workspace_id == wid and g.role == Role.ADMIN for g in p.grants):
            raise DomainError("forbidden", "Human workspace administrator permission is required")

    def lock_admin(self, db: Session, p: Principal, wid: str) -> None:
        workspace = db.scalar(select(Workspace).where(Workspace.id == wid).with_for_update())
        member = db.get(Membership, (wid, p.user_id))
        grant = db.scalar(
            select(RoleAssignment).where(
                RoleAssignment.workspace_id == wid,
                RoleAssignment.user_id == p.user_id,
                RoleAssignment.role == Role.ADMIN.value,
            )
        )
        if not workspace or not workspace.active or not member or not member.active or not grant:
            raise DomainError("forbidden", "Administrator permission changed")

    def target_eligible(self, db: Session, uid: str, wid: str, rid: str | None) -> None:
        credential = db.scalar(
            select(Credential)
            .where(
                Credential.user_id == uid,
                Credential.agent.is_(False),
                Credential.revoked.is_(False),
                Credential.expires_at > now(),
            )
            .order_by(Credential.expires_at.desc())
        )
        if credential is None or not credential.provider_token:
            raise DomainError("conflict", "Member must sign in before receiving a role", 409)
        eligibility = self.github.eligibility(self.decrypt(credential.provider_token))
        if not any(
            f"gh-{item.installation.account.id}" == wid
            and (rid is None or any(f"gh-{r.id}" == rid for r in item.repositories))
            for item in eligibility
        ):
            raise DomainError(
                "forbidden", "Member's GitHub repository access could not be verified"
            )

    def change_role(self, p: Principal, wid: str, body: RoleChange, revoke: bool) -> None:
        self.admin(p, wid)
        if body.role == Role.ADMIN and body.repository_id is not None:
            raise DomainError(
                "unsupported_schema", "Administrator grants must be workspace-scoped", 422
            )
        with Session(self.engine) as db, db.begin():
            # Lock workspace to serialize concurrent grant/revoke and last-admin checks.
            self.lock_admin(db, p, wid)
            membership = db.get(Membership, (wid, body.user_id))
            if membership is None or not membership.active:
                raise DomainError("not_found", "Member unavailable", 404)
            if body.repository_id is not None and body.repository_id not in p.github_repositories:
                raise DomainError("not_found", "Repository unavailable", 404)
            if (
                body.repository_id is not None
                and db.get(Repository, (wid, body.repository_id)) is None
            ):
                raise DomainError("not_found", "Repository unavailable", 404)
            roles = list(
                db.scalars(
                    select(RoleAssignment).where(
                        RoleAssignment.workspace_id == wid,
                        RoleAssignment.user_id == body.user_id,
                        RoleAssignment.role == body.role.value,
                        RoleAssignment.repository_id == body.repository_id,
                    )
                )
            )
            if revoke:
                if body.role == Role.ADMIN:
                    admins = list(
                        db.scalars(
                            select(RoleAssignment)
                            .join(
                                Membership,
                                (Membership.workspace_id == RoleAssignment.workspace_id)
                                & (Membership.user_id == RoleAssignment.user_id),
                            )
                            .where(
                                RoleAssignment.workspace_id == wid,
                                RoleAssignment.role == Role.ADMIN.value,
                                Membership.active.is_(True),
                            )
                        )
                    )
                    if roles and len(admins) <= len(roles):
                        raise DomainError(
                            "conflict", "Cannot revoke the last active administrator", 409
                        )
                for role in roles:
                    db.delete(role)
            else:
                self.target_eligible(db, body.user_id, wid, body.repository_id)
                self.add_role(db, body.user_id, wid, body.role, body.repository_id)
            self.audit(
                db,
                p.user_id,
                wid,
                "role.revoked" if revoke else "role.granted",
                body.model_dump(mode="json"),
            )

    def invite(self, p: Principal, wid: str, body: InvitationRequest) -> None:
        self.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.lock_admin(db, p, wid)
            for rid in body.repository_ids:
                if rid not in p.github_repositories or db.get(Repository, (wid, rid)) is None:
                    raise DomainError("not_found", "Repository unavailable", 404)
            existing = db.get(Invitation, (wid, body.github_id))
            if existing is None:
                db.add(
                    Invitation(
                        workspace_id=wid,
                        github_id=body.github_id,
                        repository_ids=list(body.repository_ids),
                        expires_at=now() + timedelta(days=7),
                    )
                )
            else:
                existing.repository_ids = list(body.repository_ids)
                existing.expires_at = now() + timedelta(days=7)
            self.audit(db, p.user_id, wid, "invitation.created", body.model_dump(mode="json"))

    def suspend(self, p: Principal, wid: str, uid: str) -> None:
        self.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.lock_admin(db, p, wid)
            member = db.get(Membership, (wid, uid))
            if member is None:
                raise DomainError("not_found", "Member unavailable", 404)
            if any(
                g.user_id == uid
                for g in db.scalars(
                    select(RoleAssignment).where(
                        RoleAssignment.workspace_id == wid, RoleAssignment.role == Role.ADMIN.value
                    )
                )
            ):
                raise DomainError(
                    "conflict", "Revoke administrator role before suspending a member", 409
                )
            member.active = False
            self.audit(db, p.user_id, wid, "membership.suspended", {"user_id": uid})

    def delegate(self, token: str, p: Principal, body: AgentTokenRequest) -> dict[str, object]:
        if p.agent:
            raise DomainError("forbidden", "Agents cannot delegate credentials")
        with Session(self.engine) as db, db.begin():
            parent = self.credential(db, digest(token))
            for rid in body.repository_ids:
                repo = db.scalar(
                    select(Repository).where(Repository.id == rid, Repository.enabled.is_(True))
                )
                if repo is None:
                    raise DomainError("not_found", "Repository unavailable", 404)
                authorize(p, repo.workspace_id, rid)
            expires = min(valid_time(parent.expires_at), now() + timedelta(seconds=body.expires_in))
            value = secrets.token_urlsafe(32)
            db.add(
                Credential(
                    token_hash=digest(value),
                    user_id=p.user_id,
                    agent=True,
                    parent_hash=parent.token_hash,
                    repository_ids=list(body.repository_ids),
                    expires_at=expires,
                    revoked=False,
                )
            )
            return {"token": value, "expires_at": expires.isoformat(), "agent": True}

    def logout(self, token: str) -> None:
        with Session(self.engine) as db, db.begin():
            self.credential(db, digest(token)).revoked = True
