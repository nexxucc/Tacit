import json
import platform
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
from tacit_client.client import ClientError, TacitClient
from tacit_contracts.models import ContextRequest, QuestionRequest, ScenarioRequest

RepoOption = Annotated[str, typer.Option("--repo")]
SnapshotOption = Annotated[str, typer.Option("--snapshot")]
CommitOption = Annotated[str, typer.Option("--commit")]

app = typer.Typer(help="Tacit: source-bound project intent. Working command; not a public release.")
workspaces = typer.Typer()
repos = typer.Typer()
app.add_typer(workspaces, name="workspaces")
app.add_typer(repos, name="repos")


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
def history(decision: str, repo: RepoOption, snapshot: SnapshotOption, agent: bool = False) -> None:
    run(
        "GET",
        f"/v1/repos/{encoded(repo)}/decisions/{encoded(decision)}/history?snapshot_id={encoded(snapshot)}",
        agent=agent,
    )


@app.command()
def doctor() -> None:
    """Local diagnostics only; never print tokens or make provider calls."""
    typer.echo(
        json.dumps(
            {
                "python": platform.python_version(),
                "credential_mode": "explicit environment",
                "audio": "not implemented",
                "production_auth": "not implemented",
            },
            indent=2,
        )
    )


@app.command()
def login() -> None:
    typer.echo(
        "GitHub pairing is not implemented. See development.md for synthetic demo credentials.",
        err=True,
    )
    raise typer.Exit(1)


@app.command()
def teach() -> None:
    typer.echo("Native teaching capture and review are not implemented yet.", err=True)
    raise typer.Exit(1)


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
