"""Explicit human approvals and repository quorum administration."""

import json
from typing import Annotated
from urllib.parse import quote

import typer
from tacit_client.client import ClientError, TacitClient
from tacit_contracts.models import DraftApprovalRequest, DraftView, PublicationPolicyRequest

app = typer.Typer(help="Review exact drafts and record independent human approvals.")
Repo = Annotated[str, typer.Option("--repo")]


def path(repo: str, draft: str | None = None) -> str:
    base = f"/v1/repos/{quote(repo, safe='')}"
    return base + (
        f"/teaching-sessions/{quote(draft, safe='')}/approvals" if draft else "/publication-policy"
    )


def failure(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(1) from None


@app.command("policy")
def policy(repo: Repo) -> None:
    try:
        with TacitClient() as client:
            typer.echo(json.dumps(client.request("GET", path(repo)), indent=2))
    except (ClientError, ValueError) as exc:
        failure(exc)


@app.command("set-policy")
def set_policy(
    repo: Repo,
    required: Annotated[int, typer.Option("--required")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    try:
        body = PublicationPolicyRequest(
            expected_version=expected_version, required_maintainers=required
        )
        typer.echo(json.dumps(body.model_dump(mode="json")), err=True)
        if not typer.confirm(
            "Change publication policy and invalidate previous approvals?", err=True
        ):
            return
        with TacitClient() as client:
            typer.echo(
                json.dumps(
                    client.request("PUT", path(repo), json=body.model_dump(mode="json")), indent=2
                )
            )
    except (ClientError, ValueError) as exc:
        failure(exc)


@app.command("list")
def list_approvals(draft: str, repo: Repo) -> None:
    try:
        with TacitClient() as client:
            typer.echo(json.dumps(client.request("GET", path(repo, draft)), indent=2))
    except (ClientError, ValueError) as exc:
        failure(exc)


@app.command("approve")
def approve(draft: str, repo: Repo) -> None:
    try:
        with TacitClient() as client:
            view = DraftView.model_validate(
                client.request(
                    "GET",
                    f"/v1/repos/{quote(repo, safe='')}/teaching-sessions/{quote(draft, safe='')}",
                )
            )
            status = client.request("GET", path(repo, draft))
            if not isinstance(status, dict) or not isinstance(status.get("policy_version"), int):
                raise ValueError("Invalid approval status")
            body = DraftApprovalRequest(
                policy_version=status["policy_version"],
                review_digest=view.review_digest,
                expected_snapshot_id=view.expected_snapshot_id,
            )
            typer.echo(json.dumps(view.model_dump(mode="json"), indent=2), err=True)
            typer.echo(json.dumps(status, indent=2), err=True)
            if not typer.confirm(
                "Approve exactly this draft? This records approval; it does not publish.", err=True
            ):
                return
            typer.echo(
                json.dumps(
                    client.request("POST", path(repo, draft), json=body.model_dump(mode="json")),
                    indent=2,
                )
            )
    except (ClientError, ValueError) as exc:
        failure(exc)


@app.command("withdraw")
def withdraw(draft: str, repo: Repo) -> None:
    try:
        if not typer.confirm(
            "Withdraw your approval? Published snapshots cannot be changed.", err=True
        ):
            return
        with TacitClient() as client:
            typer.echo(json.dumps(client.request("DELETE", path(repo, draft)), indent=2))
    except (ClientError, ValueError) as exc:
        failure(exc)
