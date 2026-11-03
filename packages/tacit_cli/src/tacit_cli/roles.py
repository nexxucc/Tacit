"""Human administration of named content roles and organization team mappings."""

import json
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import ValidationError
from tacit_client.client import ClientError, TacitClient
from tacit_contracts.models import (
    CustomRoleGrantRequest,
    CustomRoleRequest,
    KnowledgePermission,
    TeamRoleRequest,
)

custom = typer.Typer(help="Manage workspace custom content roles; human administrators only.")
teams = typer.Typer(help="Map GitHub organization teams to scoped content roles.")
Workspace = Annotated[str, typer.Option("--workspace")]
Repository = Annotated[str | None, typer.Option("--repo")]


def path(workspace: str, suffix: str) -> str:
    return f"/v1/workspaces/{quote(workspace, safe='')}/{suffix}"


def request(method: str, endpoint: str, body: object = None) -> None:
    try:
        with TacitClient() as client:
            value = client.request(method, endpoint, json=body, timeout=150)
        typer.echo(json.dumps(value, indent=2))
    except ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@custom.command("list")
def list_roles(workspace: Workspace) -> None:
    request("GET", path(workspace, "custom-roles"))


@custom.command("put")
def put(
    role_id: str,
    workspace: Workspace,
    permission: Annotated[list[KnowledgePermission], typer.Option("--permission")],
    expected_version: Annotated[int, typer.Option("--expected-version", min=0)],
    description: str = "",
) -> None:
    """Create with version zero, or update the exact current version. No admin capability."""
    try:
        body = CustomRoleRequest(
            permissions=tuple(permission),
            expected_version=expected_version,
            description=description,
        )
    except ValidationError:
        raise typer.BadParameter(
            "Use unique knowledge.read / knowledge.write permissions; write requires read"
        ) from None
    request(
        "PUT",
        path(workspace, "custom-roles/" + quote(role_id, safe="")),
        body.model_dump(mode="json"),
    )


@custom.command("delete")
def delete(
    role_id: str,
    workspace: Workspace,
    expected_version: Annotated[int, typer.Option("--expected-version", min=1)],
) -> None:
    request(
        "DELETE",
        path(workspace, "custom-roles/" + quote(role_id, safe="") + f"?version={expected_version}"),
    )


@custom.command("assignments")
def assignments(workspace: Workspace) -> None:
    request("GET", path(workspace, "custom-role-assignments"))


def assignment(user: str, role_id: str, workspace: str, repo: str | None, revoke: bool) -> None:
    try:
        body = CustomRoleGrantRequest(user_id=user, custom_role_id=role_id, repository_id=repo)
    except ValidationError:
        raise typer.BadParameter("Invalid user, custom role ID or repository scope") from None
    request(
        "POST",
        path(workspace, "custom-role-assignments" + ("/revoke" if revoke else "")),
        body.model_dump(mode="json"),
    )


@custom.command("grant")
def grant(user: str, role_id: str, workspace: Workspace, repo: Repository = None) -> None:
    assignment(user, role_id, workspace, repo, False)


@custom.command("revoke")
def revoke(user: str, role_id: str, workspace: Workspace, repo: Repository = None) -> None:
    assignment(user, role_id, workspace, repo, True)


@teams.command("list")
def list_mappings(workspace: Workspace) -> None:
    request("GET", path(workspace, "team-role-mappings"))


@teams.command("add")
def add(
    team_slug: str,
    workspace: Workspace,
    role: str | None = None,
    custom_role: str | None = None,
    repo: Repository = None,
) -> None:
    """Choose --role learner/maintainer OR --custom-role ID. No GitHub writes occur."""
    try:
        body = TeamRoleRequest.model_validate(
            {
                "team_slug": team_slug,
                "role": role,
                "custom_role_id": custom_role,
                "repository_id": repo,
            }
        )
    except ValidationError:
        raise typer.BadParameter(
            "Choose one --role learner/maintainer or --custom-role, with a valid team slug"
        ) from None
    request("POST", path(workspace, "team-role-mappings"), body.model_dump(mode="json"))


@teams.command("remove")
def remove(mapping_id: str, workspace: Workspace) -> None:
    request("DELETE", path(workspace, "team-role-mappings/" + quote(mapping_id, safe="")))


@teams.command("effective")
def effective(workspace: Workspace) -> None:
    """Inspect your current grants and their provenance, with fresh team membership checks."""
    request("GET", path(workspace, "effective-access"))
