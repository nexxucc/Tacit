"""Durable capture/upload workflow; explicit transcription and separate confirmation."""

import json
import threading
import time
import wave
from pathlib import Path
from urllib.parse import quote

import typer
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import api_url
from tacit_contracts.models import VoiceSessionRequest

from tacit_cli.audio import AudioError, RecordingCancelled, capture_chunks
from tacit_cli.spool import Spool


def path(spool: Spool) -> str:
    return f"/v1/repos/{quote(spool.meta['repo'], safe='')}/voice-sessions"


def checked_client(spool: Spool, client: TacitClient) -> None:
    identity = client.request("GET", "/v1/auth/me")
    if (
        not isinstance(identity, dict)
        or identity.get("user_id") != spool.meta["author"]
        or identity.get("agent") is not False
        or api_url().rstrip("/") != spool.meta["server"]
    ):
        raise ClientError("Recording belongs to a different account or backend")
    if spool.meta["expires_at"] <= time.time():
        spool.forget()
        raise AudioError("Local recording retention expired; audio removed")


def upload(
    spool: Spool, client: TacitClient, *, recording: threading.Event | None = None
) -> dict[str, object]:
    checked_client(spool, client)
    start = client.request("POST", path(spool), json=spool.meta["source"])
    if not isinstance(start, dict):
        raise ClientError("Invalid voice session response")
    if spool.meta["backend_id"] is None:
        spool.meta["backend_id"] = start["id"]
        spool.save()
    elif spool.meta["backend_id"] != start["id"]:
        raise ClientError("Recording session identity changed")
    if start.get("status") in ("completed", "needs_correction"):
        return start
    if start.get("status") in ("ready", "processing", "uncertain"):
        if (
            start.get("audio_sha256") != spool.digest()
            or start.get("pcm_bytes") != spool.meta["bytes"]
            or start.get("chunks") != spool.meta["chunks"]
        ):
            raise AudioError("Retained audio does not match the finalized server recording")
        return start
    sid = quote(str(start["id"]), safe="")
    route = path(spool) + "/" + sid
    # While recording, reload atomically written manifests without sharing mutable state.
    with client.voice_socket(route + "/upload") as socket:
        raw = json.loads(socket.recv(timeout=15))
        if not isinstance(raw, dict):
            raise ClientError("Invalid audio acknowledgement")
        status: dict[str, object] = raw
        sequence = int(str(status["chunks"]))
        if sequence > spool.meta["chunks"]:
            raise AudioError("Server has more chunks than the retained recording")
        while True:
            local = Spool(spool.id)
            if sequence < local.meta["chunks"]:
                socket.send(sequence.to_bytes(8, "big") + local.chunk(sequence))
                raw = json.loads(socket.recv(timeout=15))
                if not isinstance(raw, dict):
                    raise ClientError("Invalid audio acknowledgement")
                status = raw
                if status.get("chunks") != sequence + 1:
                    raise ClientError("Audio acknowledgement failed; resume retained recording")
                sequence += 1
            elif recording is not None and recording.is_set():
                time.sleep(0.1)
            else:
                break
    local = Spool(spool.id)
    if local.meta["status"] != "recorded":
        return status  # captured partial audio stays unpublished
    result = client.request(
        "POST",
        route + "/finish",
        json={
            "chunks": local.meta["chunks"],
            "audio_sha256": local.digest(),
            "capture_complete": local.meta.get("capture_complete", False),
        },
    )
    if not isinstance(result, dict):
        raise ClientError("Invalid finalized audio response")
    return result


def resume(spool: Spool, *, accept_partial: bool = False) -> dict[str, object]:
    if spool.meta["status"] != "recorded":
        if not accept_partial or not spool.meta["chunks"]:
            raise AudioError(
                "Recording was interrupted. Inspect it and use --accept-partial only if that captured portion is intentional"
            )
        spool.meta["status"] = "recorded"
        spool.save()
    with TacitClient() as client:
        uploaded = upload(spool, client)
        if uploaded.get("status") in ("completed", "needs_correction"):
            spool.forget()
            existing = uploaded.get("draft")
            return existing if isinstance(existing, dict) else uploaded
        sid = quote(str(uploaded["id"]), safe="")
        seconds = int(spool.meta["bytes"]) / 32000
        if seconds > 30:
            typer.echo(
                f"Microphone capture is finished. Sending {seconds:.1f}s of saved audio to Prisma "
                "at real-time speed, then waiting for the transcript; no new recording is started.",
                err=True,
            )
        else:
            typer.echo("Capture is finished. Waiting for Prisma transcription.", err=True)
        result = client.request(
            "POST", path(spool) + "/" + sid + "/transcribe", timeout=seconds + 180
        )
    if not isinstance(result, dict):
        raise ClientError("Invalid transcription response")
    if result.get("status") in ("completed", "needs_correction"):
        spool.forget()
    draft = result.get("draft")
    return draft if isinstance(draft, dict) else result


def record_session(
    repo: str,
    source: VoiceSessionRequest,
    *,
    seconds: float,
    device: int | None,
    audio_file: Path | None,
    stream: bool = True,
) -> dict[str, object] | None:
    with TacitClient() as client:
        capabilities = client.request("GET", f"/v1/repos/{quote(repo, safe='')}/voice-capabilities")
        if not isinstance(capabilities, dict) or not capabilities.get("configured"):
            raise ClientError("Prisma is not configured; use tacit audio test for local recording")
        identity = client.request("GET", "/v1/auth/me")
        if not isinstance(identity, dict) or identity.get("agent") is not False:
            raise ClientError("Human maintainer permission is required")
        # Reserve local capacity before allocating server storage. Keep the operation
        # locally recoverable if the server response is lost.
        spool = Spool.create(
            repo=repo,
            source=source.model_dump(mode="json"),
            server=api_url().rstrip("/"),
            author=str(identity["user_id"]),
        )
        typer.echo(f"Recording ID: {spool.id}; operation: {source.operation_id}", err=True)
        # Verify source/head before recording and before any paid call.
        preliminary = client.request(
            "POST",
            f"/v1/repos/{quote(repo, safe='')}/voice-sessions",
            json=source.model_dump(mode="json"),
        )
        if not isinstance(preliminary, dict):
            raise ClientError("Invalid voice session response")
    spool.meta["backend_id"] = preliminary["id"]
    spool.save()
    typer.echo(
        f"Recording ID: {spool.id}; operation: {source.operation_id}. Recover with: tacit voice resume {spool.id}",
        err=True,
    )
    capturing = threading.Event()
    capturing.set()
    errors: list[Exception] = []

    def sender() -> None:
        try:
            with TacitClient() as uploader:
                upload(Spool(spool.id), uploader, recording=capturing)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=sender, daemon=True) if stream else None
    if thread:
        thread.start()
    try:
        if audio_file:
            with wave.open(str(audio_file), "rb") as wav:
                if (
                    wav.getnchannels(),
                    wav.getsampwidth(),
                    wav.getframerate(),
                    wav.getcomptype(),
                ) != (1, 2, 16000, "NONE") or not 0 < wav.getnframes() <= 895 * 16000:
                    raise AudioError("Use a mono 16 kHz PCM16 WAV of at most 895 seconds")
                frames = wav.getnframes()
                while frames:
                    count = min(16000, frames)
                    data = wav.readframes(count)
                    if len(data) != count * 2:
                        raise AudioError("Truncated input WAV")
                    spool.append(data)
                    frames -= count
        else:
            typer.echo(
                "Recording: Enter finishes; Ctrl+C cancels. Upload interruptions retain local audio.",
                err=True,
            )
            for chunk in capture_chunks(seconds, device):
                spool.append(chunk)
        if not spool.meta["chunks"]:
            raise AudioError("No audio captured")
        spool.meta["status"] = "recorded"
        spool.meta["capture_complete"] = True
        # Sender owns backend_id only; don't overwrite its acknowledged session metadata.
        spool.save()
    except RecordingCancelled:
        capturing.clear()
        if thread:
            thread.join(timeout=20)
        try:
            with TacitClient() as client:
                client.request("DELETE", path(spool) + "/" + quote(str(preliminary["id"]), safe=""))
        finally:
            spool.forget()
        raise
    except (AudioError, OSError, wave.Error, KeyboardInterrupt) as exc:
        spool.meta["status"] = "partial"
        spool.save()
        raise AudioError(
            f"Capture interrupted; recording {spool.id} retained for explicit partial recovery"
        ) from exc
    finally:
        capturing.clear()
        if thread:
            thread.join(timeout=20)
    if thread and thread.is_alive():
        raise ClientError("Upload is still pending; retained recording can be resumed")
    if errors:
        raise ClientError(
            f"Upload interrupted. Run tacit voice resume {spool.id}; no transcription was started"
        )
    return resume(Spool(spool.id))
