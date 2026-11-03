"""Scoped role configuration and per-request team-derived grants.

No team grant is copied into permanent individual roles. All policy mutations
share the workspace lock used by publication and direct-role administration.
"""

import hashlib
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_contracts.models import (
    CustomRoleGrantRequest,
    CustomRolePermissions,
    CustomRoleRequest,
    KnowledgePermission,
    Role,
    TeamRoleRequest,
)
from tacit_domain.access import DomainError, Grant, Principal

from tacit_api.db import (
    CustomRole,
    CustomRoleAssignment,
    Installation,
    Membership,
    Repository,
    RoleAssignment,
    TeamRoleMapping,
    User,
    Workspace,
)

if TYPE_CHECKING:
    from tacit_api.access import AccessService

MAX_ROLES = 100
MAX_MAPPINGS = 20
MAX_TEAM_CHECKS = 64


def permission_role(values: list[str]) -> Role:
    try:
        permissions = CustomRolePermissions.model_validate({"permissions": values}).permissions
    except ValidationError:
        raise DomainError("service_unavailable", "Invalid custom role policy", 503) from None
    return Role.MAINTAINER if KnowledgePermission.WRITE in permissions else Role.LEARNER


def effective_grants(
    access: "AccessService",
    db: Session,
    uid: str,
    memberships: set[str] | frozenset[str],
    *,
    provider_token: str,
    organizations: set[str],
    repository_ids: set[str] | frozenset[str],
    active_team_mappings: frozenset[str] | None = None,
) -> tuple[Grant, ...]:
    grants = [
        Grant(g.workspace_id, Role(g.role), g.repository_id)
        for g in db.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == uid, RoleAssignment.workspace_id.in_(memberships)
            )
        )
    ]
    definitions = {
        (r.workspace_id, r.id): r
        for r in db.scalars(
            select(CustomRole).where(
                CustomRole.workspace_id.in_(memberships), CustomRole.active.is_(True)
            )
        )
    }
    for assignment in db.scalars(
        select(CustomRoleAssignment).where(
            CustomRoleAssignment.user_id == uid, CustomRoleAssignment.workspace_id.in_(memberships)
        )
    ):
        definition = definitions.get((assignment.workspace_id, assignment.custom_role_id))
        if definition is None:
            raise DomainError("service_unavailable", "Custom role definition unavailable", 503)
        grants.append(
            Grant(
                assignment.workspace_id,
                permission_role(definition.permissions),
                assignment.repository_id,
                "custom_role",
                assignment.id,
            )
        )
    if active_team_mappings == frozenset():
        # Control-plane authorization needs no team authority. Likewise, a local
        # write check cannot invent team proof absent from its authenticated principal.
        return tuple(grants)
    mappings = [
        m
        for m in db.scalars(
            select(TeamRoleMapping).where(
                TeamRoleMapping.workspace_id.in_(memberships & organizations)
            )
        )
        if m.repository_id is None or m.repository_id in repository_ids
    ]
    if not mappings:
        return tuple(grants)
    teams = {(m.workspace_id, m.github_team_id) for m in mappings}
    if len(teams) > MAX_TEAM_CHECKS:
        raise DomainError("service_unavailable", "Team verification budget exceeded", 503)
    verified: dict[tuple[str, str], bool] = {}
    if active_team_mappings is None:
        user = db.get(User, uid)
        with access.github.request_session():
            identity = access.github.identity(provider_token)
            if user is None or str(identity.id) != user.github_id:
                raise DomainError("unauthenticated", "GitHub team subject changed", 401)
            for wid, team_id in sorted(teams):
                workspace = db.get(Workspace, wid)
                if workspace is None:
                    raise DomainError("not_found", "Workspace unavailable", 404)
                verified[(wid, team_id)] = access.github.team_member(
                    provider_token, workspace.github_owner_id, team_id, identity.login
                )
    for mapping in mappings:
        active = (
            mapping.id in active_team_mappings
            if active_team_mappings is not None
            else verified[(mapping.workspace_id, mapping.github_team_id)]
        )
        if not active:
            continue
        if mapping.custom_role_id is not None:
            definition = definitions.get((mapping.workspace_id, mapping.custom_role_id))
            if definition is None:
                raise DomainError("service_unavailable", "Custom role definition unavailable", 503)
            role = permission_role(definition.permissions)
        elif mapping.role in {Role.LEARNER.value, Role.MAINTAINER.value}:
            role = Role(mapping.role)
        else:
            raise DomainError("service_unavailable", "Invalid mapped content role", 503)
        grants.append(
            Grant(mapping.workspace_id, role, mapping.repository_id, "github_team", mapping.id)
        )
    return tuple(grants)


class RolePolicies:
    def __init__(self, access: "AccessService"):
        self.access, self.engine = access, access.engine

    def scope(self, db: Session, p: Principal, wid: str, rid: str | None) -> None:
        if rid is not None:
            repo = db.get(Repository, (wid, rid))
            if repo is None or not repo.enabled or rid not in p.github_repositories:
                raise DomainError("not_found", "Repository unavailable", 404)

    def definition(self, db: Session, wid: str, role_id: str) -> CustomRole:
        result = db.get(CustomRole, (wid, role_id))
        if result is None or not result.active:
            raise DomainError("not_found", "Custom role unavailable", 404)
        return result

    def role_view(self, role: CustomRole) -> dict[str, object]:
        return {
            "id": role.id,
            "workspace_id": role.workspace_id,
            "description": role.description,
            "permissions": role.permissions,
            "version": role.version,
        }

    def mapping_view(self, row: TeamRoleMapping) -> dict[str, object]:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "github_team_id": row.github_team_id,
            "team_slug": row.team_slug,
            "role": row.role,
            "custom_role_id": row.custom_role_id,
            "repository_id": row.repository_id,
        }

    def list_roles(self, p: Principal, wid: str) -> list[dict[str, object]]:
        self.access.admin(p, wid)
        with Session(self.engine) as db:
            return [
                self.role_view(r)
                for r in db.scalars(
                    select(CustomRole)
                    .where(CustomRole.workspace_id == wid, CustomRole.active.is_(True))
                    .order_by(CustomRole.id)
                )
            ]

    def put_role(
        self, p: Principal, wid: str, role_id: str, body: CustomRoleRequest
    ) -> dict[str, object]:
        self.access.admin(p, wid)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", role_id) or role_id in set(Role):
            raise DomainError("unsupported_schema", "Choose a non-reserved custom role ID", 422)
        with Session(self.engine) as db, db.begin():
            self.access.lock_admin(db, p, wid)
            role = db.get(CustomRole, (wid, role_id))
            if role is not None and not role.active:
                raise DomainError("conflict", "Role ID is retired; choose a new ID", 409)
            if body.expected_version != (role.version if role else 0):
                raise DomainError("conflict", "Role changed; read its current version", 409)
            if role is None:
                count = db.scalar(
                    select(func.count())
                    .select_from(CustomRole)
                    .where(CustomRole.workspace_id == wid, CustomRole.active.is_(True))
                )
                if count is not None and count >= MAX_ROLES:
                    raise DomainError("role_quota", "Custom role limit reached", 409)
                role = CustomRole(workspace_id=wid, id=role_id, version=0)
                db.add(role)
            role.description, role.permissions = body.description, sorted(body.permissions)
            role.version += 1
            self.access.audit(
                db,
                p.user_id,
                wid,
                "custom_role.updated",
                {"id": role_id, **body.model_dump(mode="json")},
            )
            db.flush()
            return self.role_view(role)

    def delete_role(self, p: Principal, wid: str, role_id: str, version: int) -> None:
        self.access.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.access.lock_admin(db, p, wid)
            role = self.definition(db, wid, role_id)
            if role.version != version:
                raise DomainError("conflict", "Role changed; read its current version", 409)
            assigned = db.scalar(
                select(CustomRoleAssignment.id)
                .where(
                    CustomRoleAssignment.workspace_id == wid,
                    CustomRoleAssignment.custom_role_id == role_id,
                )
                .limit(1)
            )
            mapped = db.scalar(
                select(TeamRoleMapping.id)
                .where(
                    TeamRoleMapping.workspace_id == wid, TeamRoleMapping.custom_role_id == role_id
                )
                .limit(1)
            )
            if assigned or mapped:
                raise DomainError(
                    "conflict", "Revoke assignments and mappings before deleting this role", 409
                )
            role.active = False
            role.version += 1
            self.access.audit(
                db, p.user_id, wid, "custom_role.deleted", {"id": role_id, "version": version}
            )

    def list_assignments(self, p: Principal, wid: str) -> list[dict[str, object]]:
        self.access.admin(p, wid)
        with Session(self.engine) as db:
            return [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "custom_role_id": r.custom_role_id,
                    "repository_id": r.repository_id,
                }
                for r in db.scalars(
                    select(CustomRoleAssignment)
                    .where(CustomRoleAssignment.workspace_id == wid)
                    .order_by(CustomRoleAssignment.id)
                )
            ]

    def assign(self, p: Principal, wid: str, body: CustomRoleGrantRequest, revoke: bool) -> None:
        self.access.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.access.lock_admin(db, p, wid)
            if not revoke:
                self.scope(db, p, wid, body.repository_id)
            self.definition(db, wid, body.custom_role_id)
            member = db.get(Membership, (wid, body.user_id))
            if member is None or (not revoke and not member.active):
                raise DomainError("not_found", "Member unavailable", 404)
            assignment = db.scalar(
                select(CustomRoleAssignment).where(
                    CustomRoleAssignment.workspace_id == wid,
                    CustomRoleAssignment.user_id == body.user_id,
                    CustomRoleAssignment.custom_role_id == body.custom_role_id,
                    CustomRoleAssignment.repository_id == body.repository_id,
                )
            )
            if revoke:
                if assignment:
                    db.delete(assignment)
            else:
                self.access.target_eligible(db, body.user_id, wid, body.repository_id)
                if assignment is None:
                    count = db.scalar(
                        select(func.count())
                        .select_from(CustomRoleAssignment)
                        .where(CustomRoleAssignment.workspace_id == wid)
                    )
                    if count is not None and count >= 1000:
                        raise DomainError("role_quota", "Custom assignment limit reached", 409)
                    db.add(
                        CustomRoleAssignment(
                            id=str(uuid4()),
                            workspace_id=wid,
                            user_id=body.user_id,
                            custom_role_id=body.custom_role_id,
                            repository_id=body.repository_id,
                        )
                    )
            self.access.audit(
                db,
                p.user_id,
                wid,
                "custom_role.revoked" if revoke else "custom_role.granted",
                body.model_dump(mode="json"),
            )

    def list_mappings(self, p: Principal, wid: str) -> list[dict[str, object]]:
        self.access.admin(p, wid)
        with Session(self.engine) as db:
            return [
                self.mapping_view(m)
                for m in db.scalars(
                    select(TeamRoleMapping)
                    .where(TeamRoleMapping.workspace_id == wid)
                    .order_by(TeamRoleMapping.id)
                )
            ]

    def map_team(
        self, p: Principal, wid: str, token: str, body: TeamRoleRequest
    ) -> dict[str, object]:
        self.access.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.access.lock_admin(db, p, wid)
            self.scope(db, p, wid, body.repository_id)
            installation = db.get(Installation, wid)
            workspace = db.get(Workspace, wid)
            if (
                installation is None
                or not installation.active
                or installation.owner_type != "Organization"
                or workspace is None
            ):
                raise DomainError(
                    "conflict", "Team mappings require an active organization installation", 409
                )
            credential = self.access.credential(db, hashlib.sha256(token.encode()).hexdigest())
            if credential.agent or credential.user_id != p.user_id or not credential.provider_token:
                raise DomainError("forbidden", "Human administrator credential required")
            if body.custom_role_id is not None:
                self.definition(db, wid, body.custom_role_id)
            team = self.access.github.team(
                self.access.decrypt(credential.provider_token),
                installation.owner_login,
                body.team_slug,
            )
            if str(team.organization.id) != workspace.github_owner_id:
                raise DomainError("not_found", "Team does not belong to this workspace", 404)
            existing = db.scalar(
                select(TeamRoleMapping).where(
                    TeamRoleMapping.workspace_id == wid,
                    TeamRoleMapping.github_team_id == str(team.id),
                    TeamRoleMapping.role == body.role,
                    TeamRoleMapping.custom_role_id == body.custom_role_id,
                    TeamRoleMapping.repository_id == body.repository_id,
                )
            )
            if existing is not None:
                return self.mapping_view(existing)
            count = db.scalar(
                select(func.count())
                .select_from(TeamRoleMapping)
                .where(TeamRoleMapping.workspace_id == wid)
            )
            if count is not None and count >= MAX_MAPPINGS:
                raise DomainError("role_quota", "Team mapping limit reached", 409)
            mapping = TeamRoleMapping(
                id=str(uuid4()),
                workspace_id=wid,
                github_team_id=str(team.id),
                team_slug=team.slug,
                role=body.role,
                custom_role_id=body.custom_role_id,
                repository_id=body.repository_id,
            )
            db.add(mapping)
            self.access.audit(
                db,
                p.user_id,
                wid,
                "team_role.mapped",
                {"id": mapping.id, "github_team_id": str(team.id), **body.model_dump(mode="json")},
            )
            db.flush()
            return self.mapping_view(mapping)

    def unmap(self, p: Principal, wid: str, mapping_id: str) -> None:
        self.access.admin(p, wid)
        with Session(self.engine) as db, db.begin():
            self.access.lock_admin(db, p, wid)
            row = db.scalar(
                select(TeamRoleMapping).where(
                    TeamRoleMapping.id == mapping_id, TeamRoleMapping.workspace_id == wid
                )
            )
            if row is None:
                raise DomainError("not_found", "Team mapping unavailable", 404)
            db.delete(row)
            self.access.audit(db, p.user_id, wid, "team_role.unmapped", {"id": mapping_id})
