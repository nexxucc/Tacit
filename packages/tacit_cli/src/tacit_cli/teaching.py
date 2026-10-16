"""Grounded teaching commands and optional terminal review UI."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import typer
from pydantic import TypeAdapter
from tacit_client.client import ClientError, TacitClient
from tacit_contracts.models import (
    ClarificationAnswer,
    PrepareDraft,
    Selection,
    SpeakRequest,
    TeachingRequest,
    TeachingView,
)

from tacit_cli.audio import AudioError

app = typer.Typer(help="Extract candidates, clarify, and prepare a human-reviewed draft")


def route(repo: str, run: str | None = None) -> str:
    base = f"/v1/repos/{quote(repo, safe='')}/teaching-runs"
    return base + "/" + quote(run, safe="") if run else base


def request(method: str, path: str, body: object = None) -> object:
    with TacitClient() as client:
        return client.request(
            method, path, **({"json": body} if body is not None else {}), timeout=150
        )


@app.command("extract")
def extract(
    repo: Annotated[str, typer.Option("--repo")],
    commit: Annotated[str, typer.Option("--commit")],
    files: Annotated[list[str], typer.Option("--file")],
    transcript_operation: str | None = None,
    text_file: Path | None = None,
    selections_file: Path | None = None,
    source_root: Path | None = None,
    expected_snapshot: str | None = None,
    operation_id: str | None = None,
) -> None:
    try:
        head = TypeAdapter(dict[str, str | None]).validate_python(
            request("GET", f"/v1/repos/{quote(repo, safe='')}/head")
        )
        base = (
            head["snapshot_id"]
            if expected_snapshot is None
            else (None if expected_snapshot == "none" else expected_snapshot)
        )
        selections = (
            json.loads(selections_file.read_text())
            if selections_file
            else [{"path": path, "captured_at": datetime.now(UTC).isoformat()} for path in files]
        )
        if source_root and not selections_file:
            from tacit_cli.source_context import capture

            selections = [
                selection.model_dump(mode="json")
                for selection in capture(source_root, commit, files)
            ]
        body = TeachingRequest(
            operation_id=operation_id or str(uuid4()),
            source_commit=commit,
            files=tuple(files),
            expected_snapshot_id=base,
            transcript_operation_id=transcript_operation,
            statement=text_file.read_text() if text_file else None,
            selections=tuple(Selection.model_validate(s) for s in selections),
        )
        typer.echo(
            f"Extraction operation: {body.operation_id}; base snapshot: {base or 'none'}", err=True
        )
        if not typer.confirm(
            "Send this transcript and selected source to Evon? This may consume model resources.",
            err=True,
        ):
            return
        typer.echo(json.dumps(request("POST", route(repo), body.model_dump(mode="json")), indent=2))
    except (ClientError, ValueError, OSError, AudioError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("list")
def list_runs(repo: Annotated[str, typer.Option("--repo")]) -> None:
    try:
        typer.echo(json.dumps(request("GET", route(repo)), indent=2))
    except ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("review")
def review(run: str, repo: Annotated[str, typer.Option("--repo")], ui: bool = False) -> None:
    try:
        view = request("GET", route(repo, run))
        if ui:
            from tacit_cli.tui import review_teaching

            review_teaching(repo, view)
        else:
            typer.echo(json.dumps(view, indent=2))
    except (ClientError, ValueError, ImportError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("answer")
def answer(
    run: str,
    repo: Annotated[str, typer.Option("--repo")],
    answer_file: Annotated[Path, typer.Option("--answer-file")],
) -> None:
    try:
        body = ClarificationAnswer.model_validate_json(answer_file.read_text())
        typer.echo(
            json.dumps(
                request("POST", route(repo, run) + "/answers", body.model_dump(mode="json")),
                indent=2,
            )
        )
    except (ClientError, ValueError, OSError, AudioError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("draft")
def draft(
    run: str,
    repo: Annotated[str, typer.Option("--repo")],
    version: int,
    operation_id: str | None = None,
    decision: str | None = None,
) -> None:
    try:
        body = PrepareDraft(
            operation_id=operation_id or str(uuid4()),
            expected_version=version,
            decision_id=decision,
        )
        typer.echo(f"Draft operation: {body.operation_id}", err=True)
        typer.echo(
            json.dumps(
                request("POST", route(repo, run) + "/draft", body.model_dump(mode="json")), indent=2
            )
        )
    except (ClientError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("cancel")
def cancel(run: str, repo: Annotated[str, typer.Option("--repo")]) -> None:
    try:
        typer.echo(json.dumps(request("DELETE", route(repo, run)), indent=2))
    except ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("speak")
def speak(
    run: str,
    repo: Annotated[str, typer.Option("--repo")],
    question_id: str,
    voice: str = "Kaveri",
    language: str = "en-IN",
    output: Path | None = None,
    operation_id: str | None = None,
) -> None:
    try:
        view = TeachingView.model_validate(request("GET", route(repo, run)))
        if question_id not in view.questions:
            raise ClientError("Select a listed clarification question")
        body = SpeakRequest(
            operation_id=operation_id or str(uuid4()),
            text=view.questions[question_id],
            voice=voice,
            language=language,
        )
        typer.echo(f"Timbre operation: {body.operation_id}", err=True)
        if not typer.confirm(
            "Synthesize this question with Timbre? It may consume credits.", err=True
        ):
            return
        result = request(
            "POST",
            f"/v1/repos/{quote(repo, safe='')}/clarification-audio",
            body.model_dump(mode="json"),
        )
        if not isinstance(result, dict) or not isinstance(result.get("artifact_id"), str):
            raise ClientError("Invalid audio artifact response")
        with TacitClient() as client:
            audio = client.audio(
                f"/v1/repos/{quote(repo, safe='')}/artifacts/{result['artifact_id']}"
            )
        if output:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(audio)
        else:
            from tacit_cli.audio import play

            play(audio)
        typer.echo(json.dumps(result, indent=2))
    except (ClientError, ValueError, OSError, AudioError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
