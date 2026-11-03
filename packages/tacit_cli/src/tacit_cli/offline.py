"""Opt-in local text teaching queue. Sync creates private drafts, never publications."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import typer
from pydantic import Field, model_validator
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import CredentialError, api_url
from tacit_contracts.models import DraftRequest, DraftView, Rule, Selection
from tacit_contracts.snapshots import PortableVersions, SnapshotLock, digest

from tacit_cli.audio import AudioError
from tacit_cli.editor import file_hash
from tacit_cli.local_storage import private_directory, reject_redirected_path, voice_path
from tacit_cli.snapshots import read_json, read_lock
from tacit_cli.source_context import capture
from tacit_cli.spool import atomic, capacity_lock

app = typer.Typer(help="Explicit offline text teaching queue; sync never publishes.")
MAX_ENTRY = 131072


class Entry(PortableVersions):
    format: Literal["tacit-offline-teaching-v1"] = "tacit-offline-teaching-v1"
    id: UUID
    server: str
    author_id: str = Field(min_length=1, max_length=128)
    repository_id: str = Field(min_length=1, max_length=128)
    created_at: str
    body: DraftRequest
    local_hashes: dict[str, str]
    selections: tuple[Selection, ...] = ()
    attempted_digest: str | None = None
    state: Literal["queued", "attempted", "synced"] = "queued"
    draft: DraftView | None = None

    @model_validator(mode="after")
    def valid_state(self) -> "Entry":
        if self.state in {"attempted", "synced"} and self.attempted_digest != digest(
            self.body.model_dump(mode="json")
        ):
            raise ValueError("Attempted request changed")
        if (self.state == "synced") != (self.draft is not None):
            raise ValueError("Queue result state is inconsistent")
        return self


def root() -> Path:
    folder = Path(os.environ.get("TACIT_OFFLINE_QUEUE", voice_path().parent / "offline-teaching"))
    private_directory(folder)
    return folder


def path(entry_id: str) -> Path:
    return root() / (str(UUID(entry_id)) + ".json")


def read(entry_id: str) -> Entry:
    target = path(entry_id)
    reject_redirected_path(target)
    entry = Entry.model_validate(read_json(target, MAX_ENTRY))
    if str(entry.id) != str(UUID(entry_id)):
        raise ValueError("Queue identity mismatch")
    SnapshotLock.safe_origin(entry.server)
    if entry.body.transcript_draft_id or entry.body.transcript_operation_id:
        raise ValueError("Offline queue cannot impersonate a voice correction")
    return entry


def write(entry: Entry) -> None:
    data = entry.model_dump_json().encode()
    if len(data) > MAX_ENTRY:
        raise ValueError("Offline queue entry exceeds size limit")
    atomic(path(str(entry.id)), data)


def fail(exc: Exception) -> None:
    # Pydantic errors may embed private statements; do not echo their input data.
    message = (
        str(exc)
        if isinstance(exc, (ClientError, CredentialError))
        else "Offline operation refused: invalid file, changed source, mismatched scope, unavailable private storage or queue limit. Existing intent is retained."
    )
    typer.echo(message, err=True)
    raise typer.Exit(1) from None


def ensure_capacity() -> None:
    if len(list(root().glob("*.json"))) >= 20:
        raise ValueError("Twenty offline entries are retained; inspect and forget an entry first")


@app.command("prepare")
def prepare(
    repo: Annotated[str, typer.Option("--repo")],
    author: Annotated[str, typer.Option("--author")],
    commit: Annotated[str, typer.Option("--commit")],
    files: Annotated[list[str], typer.Option("--file")],
    text_file: Annotated[Path, typer.Option("--text-file")],
    source_root: Annotated[Path, typer.Option("--source-root")],
    expected_snapshot: str | None = None,
    lock: Path | None = None,
    decision: str | None = None,
    rule_file: Path | None = None,
) -> None:
    """Save explicit intent without accessing credentials, the API or providers."""
    try:
        server = SnapshotLock.safe_origin(api_url())
        if (lock is None) == (expected_snapshot is None):
            raise ValueError("Supply a lock or an explicit base ('none' for an empty head)")
        base: str | None
        if lock:
            pinned = read_lock(lock)
            if pinned.repository_id != repo or pinned.api_url != server:
                raise ValueError("Lock scope differs from chosen repository/server")
            base = pinned.snapshot_id
        else:
            base = None if expected_snapshot == "none" else expected_snapshot
        with text_file.open("rb") as stream:
            statement = stream.read(32001)
        if len(statement) > 32000:
            raise ValueError("Intent file too large")
        rule = Rule.model_validate(read_json(rule_file, 65536)) if rule_file else None
        # Capture validates local Git existence and bounds. Dirty bytes are not uploaded.
        selections = capture(source_root, commit, files)
        hashes = {s.path: file_hash(source_root, s.path) for s in selections}
        body = DraftRequest(
            operation_id=str(uuid4()),
            statement=statement.decode("utf-8"),
            source_commit=commit,
            files=tuple(files),
            expected_snapshot_id=base,
            decision_id=decision,
            rule=rule,
        )
        entry = Entry(
            id=uuid4(),
            server=server,
            author_id=author,
            repository_id=repo,
            created_at=datetime.now(UTC).isoformat(),
            body=body,
            local_hashes=hashes,
            selections=selections,
        )
        with capacity_lock(root() / ".queue.lock"):
            ensure_capacity()
            if path(str(entry.id)).exists():
                raise ValueError("Queue identity already exists")
            write(entry)
        typer.echo(
            json.dumps(
                {"id": str(entry.id), "state": entry.state, "operation_id": body.operation_id}
            )
        )
    except (ValueError, OSError, ClientError, CredentialError, AudioError) as exc:
        fail(exc)


@app.command("list")
def list_entries() -> None:
    try:
        with capacity_lock(root() / ".queue.lock"):
            result = []
            for target in sorted(root().glob("*.json")):
                entry = read(target.stem)
                result.append(
                    {
                        "id": str(entry.id),
                        "repo": entry.repository_id,
                        "author": entry.author_id,
                        "server": entry.server,
                        "state": entry.state,
                        "created_at": entry.created_at,
                        "draft_id": entry.draft.id if entry.draft else None,
                    }
                )
        typer.echo(json.dumps(result, indent=2))
    except (ValueError, OSError, AudioError) as exc:
        fail(exc)


@app.command("show")
def show(entry_id: str) -> None:
    try:
        typer.echo(read(entry_id).model_dump_json(indent=2))
    except (ValueError, OSError, AudioError) as exc:
        fail(exc)


@app.command("sync")
def sync(entry_id: str, source_root: Annotated[Path, typer.Option("--source-root")]) -> None:
    """Explicitly send unchanged intent as the same human, with durable retry identity."""
    try:
        with capacity_lock(root() / ".queue.lock"):
            entry = read(entry_id)
            if entry.server != api_url():
                raise ClientError(
                    "Queue server differs from TACIT_API_URL; select the trusted server explicitly"
                )
            if entry.state == "synced":
                typer.echo(
                    entry.draft.model_dump_json(indent=2) if entry.draft else "Saved result missing"
                )
                return
            if entry.state == "queued":
                capture(source_root, entry.body.source_commit, list(entry.body.files))
                if set(entry.local_hashes) != set(entry.body.files) or any(
                    file_hash(source_root, file) != entry.local_hashes[file]
                    for file in entry.body.files
                ):
                    raise ValueError("Source changed; inspect and prepare a new entry")
            typer.echo(entry.model_dump_json(indent=2), err=True)
            if not typer.confirm(
                "Send this queued intent to this API as a private draft? No publication or model/speech calls.",
                err=True,
            ):
                return
            with TacitClient() as client:
                identity = client.request("GET", "/v1/auth/me")
                if (
                    not isinstance(identity, dict)
                    or identity.get("agent") is not False
                    or identity.get("user_id") != entry.author_id
                ):
                    raise ClientError(
                        "Sign in as the queued human author; agent/account substitution is forbidden"
                    )
                endpoint = f"/v1/repos/{quote(entry.repository_id, safe='')}"
                if entry.state == "queued":
                    head = client.request("GET", endpoint + "/head")
                    if (
                        not isinstance(head, dict)
                        or "snapshot_id" not in head
                        or head["snapshot_id"] != entry.body.expected_snapshot_id
                    ):
                        raise ClientError(
                            "Knowledge head changed; review and explicitly rebase this unattempted entry"
                        )
                # Must reach durable storage before a possibly successful POST.
                entry = entry.model_copy(
                    update={
                        "state": "attempted",
                        "attempted_digest": digest(entry.body.model_dump(mode="json")),
                    }
                )
                write(entry)
                try:
                    view = DraftView.model_validate(
                        client.request(
                            "POST",
                            endpoint + "/teaching-sessions",
                            json=entry.body.model_dump(mode="json"),
                            timeout=120,
                        )
                    )
                except ClientError as exc:
                    if str(exc) == "API error: snapshot_conflict (409)":
                        entry = entry.model_copy(
                            update={"state": "queued", "attempted_digest": None}
                        )
                        write(entry)
                    raise
                if (
                    view.author_id != entry.author_id
                    or view.repository_id != entry.repository_id
                    or view.source_commit != entry.body.source_commit
                    or view.statement != entry.body.statement
                    or view.expected_snapshot_id != entry.body.expected_snapshot_id
                    or tuple(s.path for s in view.sources) != entry.body.files
                    or view.rule != entry.body.rule
                    or (
                        entry.body.decision_id is not None
                        and view.decision_id != entry.body.decision_id
                    )
                ):
                    raise ClientError(
                        "API draft differs from queued intent; retain operation ID for investigation"
                    )
                entry = entry.model_copy(update={"state": "synced", "draft": view})
                write(entry)
                typer.echo(view.model_dump_json(indent=2))
    except (ValueError, OSError, ClientError, CredentialError, AudioError) as exc:
        fail(exc)


@app.command("rebase")
def rebase(
    entry_id: str, expected_snapshot: Annotated[str, typer.Option("--expected-snapshot")]
) -> None:
    """Explicitly change the base of an unattempted entry only; never merge intent automatically."""
    try:
        with capacity_lock(root() / ".queue.lock"):
            entry = read(entry_id)
            if entry.state != "queued":
                raise ClientError(
                    "An attempted/synced entry is immutable; recover its original operation first"
                )
            typer.echo(entry.model_dump_json(indent=2), err=True)
            if not typer.confirm(
                f"Rebase unchanged intent to {expected_snapshot}? Review retained intent against that snapshot first.",
                err=True,
            ):
                return
            body = DraftRequest.model_validate(
                entry.body.model_dump()
                | {
                    "expected_snapshot_id": None
                    if expected_snapshot == "none"
                    else expected_snapshot,
                    "operation_id": str(uuid4()),
                }
            )
            write(entry.model_copy(update={"body": body}))
    except (ValueError, OSError, ClientError, CredentialError, AudioError) as exc:
        fail(exc)


@app.command("forget")
def forget(entry_id: str) -> None:
    try:
        with capacity_lock(root() / ".queue.lock"):
            target = path(entry_id)
            reject_redirected_path(target)
            if not target.is_file():
                raise ValueError("Queue entry is not a regular file")
            if typer.confirm(
                f"Delete local intent {UUID(entry_id)}? This also permits discarding a corrupt record. Preserve its operation ID first if a server request may have occurred; deletion cannot retract a server draft or publication.",
                err=True,
            ):
                target.unlink()
    except (ValueError, OSError, AudioError) as exc:
        fail(exc)
