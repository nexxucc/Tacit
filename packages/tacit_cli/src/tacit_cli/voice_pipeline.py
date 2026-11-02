"""Durable capture/upload workflow; explicit transcription and separate confirmation."""

import json
import threading
import time
import unicodedata
import wave
from pathlib import Path
from urllib.parse import quote

import typer
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import api_url
from tacit_contracts.models import VoiceSessionRequest, VoiceSessionView

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
    # An active pause can exceed the server's socket-idle deadline. Close the
    # drained transport while paused, then reconnect using the durable sequence;
    # this resumes upload only and never invokes or replays transcription.
    status: dict[str, object] = start
    sequence = int(str(start["chunks"]))
    while True:
        local = Spool(spool.id)
        if sequence > local.meta["chunks"]:
            raise AudioError("Server has more chunks than the retained recording")
        if sequence == local.meta["chunks"]:
            if recording is not None and recording.is_set():
                time.sleep(0.1)
                continue
            break
        with client.voice_socket(route + "/upload") as socket:
            raw = json.loads(socket.recv(timeout=15))
            if not isinstance(raw, dict):
                raise ClientError("Invalid audio acknowledgement")
            status = raw
            sequence = int(str(status["chunks"]))
            while True:
                local = Spool(spool.id)
                if sequence > local.meta["chunks"]:
                    raise AudioError("Server has more chunks than the retained recording")
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
                    if local.meta["status"] == "paused":
                        break
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


def _live_result(spool: Spool, result: object) -> dict[str, object]:
    view = VoiceSessionView.model_validate(result)
    if (
        view.id != spool.meta["backend_id"]
        or view.operation_id != spool.meta["source"]["operation_id"]
        or view.repository_id != spool.meta["repo"]
        or view.status not in {"completed", "needs_correction"}
        or view.chunks != spool.meta["chunks"]
        or view.pcm_bytes != spool.meta["bytes"]
        or view.capture_complete != spool.meta["capture_complete"]
        or view.audio_sha256 != spool.digest()
    ):
        raise ClientError("Live transcript result does not match the retained recording")
    spool.forget()
    return view.draft.model_dump(mode="json") if view.draft else view.model_dump(mode="json")


def _record_live_session(spool: Spool, *, seconds: float, device: int | None) -> dict[str, object]:
    """One billable socket, with provisional output separate from authoritative JSON."""
    capturing, stopped, connected = threading.Event(), threading.Event(), threading.Event()
    capturing.set()
    errors: list[Exception] = []
    results: list[object] = []
    spool.meta["live_attempt"] = True
    spool.save()

    def sender() -> None:
        try:
            with TacitClient() as client:
                route = path(spool) + "/" + quote(str(spool.meta["backend_id"]), safe="")
                with client.voice_socket(route + "/live", max_size=1_000_000) as socket:
                    first = json.loads(socket.recv(timeout=20))
                    if not isinstance(first, dict) or first.get("type") != "session":
                        raise ClientError("Live transcript handshake failed")
                    initial = VoiceSessionView.model_validate(first.get("session"))
                    if (
                        initial.id != spool.meta["backend_id"]
                        or initial.operation_id != spool.meta["source"]["operation_id"]
                        or initial.repository_id != spool.meta["repo"]
                        or initial.chunks != 0
                        or initial.pcm_bytes != 0
                        or initial.status != "processing"
                    ):
                        raise ClientError("Live transcript session identity changed")
                    connected.set()
                    sequence = 0
                    awaiting = False
                    finishing = False
                    finish_deadline = 0.0
                    last_sent = time.monotonic()
                    last_received = last_sent
                    session_deadline = last_sent + 1015
                    segments: dict[int, tuple[str, str]] = {}
                    ids: dict[str, int] = {}
                    text_size = 0
                    while not stopped.is_set():
                        now = time.monotonic()
                        if now > session_deadline or (not finishing and now - last_received > 30):
                            raise ClientError("Live transcript connection deadline exceeded")
                        if finishing and now > finish_deadline:
                            raise ClientError("Live transcription finalization deadline exceeded")
                        local = Spool(spool.id)
                        if not awaiting and not finishing:
                            if sequence < local.meta["chunks"]:
                                socket.send(sequence.to_bytes(8, "big") + local.chunk(sequence))
                                awaiting = True
                                last_sent = now
                            elif not capturing.is_set():
                                if local.meta["status"] != "recorded":
                                    return
                                socket.send(
                                    json.dumps(
                                        {
                                            "type": "finish",
                                            "chunks": local.meta["chunks"],
                                            "audio_sha256": local.digest(),
                                            "capture_complete": local.meta["capture_complete"],
                                        }
                                    )
                                )
                                finishing = True
                                finish_deadline = now + 150
                                last_sent = now
                        if now - last_sent >= 5:
                            socket.send(json.dumps({"type": "ping"}))
                            last_sent = now
                        try:
                            event = json.loads(socket.recv(timeout=0.1))
                        except TimeoutError:
                            continue
                        last_received = time.monotonic()
                        if not isinstance(event, dict):
                            raise ClientError("Invalid live transcript event")
                        kind = event.get("type")
                        if kind == "ack":
                            if (
                                not awaiting
                                or type(event.get("chunks")) is not int
                                or event["chunks"] != sequence + 1
                            ):
                                raise ClientError("Invalid live audio acknowledgement")
                            sequence += 1
                            awaiting = False
                        elif kind == "transcript":
                            item = event.get("segment")
                            if not isinstance(item, dict):
                                raise ClientError("Invalid provisional transcript segment")
                            index, sid, text = (
                                item.get("segment_index"),
                                item.get("segment_id"),
                                item.get("text"),
                            )
                            if (
                                type(index) is not int
                                or not 0 <= index < 10000
                                or not isinstance(sid, str)
                                or not 0 < len(sid) <= 256
                                or not isinstance(text, str)
                                or not text.strip()
                                or len(text) > 120000
                            ):
                                raise ClientError("Invalid provisional transcript segment")
                            if (index in segments and segments[index] != (sid, text)) or (
                                sid in ids and ids[sid] != index
                            ):
                                raise ClientError("Conflicting provisional transcript segment")
                            if index not in segments:
                                text_size += len(text)
                                if text_size > 120000:
                                    raise ClientError(
                                        "Provisional transcript exceeds the session limit"
                                    )
                                segments[index] = (sid, text)
                                ids[sid] = index
                                safe = "".join(
                                    character
                                    if not unicodedata.category(character).startswith("C")
                                    and character not in {"\u2028", "\u2029"}
                                    else " "
                                    for character in text
                                )
                                typer.echo(
                                    f"[Provisional transcript segment {index}] {safe}", err=True
                                )
                        elif kind == "result":
                            if not finishing or awaiting:
                                raise ClientError(
                                    "Live result arrived before recording finalization"
                                )
                            results.append(event.get("session"))
                            return
                        elif kind == "error":
                            raise ClientError(
                                "Live transcription stopped; the provider request will not be replayed"
                            )
                        elif kind != "pong":
                            raise ClientError("Unexpected live transcript event")
        except Exception as exc:
            errors.append(exc)
            typer.echo(
                "Live transcription disconnected. Finish or cancel recording; retained audio will not be replayed automatically.",
                err=True,
            )
        finally:
            connected.set()

    worker = threading.Thread(target=sender, daemon=True)
    worker.start()

    def state(value: str) -> None:
        spool.meta["status"] = "paused" if value == "paused" else "recording"
        spool.save()
        typer.echo(
            "Microphone paused; r + Enter resumes, Enter finishes, Ctrl+C cancels. "
            "The live provider connection stays open and may time out."
            if value == "paused"
            else "Microphone resumed; p + Enter pauses, Enter finishes, Ctrl+C cancels.",
            err=True,
        )

    try:
        if not connected.wait(timeout=25) or errors:
            raise ClientError("Live transcript connection could not start")
        typer.echo(
            "Live transcription is active. Preview segments are provisional; review the final draft. "
            "p + Enter pauses, r + Enter resumes, Enter finishes; Ctrl+C cancels.",
            err=True,
        )
        for chunk in capture_chunks(seconds, device, on_state=state):
            spool.append(chunk)
            if errors or not worker.is_alive():
                raise ClientError("Live transcription was interrupted")
        if not spool.meta["chunks"]:
            raise AudioError("No audio captured")
        spool.meta["status"] = "recorded"
        spool.meta["capture_complete"] = True
        spool.save()
        capturing.clear()
        typer.echo("Capture finished; waiting for the final live transcript.", err=True)
        deadline = time.monotonic() + 155
        update = time.monotonic() + 15
        while worker.is_alive() and time.monotonic() < deadline:
            worker.join(timeout=0.5)
            if time.monotonic() >= update:
                typer.echo(
                    "Still waiting for Prisma to finalize this live transcript; no audio replay is started.",
                    err=True,
                )
                update = time.monotonic() + 15
        if worker.is_alive() or errors or not results:
            raise ClientError("Live transcription did not return a confirmed final result")
        return _live_result(spool, results[0])
    except RecordingCancelled:
        stopped.set()
        capturing.clear()
        worker.join(timeout=15)
        try:
            with TacitClient() as client:
                client.request(
                    "DELETE", path(spool) + "/" + quote(str(spool.meta["backend_id"]), safe="")
                )
        finally:
            spool.forget()
        raise RecordingCancelled(
            "Recording cancelled; retained audio removed. "
            "Prisma may already have processed audio and consumed credits."
        ) from None
    except (ClientError, AudioError, OSError, ValueError, KeyboardInterrupt) as exc:
        if spool.meta["status"] != "recorded":
            spool.meta["status"] = "partial"
            spool.save()
        detail = f" {exc}" if isinstance(exc, AudioError) else ""
        raise ClientError(
            f"Live transcription interrupted; recording {spool.id} retained. "
            "No provider reconnection or replay was attempted. Use voice resume to check for a saved result, "
            "or voice export to inspect the audio." + detail
        ) from exc
    finally:
        stopped.set()
        capturing.clear()
        worker.join(timeout=15)


def resume(spool: Spool, *, accept_partial: bool = False) -> dict[str, object]:
    if spool.meta.get("live_attempt"):
        with TacitClient() as client:
            checked_client(spool, client)
            route = path(spool) + "/" + quote(str(spool.meta["backend_id"]), safe="")
            result = client.request("GET", route)
            if isinstance(result, dict) and result.get("status") in {
                "completed",
                "needs_correction",
            }:
                return _live_result(spool, result)
            # The backend permits this only for an already saved transcript; its
            # durable live reservation prevents replaying audio to the provider.
            result = client.request("POST", route + "/transcribe", timeout=180)
            return _live_result(spool, result)
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
    live_transcript: bool = False,
    preflight: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if live_transcript and (audio_file is not None or not stream):
        raise ClientError("Live transcription requires microphone capture with durable streaming")
    with TacitClient() as client:
        capabilities = preflight or client.request(
            "GET", f"/v1/repos/{quote(repo, safe='')}/voice-capabilities", timeout=60
        )
        if not isinstance(capabilities, dict) or not capabilities.get("configured"):
            raise ClientError("Prisma is not configured; use tacit audio test for local recording")
        if live_transcript and capabilities.get("live_transcript") is not True:
            raise ClientError("This backend does not support live transcript capture")
        identity = (
            {"user_id": capabilities["user_id"], "agent": False}
            if isinstance(capabilities.get("user_id"), str)
            else client.request("GET", "/v1/auth/me", timeout=60)
        )
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
        typer.echo(
            "Preparing source-bound recording session; microphone is not recording yet.", err=True
        )
        preliminary = client.request(
            "POST",
            f"/v1/repos/{quote(repo, safe='')}/voice-sessions",
            json=source.model_dump(mode="json"),
            timeout=60,
        )
        if not isinstance(preliminary, dict):
            raise ClientError("Invalid voice session response")
    spool.meta["backend_id"] = preliminary["id"]
    spool.save()
    if live_transcript:
        return _record_live_session(spool, seconds=seconds, device=device)
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

    def capture_state(value: str) -> None:
        # This state describes the currently running microphone only. A saved-session
        # resume never reopens capture or silently appends a new recording.
        spool.meta["status"] = "paused" if value == "paused" else "recording"
        spool.save()
        typer.echo(
            "Microphone paused; r + Enter resumes, Enter finishes, Ctrl+C cancels."
            if value == "paused"
            else "Microphone resumed; p + Enter pauses, Enter finishes, Ctrl+C cancels.",
            err=True,
        )

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
                "Recording: p + Enter pauses, r + Enter resumes, Enter finishes; Ctrl+C cancels. "
                "The duration limit counts active recording only. Upload interruptions retain local audio.",
                err=True,
            )
            for chunk in capture_chunks(seconds, device, on_state=capture_state):
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
        detail = f". {exc}" if isinstance(exc, AudioError) else ""
        raise AudioError(
            f"Capture interrupted; recording {spool.id} retained for explicit partial recovery{detail}"
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
