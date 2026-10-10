from dataclasses import dataclass

from tacit_contracts.models import Role


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Grant:
    workspace_id: str
    role: Role
    repository_id: str | None = None


@dataclass(frozen=True)
class Principal:
    user_id: str
    agent: bool
    memberships: frozenset[str]
    github_repositories: frozenset[str]
    grants: tuple[Grant, ...]


def authorize(
    principal: Principal, workspace: str, repository: str, *, write: bool = False
) -> None:
    # These sets represent fresh verified provider access, not claims from client input.
    if workspace not in principal.memberships or repository not in principal.github_repositories:
        raise DomainError("not_found", "Repository unavailable", 404)
    roles = {
        g.role
        for g in principal.grants
        if g.workspace_id == workspace and g.repository_id in (None, repository)
    }
    if not roles.intersection({Role.LEARNER, Role.MAINTAINER}):
        raise DomainError("forbidden", "A repository content role is required")
    if write and (principal.agent or Role.MAINTAINER not in roles):
        raise DomainError("forbidden", "Human maintainer permission is required")
