import array
import base64
import io
import json
import math
import os
import platform
import sys
import time
import wave
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import httpx
import typer
from pydantic import TypeAdapter
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
    VoiceRequest,
    VoiceSessionRequest,
)

from tacit_cli.audio import AudioError, devices, record
from tacit_cli.spool import Spool, sessions
from tacit_cli.teaching import app as teaching_app
from tacit_cli.voice_pipeline import record_session, resume

RepoOption = Annotated[str, typer.Option("--repo")]
SnapshotOption = Annotated[str, typer.Option("--snapshot")]
CommitOption = Annotated[str, typer.Option("--commit")]

app = typer.Typer(help="Tacit: source-bound project intent. Working command; not a public release.")
workspaces = typer.Typer()
repos = typer.Typer()
roles = typer.Typer()
members = typer.Typer()
audio = typer.Typer(help="Arch microphone diagnostics; no provider calls.")
app.add_typer(audio, name="audio")
app.add_typer(teaching_app, name="teaching")
voice = typer.Typer(
    help="Recover or cancel retained voice recordings; never automatically publish."
)
app.add_typer(voice, name="voice")
app.add_typer(workspaces, name="workspaces")
app.add_typer(repos, name="repos")
app.add_typer(roles, name="roles")
app.add_typer(members, name="members")


def run(
    method: str, path: str, *, agent: bool = False, body: object = None, timeout: float = 15
) -> None:
    try:
        with TacitClient(agent=agent) as client:
            result = client.request(
                method, path, **({"json": body} if body is not None else {}), timeout=timeout
            )
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
    reason: bool = False,
    operation_id: str | None = None,
) -> None:
    operation = operation_id or str(uuid4()) if reason else None
    if reason:
        typer.echo(
            f"Question operation: {operation}; text reasoning may consume model resources", err=True
        )
    body = QuestionRequest(
        snapshot_id=snapshot,
        source_commit=commit,
        question=question,
        reason=reason,
        operation_id=operation,
    )
    run(
        "POST",
        f"/v1/repos/{encoded(repo)}/questions",
        agent=agent,
        body=body.model_dump(mode="json"),
        timeout=150 if reason else 15,
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
        supplied = json.loads(scenario.read_text())
        body = ScenarioRequest(
            snapshot_id=snapshot,
            source_commit=commit,
            facts=supplied.get("facts", supplied),
            action=supplied.get("action"),
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
                "audio": "Arch PortAudio; install audio extra and run tacit audio devices",
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
    transcript_operation: Annotated[str | None, typer.Option("--transcript-operation")] = None,
    transcript_draft: Annotated[
        str | None,
        typer.Option(
            "--transcript-draft",
            help="Correct a voice draft while retaining its original transcript",
        ),
    ] = None,
) -> None:
    """Create a source-bound teaching draft. This command never publishes."""
    operation = operation_id or str(uuid4())
    try:
        statement = text_file.read_text(encoding="utf-8")
        rule = Rule.model_validate_json(rule_file.read_text()) if rule_file else None
        with TacitClient() as client:
            if expected_snapshot is None:
                head = TypeAdapter(dict[str, str | None]).validate_python(
                    client.request("GET", f"/v1/repos/{encoded(repo)}/head")
                )
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
                transcript_draft_id=transcript_draft,
                transcript_operation_id=transcript_operation,
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


@audio.command("devices")
def audio_devices() -> None:
    try:
        typer.echo(json.dumps(devices(), indent=2))
    except AudioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@audio.command("test")
def audio_test(
    seconds: Annotated[float, typer.Option(min=1, max=60)] = 5,
    device: int | None = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Save a private WAV for local playback; never uploads"),
    ] = None,
) -> None:
    """Capture locally; optionally save a private WAV. No upload or transcription."""
    try:
        typer.echo("Recording: Enter stops; Ctrl+C cancels. Audio will be discarded.", err=True)
        clip = record(seconds, device)
        with wave.open(io.BytesIO(clip), "rb") as wav:
            samples = array.array("h", wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(float(v) * v for v in samples) / len(samples)) / 32768 if samples else 0
        if output:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(clip)
        typer.echo(
            json.dumps(
                {
                    "wav_bytes": len(clip),
                    "sample_rate": 16000,
                    "channels": 1,
                    "uploaded": False,
                    "rms": round(rms, 6),
                    "clipped_samples": sum(abs(v) >= 32760 for v in samples),
                    "output": str(output) if output else None,
                }
            )
        )
    except (AudioError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("teach-voice")
def teach_voice(
    repo: RepoOption,
    commit: CommitOption,
    source_files: Annotated[list[str], typer.Option("--file")],
    audio_file: Annotated[
        Path | None,
        typer.Option(
            "--audio-file", help="Existing mono 16 kHz PCM16 WAV; otherwise record microphone"
        ),
    ] = None,
    seconds: Annotated[float, typer.Option(min=1, max=895)] = 30,
    stream: Annotated[
        bool,
        typer.Option(
            "--stream/--clip",
            help="Durable sequenced capture/upload; --clip uses the legacy short WAV path",
        ),
    ] = True,
    device: int | None = None,
    language: str = "en-IN",
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    expected_snapshot: Annotated[str | None, typer.Option("--expected-snapshot")] = None,
) -> None:
    """Record/upload recoverable audio and create a private draft; confirmation publishes."""
    operation = operation_id or str(uuid4())
    try:
        with TacitClient() as client:
            # Establish authenticated access before touching the microphone.
            capabilities = client.request("GET", f"/v1/repos/{encoded(repo)}/voice-capabilities")
            if not isinstance(capabilities, dict) or capabilities.get("configured") is not True:
                raise ClientError(
                    "Prisma is not configured on the backend; use tacit audio test for local capture"
                )
            head = TypeAdapter(dict[str, str | None]).validate_python(
                client.request("GET", f"/v1/repos/{encoded(repo)}/head")
            )
            if not isinstance(head, dict):
                raise ClientError("Invalid knowledge head response")
            base = (
                head.get("snapshot_id")
                if expected_snapshot is None
                else (None if expected_snapshot == "none" else expected_snapshot)
            )
            if stream or seconds > 30:
                source = VoiceSessionRequest.model_validate(
                    {
                        "operation_id": operation,
                        "language": language,
                        "source_commit": commit,
                        "files": source_files,
                        "expected_snapshot_id": base,
                    }
                )
                with redirect_stdout(sys.stderr):
                    if not typer.confirm(
                        "Record/upload this session and send it to Prisma? Transcription may consume credits.",
                        err=True,
                    ):
                        typer.echo("Cancelled; no recording or transcription started.", err=True)
                        return
                durable = record_session(
                    repo, source, seconds=seconds, device=device, audio_file=audio_file
                )
                typer.echo(json.dumps(durable, indent=2))
                return
            if audio_file:
                with audio_file.open("rb") as audio_input:
                    clip = audio_input.read(961_025)
                if len(clip) > 961_024:
                    raise AudioError("Audio file exceeds the short-clip size limit")
            else:
                typer.echo("Recording: Enter stops; Ctrl+C cancels before upload.", err=True)
                clip = record(seconds, device)
            body = VoiceRequest.model_validate(
                {
                    "operation_id": operation,
                    "audio_base64": base64.b64encode(clip).decode("ascii"),
                    "language": language,
                    "source_commit": commit,
                    "files": tuple(source_files),
                    "expected_snapshot_id": base,
                }
            )
            typer.echo(
                f"Voice operation ID: {operation}; expected snapshot: {base or 'none'}. Save both for retries with the same WAV input. Transcription can consume Gnani credits.",
                err=True,
            )
            with redirect_stdout(sys.stderr):
                if not typer.confirm(
                    "Send this clip to Prisma to create a reviewable draft?", err=True
                ):
                    typer.echo("Cancelled; no audio uploaded.", err=True)
                    return
            result = client.request(
                "POST",
                f"/v1/repos/{encoded(repo)}/voice-drafts",
                json=body.model_dump(mode="json"),
                timeout=120,
            )
        typer.echo(json.dumps(result, indent=2))
    except (ClientError, AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@voice.command("list")
def voice_list() -> None:
    try:
        typer.echo(json.dumps(sessions(), indent=2))
    except (AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@voice.command("resume")
def voice_resume(recording: str, accept_partial: bool = False) -> None:
    try:
        retained = Spool(recording)
        typer.echo(
            f"Repository: {retained.meta['repo']}; recorded seconds: {retained.meta['bytes'] / 32000:.2f}; state: {retained.meta['status']}",
            err=True,
        )
        with redirect_stdout(sys.stderr):
            if not typer.confirm(
                "Resume upload/transcription? An unattempted transcription may consume credits.",
                err=True,
            ):
                return
        typer.echo(json.dumps(resume(retained, accept_partial=accept_partial), indent=2))
    except (ClientError, AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@voice.command("cancel")
def voice_cancel(recording: str) -> None:
    from tacit_cli.voice_pipeline import checked_client, path

    try:
        retained = Spool(recording)
        with TacitClient() as client:
            checked_client(retained, client)
            if retained.meta["backend_id"]:
                client.request(
                    "DELETE", path(retained) + "/" + encoded(str(retained.meta["backend_id"]))
                )
        retained.forget()
        typer.echo(
            "Retained local/server audio removed. Previously published knowledge is unchanged."
        )
    except (ClientError, AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@voice.command("export")
def voice_export(recording: str, output: Annotated[Path, typer.Option("--output")]) -> None:
    """Export retained captured audio for local review; never contacts a provider."""
    try:
        retained = Spool(recording)
        if retained.meta["expires_at"] <= time.time():
            retained.forget()
            raise AudioError("Local recording retention expired")
        retained.digest()
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            for i in range(retained.meta["chunks"]):
                wav.writeframes(retained.chunk(i))
        typer.echo(f"Captured audio exported to {output}; interrupted recordings may be partial.")
    except (AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@voice.command("forget")
def voice_forget(recording: str) -> None:
    """Delete only local retained audio; backend retention/cancellation is separate."""
    try:
        retained = Spool(recording)
        retained.forget()
        typer.echo("Local recording removed; backend audio follows its retention policy.")
    except (AudioError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def transcript(operation: str, repo: RepoOption) -> None:
    """Retrieve your immutable original transcript, including after a draft conflict."""
    run("GET", f"/v1/repos/{encoded(repo)}/voice-transcripts/{encoded(operation)}")


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


@workspaces.command("use")
def use_workspace(workspace: str) -> None:
    from tacit_cli.config import save

    try:
        with TacitClient() as client:
            eligible = TypeAdapter(list[dict[str, str]]).validate_python(
                client.request("GET", "/v1/workspaces")
            )
        if not any(w["id"] == workspace for w in eligible):
            raise ClientError("Workspace unavailable")
        save(workspace)
        typer.echo("Workspace selection saved for this backend/account.")
    except (ClientError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@repos.command("use")
def use_repository(repo: str, workspace: Annotated[str, typer.Option("--workspace")]) -> None:
    from tacit_cli.config import save

    try:
        with TacitClient() as client:
            eligible = TypeAdapter(list[dict[str, str]]).validate_python(
                client.request("GET", f"/v1/workspaces/{encoded(workspace)}/repos")
            )
        if not any(r["id"] == repo for r in eligible):
            raise ClientError("Repository unavailable")
        save(workspace, repo)
        typer.echo("Repository selection saved for this backend/account.")
    except (ClientError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("selected-context")
def selected_context(commit: CommitOption, snapshot: str | None = None) -> None:
    from tacit_cli.config import repository

    try:
        repo = repository()
        if not repo:
            raise ClientError("Select a repository with tacit repos use first")
        with TacitClient() as client:
            head = TypeAdapter(dict[str, str | None]).validate_python(
                client.request("GET", f"/v1/repos/{encoded(repo)}/head")
            )
        selected = snapshot or head.get("snapshot_id")
        if not selected:
            raise ClientError("Repository has no published snapshot")
        run(
            "POST",
            f"/v1/repos/{encoded(repo)}/context",
            body=ContextRequest(snapshot_id=selected, source_commit=commit).model_dump(mode="json"),
        )
    except (ClientError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("install")
def installation() -> None:
    try:
        with TacitClient() as client:
            result = TypeAdapter(dict[str, str]).validate_python(
                client.request("GET", "/v1/install")
            )
        typer.echo(result["url"])
    except (ClientError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def main() -> None:
    app()
