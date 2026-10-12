import json
import platform
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import httpx
import typer
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import (
    CredentialError,
    api_url,
    delete_token,
    save_token,
    secure_backend,
)
from tacit_contracts.models import (
    ContextRequest,
    DraftRequest,
    DraftView,
    PublicationRequest,
    QuestionRequest,
    Role,
    Rule,
    ScenarioRequest,
)

RepoOption = Annotated[str, typer.Option("--repo")]
SnapshotOption = Annotated[str, typer.Option("--snapshot")]
CommitOption = Annotated[str, typer.Option("--commit")]

app = typer.Typer(help="Tacit: source-bound project intent. Working command; not a public release.")
workspaces = typer.Typer()
repos = typer.Typer()
roles = typer.Typer()
members = typer.Typer()
app.add_typer(workspaces, name="workspaces")
app.add_typer(repos, name="repos")
app.add_typer(roles, name="roles")
app.add_typer(members, name="members")


def run(method: str, path: str, *, agent: bool = False, body: object = None) -> None:
    try:
        with TacitClient(agent=agent) as client:
            result = client.request(method, path, **({"json": body} if body is not None else {}))
        typer.echo(json.dumps(result, indent=2))
    except ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def encoded(value: str) -> str:
    return quote(value, safe="")


@app.command()
def whoami(agent: bool = False) -> None:
    run("GET", "/v1/auth/me", agent=agent)


@workspaces.command("list")
def list_workspaces(agent: bool = False) -> None:
    run("GET", "/v1/workspaces", agent=agent)


@repos.command("list")
def list_repos(workspace: Annotated[str, typer.Option("--workspace")], agent: bool = False) -> None:
    run("GET", f"/v1/workspaces/{encoded(workspace)}/repos", agent=agent)


@app.command()
def context(
    repo: RepoOption,
    snapshot: SnapshotOption,
    commit: CommitOption,
    files: list[str] | None = None,
    limit: int = 20,
    cursor: int = 0,
    agent: bool = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="JSON is the scaffold's default output")
    ] = False,
) -> None:
    """Retrieve exact confirmed text. Output is JSON in this scaffold."""
    body = ContextRequest(
        snapshot_id=snapshot,
        source_commit=commit,
        files=tuple(files or ()),
        limit=limit,
        cursor=cursor,
    )
    run(
        "POST", f"/v1/repos/{encoded(repo)}/context", agent=agent, body=body.model_dump(mode="json")
    )


@app.command()
def ask(
    question: str,
    repo: RepoOption,
    snapshot: SnapshotOption,
    commit: CommitOption,
    agent: bool = False,
) -> None:
    body = QuestionRequest(snapshot_id=snapshot, source_commit=commit, question=question)
    run(
        "POST",
        f"/v1/repos/{encoded(repo)}/questions",
        agent=agent,
        body=body.model_dump(mode="json"),
    )


@app.command()
def evaluate(
    scenario: Annotated[Path, typer.Option("--scenario")],
    repo: RepoOption,
    snapshot: SnapshotOption,
    commit: CommitOption,
    agent: bool = False,
) -> None:
    try:
        body = ScenarioRequest(
            snapshot_id=snapshot, source_commit=commit, facts=json.loads(scenario.read_text())
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Invalid scenario: {exc}", err=True)
        raise typer.Exit(2) from None
    run(
        "POST",
        f"/v1/repos/{encoded(repo)}/evaluate",
        agent=agent,
        body=body.model_dump(mode="json"),
    )


@app.command()
def history(
    decision: str,
    repo: RepoOption,
    snapshot: SnapshotOption,
    agent: bool = False,
    cursor: int = 0,
    limit: int = 20,
) -> None:
    run(
        "GET",
        f"/v1/repos/{encoded(repo)}/decisions/{encoded(decision)}/history?snapshot_id={encoded(snapshot)}&cursor={cursor}&limit={limit}",
        agent=agent,
    )


@app.command()
def doctor() -> None:
    """Local diagnostics only; never print tokens or make provider calls."""
    typer.echo(
        json.dumps(
            {
                "python": platform.python_version(),
                "credential_mode": "OS keyring or explicit environment",
                "audio": "not implemented",
                "production_auth": "GitHub device flow; backend configuration required",
            },
            indent=2,
        )
    )


@app.command()
def login() -> None:
    """Pair through GitHub, then store only the Tacit credential in the OS keyring."""
    try:
        secure_backend()
        with httpx.Client(
            base_url=api_url(), timeout=30, trust_env=False, follow_redirects=False
        ) as http:
            response = http.post("/v1/auth/device/start")
            response.raise_for_status()
            pairing = response.json()
            typer.echo(f"Open {pairing['verification_uri']} and enter {pairing['user_code']}")
            deadline = time.monotonic() + pairing["expires_in"]
            interval = pairing["interval"]
            while time.monotonic() < deadline:
                time.sleep(interval)
                response = http.post(
                    "/v1/auth/device/poll", json={"pairing_token": pairing["pairing_token"]}
                )
                response.raise_for_status()
                result = response.json()
                if result["status"] == "authorized":
                    try:
                        save_token(result["token"])
                    except CredentialError:
                        http.post(
                            "/v1/auth/logout",
                            headers={"Authorization": "Bearer " + result["token"]},
                        )
                        raise
                    typer.echo("Signed in. Tacit credential saved in the OS keyring.")
                    return
                if result["status"] != "pending":
                    raise CredentialError("GitHub authorization was denied")
                interval = result["interval"]
            raise CredentialError("GitHub pairing expired; run tacit login again")
    except (CredentialError, httpx.HTTPError, ValueError, KeyError) as exc:
        message = (
            str(exc)
            if isinstance(exc, CredentialError)
            else "GitHub pairing failed; check backend configuration/connectivity"
        )
        typer.echo(message, err=True)
        raise typer.Exit(1) from None


@app.command()
def logout() -> None:
    try:
        with TacitClient() as client:
            client.request("POST", "/v1/auth/logout")
        delete_token()
        typer.echo("Credential revoked. Remove TACIT_TOKEN from your environment if set.")
    except (ClientError, CredentialError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("agent-token")
def agent_token(repo: Annotated[list[str], typer.Option("--repo")], expires_in: int = 3600) -> None:
    """Issue a read-only token; stdout contains the secret for intentional secure capture."""
    run("POST", "/v1/auth/agent-tokens", body={"repository_ids": repo, "expires_in": expires_in})


@members.command("list")
def list_members(workspace: Annotated[str, typer.Option("--workspace")]) -> None:
    run("GET", f"/v1/workspaces/{encoded(workspace)}/members")


@members.command("invite")
def invite_member(
    github_id: str,
    workspace: Annotated[str, typer.Option("--workspace")],
    repo: Annotated[list[str], typer.Option("--repo")],
) -> None:
    run(
        "POST",
        f"/v1/workspaces/{encoded(workspace)}/invitations",
        body={"github_id": github_id, "repository_ids": repo},
    )


@members.command("remove")
def remove_member(user: str, workspace: Annotated[str, typer.Option("--workspace")]) -> None:
    run("DELETE", f"/v1/workspaces/{encoded(workspace)}/members/{encoded(user)}")


@roles.command("grant")
def grant_role(
    user: str,
    role: Role,
    workspace: Annotated[str, typer.Option("--workspace")],
    repo: Annotated[str | None, typer.Option("--repo")] = None,
) -> None:
    run(
        "POST",
        f"/v1/workspaces/{encoded(workspace)}/roles",
        body={"user_id": user, "role": role.value, "repository_id": repo},
    )


@roles.command("revoke")
def revoke_role(
    user: str,
    role: Role,
    workspace: Annotated[str, typer.Option("--workspace")],
    repo: Annotated[str | None, typer.Option("--repo")] = None,
) -> None:
    run(
        "POST",
        f"/v1/workspaces/{encoded(workspace)}/roles/revoke",
        body={"user_id": user, "role": role.value, "repository_id": repo},
    )


@app.command()
def teach(
    repo: RepoOption,
    commit: CommitOption,
    source_files: Annotated[list[str], typer.Option("--file")],
    text_file: Annotated[
        Path, typer.Option("--text-file", help="Temporary text input for the future voice workflow")
    ],
    rule_file: Annotated[Path | None, typer.Option("--rule")] = None,
    decision: Annotated[
        str | None, typer.Option("--decision", help="Supersede an existing decision")
    ] = None,
    expected_snapshot: Annotated[
        str | None,
        typer.Option(
            "--expected-snapshot", help="Defaults to current head; use 'none' for an empty head"
        ),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
) -> None:
    """Create a source-bound teaching draft. This command never publishes."""
    operation = operation_id or str(uuid4())
    try:
        statement = text_file.read_text(encoding="utf-8")
        rule = Rule.model_validate_json(rule_file.read_text()) if rule_file else None
        with TacitClient() as client:
            if expected_snapshot is None:
                head = client.request("GET", f"/v1/repos/{encoded(repo)}/head")
                if not isinstance(head, dict):
                    raise ClientError("Invalid knowledge head response")
                base = head.get("snapshot_id")
            else:
                base = None if expected_snapshot == "none" else expected_snapshot
            body = DraftRequest(
                operation_id=operation,
                statement=statement,
                source_commit=commit,
                files=tuple(source_files),
                expected_snapshot_id=base,
                decision_id=decision,
                rule=rule,
            )
            typer.echo(
                f"Draft operation ID: {operation}; expected snapshot: {base or 'none'} (reuse both with the same input on retry)",
                err=True,
            )
            result = client.request(
                "POST",
                f"/v1/repos/{encoded(repo)}/teaching-sessions",
                json=body.model_dump(mode="json"),
            )
        typer.echo(json.dumps(result, indent=2))
    except (ClientError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def review(draft: str, repo: RepoOption) -> None:
    """Show proposed knowledge, source evidence, prior revision and exact review digest."""
    run("GET", f"/v1/repos/{encoded(repo)}/teaching-sessions/{encoded(draft)}")


@app.command()
def confirm(
    draft: str,
    repo: RepoOption,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
) -> None:
    """Review again and explicitly confirm an immutable publication."""
    operation = operation_id or str(uuid4())
    try:
        with TacitClient() as client:
            view = DraftView.model_validate(
                client.request(
                    "GET", f"/v1/repos/{encoded(repo)}/teaching-sessions/{encoded(draft)}"
                )
            )
            typer.echo(json.dumps(view.model_dump(mode="json"), indent=2), err=True)
            typer.echo(
                f"Confirmation operation ID: {operation} (reuse this ID if the outcome is uncertain)",
                err=True,
            )
            with redirect_stdout(sys.stderr):
                confirmed = typer.confirm("Publish exactly this reviewed knowledge?", err=True)
            if not confirmed:
                typer.echo("Cancelled; no knowledge was published.", err=True)
                raise typer.Exit(0)
            body = PublicationRequest(
                draft_id=draft,
                operation_id=operation,
                review_digest=view.review_digest,
                expected_snapshot_id=view.expected_snapshot_id,
            )
            result = client.request(
                "POST", f"/v1/repos/{encoded(repo)}/publications", json=body.model_dump(mode="json")
            )
        typer.echo(json.dumps(result, indent=2))
    except (ClientError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@repos.command("head")
def repo_head(repo: RepoOption, agent: bool = False) -> None:
    run("GET", f"/v1/repos/{encoded(repo)}/head", agent=agent)


@app.command()
def mcp() -> None:
    """Launch the separately installed read-only MCP adapter."""
    import shutil
    import subprocess

    executable = shutil.which("tacit-mcp")
    if executable is None:
        typer.echo("Install tacit-mcp alongside tacit-cli", err=True)
        raise typer.Exit(1)
    raise typer.Exit(subprocess.call([executable]))


def main() -> None:
    app()
