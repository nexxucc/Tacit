"""Adversarial live-voice tests: synthetic PCM, loopback sockets and fixture STT only."""

import asyncio
import hashlib
import importlib
import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tacit_api.db import TeachingDraft, VoiceAttempt, VoiceSession
from tacit_api.providers import ProviderUnavailable
from tacit_api.publication import PublicationService
from tacit_api.streaming import StreamResult, transcribe_chunks
from tacit_cli.spool import sessions
from tacit_domain.access import DomainError
from test_access_management import engine as engine
from test_access_management import login
from test_voice import COMMIT
from test_voice import voice_setup as voice_setup
from test_voice_pipeline import Socket, start
from typer.testing import CliRunner

PCM = b"\x01\x00" * 16000


@pytest.fixture
def live_setup(voice_setup):
    # Live capture runs provider, upload and authorization transactions concurrently.
    # The inherited in-memory StaticPool fixture shares exactly one SQLite connection;
    # it cannot model these PostgreSQL transactions safely.
    if voice_setup[2].dialect.name == "sqlite":
        pytest.skip("Concurrent live voice requires the disposable PostgreSQL fixture")
    return voice_setup


def live_provider(
    speech, monkeypatch, *, fail=False, preview=None, text="Only maintainers publish."
):
    calls, received, cancelled = [], [], []

    async def live(chunks, *, language, on_segment):
        calls.append(language)
        try:
            async for data in chunks:
                received.append(data)
                if len(received) == 1:
                    if fail:
                        raise ProviderUnavailable("SECRET fixture failure")
                    await on_segment({"segment_index": 1, "segment_id": "first", "text": text})
                    if preview:
                        preview.set()
            return StreamResult(text, ({"segment_index": 1, "segment_id": "first", "text": text},))
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(speech, "transcribe_live", live, raising=False)
    return calls, received, cancelled


def until(ws, kind):
    observed = []
    for _ in range(8):
        event = ws.receive_json()
        observed.append(event)
        if event["type"] == kind:
            return event, observed
    raise AssertionError(observed)


def finalize(ws, pcm=PCM, chunks=1, **extra):
    ws.send_json(
        {
            "type": "finish",
            "chunks": chunks,
            "audio_sha256": hashlib.sha256(pcm).hexdigest(),
            "capture_complete": True,
            **extra,
        }
    )


def row_state(database):
    with Session(database) as db:
        item = db.scalar(select(VoiceSession))
        return (
            item.status,
            item.audio_sha256,
            db.scalar(select(func.count()).select_from(VoiceAttempt)),
            db.scalar(select(func.count()).select_from(TeachingDraft)),
        )


def test_live_preview_precedes_finish_and_final_is_bound_once(live_setup, monkeypatch):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, received, _ = live_provider(speech, monkeypatch)
    view, body = start(client, owner)
    sid = view["id"]
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{sid}/live", headers=owner
    ) as ws:
        assert ws.receive_json()["session"]["status"] == "processing"
        ws.send_bytes(bytes(8) + PCM)
        _, observed = until(ws, "transcript")
        assert observed[-1]["segment"]["text"] == "Only maintainers publish."
        assert row_state(db)[0] == "processing"  # No finish frame has been sent.
        ws.send_json({"type": "ping"})
        until(ws, "pong")
        finalize(ws)
        result, _ = until(ws, "result")
    final = result["session"]
    assert final["status"] == "completed"
    assert final["audio_sha256"] == hashlib.sha256(PCM).hexdigest()
    assert final["draft"]["voice"]["segments"][0]["segment_index"] == 1
    assert final["draft"]["voice"]["session_id"] == sid
    assert received == [PCM] and calls == ["en-IN"] and speech.calls == 0
    retry = client.post(f"/v1/repos/gh-101/voice-sessions/{sid}/transcribe", headers=owner)
    assert retry.status_code == 200 and retry.json()["draft"]["id"] == final["draft"]["id"]
    assert row_state(db)[2:] == (1, 1)
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["duplicate", "bad_hash", "bad_control", "provider"])
def test_live_failures_retain_audio_and_never_replay(live_setup, monkeypatch, failure):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, received, _ = live_provider(speech, monkeypatch, fail=failure == "provider")
    view, _ = start(client, owner)
    sid = view["id"]
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{sid}/live", headers=owner
    ) as ws:
        ws.receive_json()
        ws.send_bytes(bytes(8) + PCM)
        if failure != "provider":
            until(ws, "transcript")
            if failure == "duplicate":
                ws.send_bytes(bytes(8) + PCM)
            elif failure == "bad_hash":
                finalize(ws, audio_sha256="0" * 64)
            else:
                ws.send_text('{"type":"finish","chunks":true}')
        error, _ = until(ws, "error")
        assert "SECRET" not in json.dumps(error)
    status, digest, attempts, drafts = row_state(db)
    assert (status, digest, attempts, drafts) == (
        "uncertain",
        hashlib.sha256(PCM).hexdigest(),
        1,
        0,
    )
    assert received == [PCM] and len(calls) == 1
    retry = client.post(f"/v1/repos/gh-101/voice-sessions/{sid}/transcribe", headers=owner)
    assert retry.status_code == 409 and len(calls) == 1
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/repos/gh-101/voice-sessions/{sid}/live", headers=owner):
            pass
    assert len(calls) == 1


@pytest.mark.parametrize("stop", ["disconnect", "cancel", "logout"])
def test_live_stop_cancels_provider_and_preserves_revocation(live_setup, monkeypatch, stop):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, received, cancelled = live_provider(speech, monkeypatch)
    view, _ = start(client, owner)
    sid = view["id"]
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{sid}/live", headers=owner
    ) as ws:
        ws.receive_json()
        ws.send_bytes(bytes(8) + PCM)
        until(ws, "transcript")
        if stop == "cancel":
            assert (
                client.delete(f"/v1/repos/gh-101/voice-sessions/{sid}", headers=owner).status_code
                == 200
            )
            ws.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        elif stop == "logout":
            assert client.post("/v1/auth/logout", headers=owner).status_code == 200
            ws.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    assert calls == ["en-IN"] and received == [PCM] and cancelled
    assert row_state(db)[0] == ("cancelled" if stop == "cancel" else "uncertain")


def test_saved_live_transcript_recovers_failed_draft_without_paid_replay(live_setup, monkeypatch):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, _, _ = live_provider(speech, monkeypatch)
    original = PublicationService.create
    broken = True

    def create(self, *args, **kwargs):
        if broken:
            raise DomainError("fixture_draft_failure", "Fixture failure", 503)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PublicationService, "create", create)
    view, _ = start(client, owner)
    sid = view["id"]
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{sid}/live", headers=owner
    ) as ws:
        ws.receive_json()
        ws.send_bytes(bytes(8) + PCM)
        until(ws, "transcript")
        finalize(ws)
        until(ws, "error")
    assert row_state(db)[0] == "uncertain"
    broken = False
    result = client.post(f"/v1/repos/gh-101/voice-sessions/{sid}/transcribe", headers=owner)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    assert calls == ["en-IN"] and speech.calls == 0


@contextmanager
def server_for(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, log_level=None, lifespan="off", loop="asyncio")
    )
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert worker.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        worker.join(5)
        sock.close()
        assert not worker.is_alive()


def test_cli_live_loopback_preview_stdout_and_no_second_transcription(
    live_setup, monkeypatch, tmp_path
):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    preview = threading.Event()
    calls, received, _ = live_provider(speech, monkeypatch, preview=preview)
    pipeline = importlib.import_module("tacit_cli.voice_pipeline")
    cli = importlib.import_module("tacit_cli.main")

    def capture(*args, **kwargs):
        yield PCM
        assert preview.wait(5), "No live preview while capture was still active"
        yield PCM

    monkeypatch.setattr(pipeline, "capture_chunks", capture)
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("TACIT_TOKEN", owner["Authorization"].removeprefix("Bearer "))
    with server_for(client.app) as url:
        monkeypatch.setenv("TACIT_API_URL", url)
        result = CliRunner().invoke(
            cli.app,
            [
                "teach-voice",
                "--repo",
                "gh-101",
                "--commit",
                COMMIT,
                "--file",
                "worker.py",
                "--seconds",
                "5",
                "--live-transcript",
            ],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ready_for_review"
    assert "[Provisional transcript segment 1]" in result.stderr
    assert calls == ["en-IN"] and received == [PCM, PCM] and speech.calls == 0
    assert sessions() == []


@pytest.mark.parametrize(
    "events,complete",
    [
        ([{"type": "transcript", "segment_index": 1, "segment_id": "one", "text": "First"}], True),
        (
            [
                {
                    "type": "transcript",
                    "segment_index": 2,
                    "segment_id": "two",
                    "text": "Missing first",
                }
            ],
            False,
        ),
    ],
)
def test_provider_live_indexes_are_preserved_and_gaps_fail_complete(events, complete):
    ws = Socket(events)
    seen = []

    async def source():
        yield PCM

    async def pause(_):
        await asyncio.sleep(0)

    async def segment(item):
        seen.append(item)

    result = asyncio.run(
        transcribe_chunks(
            "fixture-only",
            source(),
            "en-IN",
            connector=lambda *a, **kw: ws,
            pause=pause,
            idle_timeout=0.001,
            drain_timeout=0.05,
            on_segment=segment,
        )
    )
    assert result.complete is complete
    assert seen == list(result.segments)
    assert ws.closed


@pytest.mark.parametrize(
    "event",
    [
        {"type": "transcript", "segment_index": True, "segment_id": "one", "text": "bad"},
        {"type": "transcript", "segment_index": 1, "segment_id": "one", "text": ""},
        {
            "type": "transcript",
            "segment_index": 1,
            "segment_id": "one",
            "text": "bad",
            "is_final": False,
        },
    ],
)
def test_provider_rejects_malformed_preview_before_callback(event):
    ws = Socket([event])
    seen = []

    async def source():
        yield PCM

    async def pause(_):
        await asyncio.sleep(0)

    async def segment(item):
        seen.append(item)

    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            transcribe_chunks(
                "fixture-only",
                source(),
                "en-IN",
                connector=lambda *a, **kw: ws,
                pause=pause,
                idle_timeout=0.001,
                drain_timeout=0.05,
                on_segment=segment,
            )
        )
    assert seen == [] and ws.closed


def test_live_denies_other_author_agent_and_wrong_repository_before_attempt(
    live_setup, monkeypatch
):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, _, _ = live_provider(speech, monkeypatch)
    view, _ = start(client, owner)
    other = login(client, gh, db, 2)
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    delegated = {"Authorization": "Bearer " + agent["token"]}
    for headers, rid in [(other, "gh-101"), (delegated, "gh-101"), (owner, "gh-202")]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/v1/repos/{rid}/voice-sessions/{view['id']}/live", headers=headers
            ):
                pass
    assert row_state(db)[0:4] == ("uploading", None, 0, 0)
    assert calls == [] and speech.calls == 0


def test_live_second_connection_cannot_reserve_or_replay_active_attempt(live_setup, monkeypatch):
    client, gh, db, speech = live_setup
    owner = login(client, gh, db)
    calls, _, _ = live_provider(speech, monkeypatch)
    view, _ = start(client, owner)
    route = f"/v1/repos/gh-101/voice-sessions/{view['id']}/live"
    with client.websocket_connect(route, headers=owner) as first:
        first.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(route, headers=owner):
                pass
        first.send_bytes(bytes(8) + PCM)
        until(first, "transcript")
        finalize(first)
        result, _ = until(first, "result")
    assert result["session"]["status"] == "completed"
    assert calls == ["en-IN"] and row_state(db)[2:] == (1, 1)
