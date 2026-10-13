"""Real local transports/transactions; generated audio, fixture providers, zero billing."""

import asyncio
import hashlib
import importlib
import json
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect
from tacit_api.access import now
from tacit_api.db import TeachingDraft, VoiceChunk, VoiceSession
from tacit_api.providers import ProviderUnavailable
from tacit_api.streaming import StreamResult, transcribe_pcm
from tacit_api.voice_sessions import VoiceSessions
from tacit_cli.audio import AudioError, capture_chunks
from tacit_cli.spool import Spool, sessions
from test_access_management import engine as engine
from test_access_management import login
from test_publication import publish
from test_voice import COMMIT, Capture, wav
from test_voice import voice_setup as voice_setup
from typer.testing import CliRunner


def start(client, owner, *, op=None, repo="gh-101"):
    body = dict(
        operation_id=op or str(uuid4()),
        language="en-IN",
        source_commit=COMMIT,
        files=["worker.py"],
        expected_snapshot_id=None,
    )
    response = client.post(f"/v1/repos/{repo}/voice-sessions", headers=owner, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def chunk(client, owner, sid, sequence, pcm=b"\0\0" * 16000):
    return client.put(
        f"/v1/repos/gh-101/voice-sessions/{sid}/chunks/{sequence}", headers=owner, content=pcm
    )


def finish(client, owner, sid, count, pcm):
    return client.post(
        f"/v1/repos/gh-101/voice-sessions/{sid}/finish",
        headers=owner,
        json=dict(chunks=count, audio_sha256=hashlib.sha256(pcm).hexdigest()),
    )


def stt(client, owner, sid):
    return client.post(f"/v1/repos/gh-101/voice-sessions/{sid}/transcribe", headers=owner)


def test_upload_reconnect_deduplication_encryption_transcription_and_publish(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    view, body = start(client, owner)
    sid = view["id"]
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body).json()["id"] == sid
    )
    pcm = b"\x11\x00" * 16000
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{sid}/upload", headers=owner
    ) as socket:
        assert socket.receive_json()["chunks"] == 0
        socket.send_bytes((0).to_bytes(8, "big") + pcm)
        assert socket.receive_json()["chunks"] == 1
    # Disconnect after a received ACK; retry the same chunk exactly once.
    assert chunk(client, owner, sid, 0, pcm).json()["chunks"] == 1
    assert chunk(client, owner, sid, 0, b"\0\0" * 16000).status_code == 409
    assert chunk(client, owner, sid, 2).status_code == 409
    assert chunk(client, owner, sid, 1, b"x").status_code == 422
    assert chunk(client, owner, sid, 1, b"x" * 32001).status_code == 413
    with Session(database) as db:
        retained = db.scalar(select(VoiceChunk))
        assert retained.encrypted_pcm != pcm
        assert len(retained.encrypted_pcm) > len(pcm)
    assert finish(client, owner, sid, 2, pcm).status_code == 409
    assert finish(client, owner, sid, 1, pcm).status_code == 200
    stored_audio = client.get(f"/v1/repos/gh-101/voice-sessions/{sid}/audio", headers=owner)
    assert stored_audio.status_code == 200
    assert stored_audio.headers["Cache-Control"] == "no-store"
    assert stored_audio.content.endswith(pcm)
    response = stt(client, owner, sid)
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == "completed"
    assert completed["draft"]["voice"]["original_transcript"] == "Only human maintainers publish."
    assert stt(client, owner, sid).json()["draft"]["id"] == completed["draft"]["id"]
    assert speech.calls == 1
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(VoiceChunk)) == 0
    assert publish(client, owner, completed["draft"]).status_code == 200


def test_author_agent_and_cross_repository_boundaries(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    item, body = start(client, owner)
    sid = item["id"]
    second = login(client, provider, database, 2)
    assert (
        client.post(
            "/v1/workspaces/gh-1/invitations",
            headers=owner,
            json=dict(github_id="2", repository_ids=["gh-101"]),
        ).status_code
        == 200
    )
    assert client.get("/v1/workspaces", headers=second).status_code == 200
    assert (
        client.post(
            "/v1/workspaces/gh-1/roles",
            headers=owner,
            json=dict(user_id="gh-2", role="maintainer", repository_id="gh-101"),
        ).status_code
        == 200
    )
    assert client.get(f"/v1/repos/gh-101/voice-sessions/{sid}", headers=second).status_code == 404
    assert chunk(client, second, sid, 0).status_code == 404
    assert stt(client, second, sid).status_code == 404
    assert (
        client.get(f"/v1/repos/gh-101/voice-sessions/{sid}/audio", headers=second).status_code
        == 404
    )
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json=dict(repository_ids=["gh-101"])
    ).json()
    delegated = {"Authorization": "Bearer " + agent["token"]}
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=delegated, json=body).status_code
        == 403
    )
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/repos/gh-101/voice-sessions/{sid}/upload", headers=delegated
        ):
            pass
    assert client.get(f"/v1/repos/gh-202/voice-sessions/{sid}", headers=owner).status_code == 403
    assert speech.calls == 0


def test_cancel_retention_and_quota(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    items = [start(client, owner)[0] for _ in range(3)]
    assert chunk(client, owner, items[0]["id"], 0).status_code == 200
    request = dict(
        operation_id=str(uuid4()),
        language="en-IN",
        source_commit=COMMIT,
        files=["worker.py"],
        expected_snapshot_id=None,
    )
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=request).json()["code"]
        == "voice_quota"
    )
    assert (
        client.delete(f"/v1/repos/gh-101/voice-sessions/{items[0]['id']}", headers=owner).json()[
            "status"
        ]
        == "cancelled"
    )
    assert stt(client, owner, items[0]["id"]).status_code == 409
    assert chunk(client, owner, items[0]["id"], 0).status_code == 409
    assert chunk(client, owner, items[1]["id"], 0).status_code == 200
    with Session(database) as db, db.begin():
        item = db.scalar(select(VoiceSession).where(VoiceSession.id == items[1]["id"]))
        item.expires_at = now() - timedelta(seconds=1)
    assert chunk(client, owner, items[1]["id"], 0).status_code == 410
    from types import SimpleNamespace

    VoiceSessions(SimpleNamespace(engine=database), speech).prune()
    # Cleanup must delete raw data without deleting immutable transcripts.
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(VoiceChunk)) == 0
    assert speech.calls == 0


def test_failure_never_replays_and_cancel_during_paid_call_never_creates_draft(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    item, _ = start(client, owner)
    sid = item["id"]
    pcm = b"\0\0" * 16000
    assert chunk(client, owner, sid, 0, pcm).status_code == 200
    assert finish(client, owner, sid, 1, pcm).status_code == 200
    speech.fail = True
    assert stt(client, owner, sid).status_code == 503
    speech.fail = False
    assert stt(client, owner, sid).status_code == 409
    assert speech.calls == 1
    another, _ = start(client, owner)
    sid2 = another["id"]
    assert chunk(client, owner, sid2, 0, pcm).status_code == 200
    assert finish(client, owner, sid2, 1, pcm).status_code == 200
    speech.hook = lambda: client.delete(f"/v1/repos/gh-101/voice-sessions/{sid2}", headers=owner)
    assert stt(client, owner, sid2).status_code == 409
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 0


@pytest.mark.parametrize("extra_samples", [0, 1])
def test_live_rest_duration_boundary_routes_without_rebilling(
    voice_setup, monkeypatch, extra_samples
):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    streamed = []

    async def stream(pcm, *, language):
        streamed.append(len(pcm))
        return StreamResult(
            "Human maintainers publish.",
            ({"segment_id": "boundary", "segment_index": 0, "text": "Human maintainers publish."},),
        )

    monkeypatch.setattr(speech, "transcribe_stream", stream, raising=False)
    item, body = start(client, owner)
    sid = item["id"]
    pcm = b"\0\0" * 480000
    pieces = [pcm[i : i + 32000] for i in range(0, len(pcm), 32000)]
    if extra_samples:
        pieces.append(b"\0\0")
    for i, piece in enumerate(pieces):
        assert chunk(client, owner, sid, i, piece).status_code == 200
    assert finish(client, owner, sid, len(pieces), b"".join(pieces)).status_code == 200
    assert stt(client, owner, sid).status_code == 200
    assert stt(client, owner, sid).status_code == 200
    assert speech.calls == (0 if extra_samples else 1)
    assert streamed == ([len(pcm) + 2] if extra_samples else [])


@pytest.mark.parametrize("complete", [True, False])
def test_long_transcript_preserved_with_explicit_correction(voice_setup, monkeypatch, complete):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    text = "Human maintainers may publish. " * (300 if complete else 1)
    calls = []

    async def stream(pcm, *, language):
        calls.append(len(pcm))
        return StreamResult(
            text,
            (
                {
                    "segment_id": "fixture-segment",
                    "segment_index": 0 if complete else 1,
                    "text": text,
                },
            ),
            complete,
        )

    monkeypatch.setattr(speech, "transcribe_stream", stream, raising=False)
    item, body = start(client, owner)
    sid = item["id"]
    pcm = b"\0\0" * 16000
    for i in range(31):
        assert chunk(client, owner, sid, i, pcm).status_code == 200
    assert finish(client, owner, sid, 31, pcm * 31).status_code == 200
    result = stt(client, owner, sid)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_correction"
    assert stt(client, owner, sid).json()["status"] == "needs_correction"
    original = client.get(
        f"/v1/repos/gh-101/voice-transcripts/{body['operation_id']}", headers=owner
    ).json()
    assert original["voice"]["original_transcript"] == text
    assert original["voice"]["transcription_complete"] is complete
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(TeachingDraft)) == 0
    correction = client.post(
        "/v1/repos/gh-101/teaching-sessions",
        headers=owner,
        json=dict(
            operation_id=str(uuid4()),
            statement="Only human maintainers may publish.",
            source_commit=COMMIT,
            files=["worker.py"],
            expected_snapshot_id=None,
            transcript_operation_id=body["operation_id"],
        ),
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["voice"]["original_transcript"] == text
    assert correction.json()["voice"]["corrected"] is True
    assert len(calls) == 1
    assert publish(client, owner, correction.json()).status_code == 200


class Socket:
    def __init__(self, events):
        self.messages = asyncio.Queue()
        self.messages.put_nowait(
            json.dumps(dict(type="connected", config=dict(sample_rate=16000, chunk_size=512)))
        )
        self.events, self.frames, self.closed = events, [], False

    async def recv(self):
        return await self.messages.get()

    async def send(self, frame):
        self.frames.append(frame)
        if len(self.frames) == 2:
            for event in self.events:
                self.messages.put_nowait(json.dumps(event))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


def test_prisma_total_deadline_bounds_stalled_send_without_replay():
    class Stalled(Socket):
        async def send(self, frame):
            self.frames.append(frame)
            await asyncio.Event().wait()

    sock = Stalled([])
    connections = []

    def connector(*args, **kwargs):
        connections.append(kwargs)
        return sock

    with pytest.raises(ProviderUnavailable, match="deadline"):
        asyncio.run(
            transcribe_pcm(
                "fixture-only",
                b"\0\0" * 600,
                "en-IN",
                connector=connector,
                total_timeout=0.03,
                idle_timeout=0.01,
            )
        )
    assert len(connections) == 1 and len(sock.frames) == 1 and sock.closed
    assert connections[0]["close_timeout"] == 5


def test_prisma_exact_framing_headers_pacing_segment_dedup_and_failures(caplog):
    events = [
        dict(type="processing"),
        dict(type="transcript", segment_id="one", segment_index=0, text="Exact first."),
        dict(type="transcript", segment_id="one", segment_index=0, text="Exact first."),
    ]
    sock = Socket(events)
    pauses = []

    def connector(url, **kwargs):
        assert url == "wss://api.vachana.ai/stt/v3/stream"
        assert kwargs["additional_headers"] == {
            "x-api-key-id": "fixture-only",
            "lang_code": "en-IN",
            "x-sample-rate": "16000",
            "x-format": "verbatim",
            "x-enable-substitution": "false",
        }
        return sock

    async def pause(seconds):
        pauses.append(seconds)
        await asyncio.sleep(0)

    result = asyncio.run(
        transcribe_pcm(
            "fixture-only",
            b"\x01\x00" * 600,
            "en-IN",
            connector=connector,
            pause=pause,
            idle_timeout=0.01,
        )
    )
    assert result.text == "Exact first." and len(result.segments) == 1
    assert all(len(frame) == 1024 for frame in sock.frames)
    assert b"".join(sock.frames).startswith(b"\x01\x00" * 600)
    assert all(value == 0.032 for value in pauses) and sock.closed
    for bad in [
        [dict(type="error", message="secret")],
        [dict(type="processing")],
    ]:
        sock = Socket(bad)
        with pytest.raises(ProviderUnavailable) as error:
            asyncio.run(
                transcribe_pcm(
                    "fixture-only",
                    b"\0\0" * 600,
                    "en-IN",
                    connector=lambda *args, sock=sock, **kwargs: sock,
                    pause=pause,
                    idle_timeout=0.01,
                    drain_timeout=0.05,
                )
            )
        assert "secret" not in str(error.value)
        assert sock.closed
    assert "Prisma realtime failed: stage=" in caplog.text
    assert "reason=Provider error" in caplog.text
    assert "secret" not in caplog.text and "fixture-only" not in caplog.text


@pytest.mark.parametrize("indexes", [[1, 2], [0, 2]])
def test_prisma_noncanonical_indexes_preserve_paid_output_for_correction(indexes):
    sock = Socket(
        [
            dict(type="transcript", segment_id=str(i), segment_index=i, text=f"Segment {i}.")
            for i in reversed(indexes)
        ]
    )

    async def pause(seconds):
        await asyncio.sleep(0)

    result = asyncio.run(
        transcribe_pcm(
            "fixture-only",
            b"\0\0" * 600,
            "en-IN",
            connector=lambda *args, **kwargs: sock,
            pause=pause,
            idle_timeout=0.01,
        )
    )
    assert not result.complete
    assert [s["segment_index"] for s in result.segments] == indexes
    assert result.text == "\n".join(f"Segment {i}." for i in indexes)


def test_local_spool_integrity_partial_recovery_permissions_and_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    spool = Spool.create(repo="gh-101", source={}, server="http://localhost:8000", author="gh-1")
    spool.append(b"\0\0" * 16000)
    assert spool.path.stat().st_mode & 0o777 == 0o700
    assert (spool.path / "0000.pcm").stat().st_mode & 0o777 == 0o600
    assert spool.digest() == hashlib.sha256(b"\0\0" * 16000).hexdigest()
    (spool.path / "0000.pcm").write_bytes(b"corrupt")
    with pytest.raises(AudioError, match="integrity"):
        spool.digest()
    spool.meta["expires_at"] = 0
    spool.save()
    assert sessions() == [] and not spool.path.exists()
    with pytest.raises(ValueError):
        Spool("../outside")


def test_durable_cli_fixture_upload_no_publication_and_cancel(voice_setup, monkeypatch, tmp_path):
    from tacit_client.client import ClientError

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))

    class Wire:
        def __init__(self, socket):
            self.socket = socket

        def recv(self, timeout=15):
            return self.socket.receive_text()

        def send(self, packet):
            self.socket.send_bytes(packet)

    class Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            kwargs.pop("timeout", None)
            result = client.request(method, path, headers=owner, **kwargs)
            if result.status_code >= 400:
                raise ClientError(result.json()["code"])
            return result.json()

        @contextmanager
        def voice_socket(self, path):
            with client.websocket_connect(path, headers=owner) as socket:
                yield Wire(socket)

    cli = importlib.import_module("tacit_cli.main")
    pipeline = importlib.import_module("tacit_cli.voice_pipeline")
    monkeypatch.setattr(cli, "TacitClient", Bridge)
    monkeypatch.setattr(pipeline, "TacitClient", Bridge)
    monkeypatch.setattr(pipeline, "api_url", lambda: "http://localhost:8000")
    file = tmp_path / "input.wav"
    file.write_bytes(wav())
    args = [
        "teach-voice",
        "--repo",
        "gh-101",
        "--commit",
        COMMIT,
        "--file",
        "worker.py",
        "--audio-file",
        str(file),
    ]
    runner = CliRunner()
    result = runner.invoke(cli.app, args, input="n\n")
    assert result.exit_code == 0 and speech.calls == 0
    result = runner.invoke(cli.app, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ready_for_review"
    assert speech.calls == 1 and sessions() == []
    assert file.exists()
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


def test_capture_generator_closes_on_device_loss_and_consumer_exit():
    device = Capture()
    assert b"".join(capture_chunks(0.01, sd=device, interactive=False)) == b"\0\0" * 160
    assert device.closed
    device = Capture("overflow")
    with pytest.raises(AudioError, match="incomplete"):
        list(capture_chunks(0.01, sd=device, interactive=False))
    assert device.closed


def test_spool_recovery_after_upload_failure_is_exact_and_account_bound(
    voice_setup, monkeypatch, tmp_path
):
    from tacit_cli.voice_pipeline import resume
    from tacit_client.client import ClientError

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    item, body = start(client, owner)
    retained = Spool.create(
        repo="gh-101", source=body, server="http://localhost:8000", author="gh-1"
    )
    retained.meta["backend_id"] = item["id"]
    retained.append(b"\0\0" * 16000)
    retained.meta["status"] = "recorded"
    retained.save()
    # Server committed the chunk, but the client lost its acknowledgement.
    assert chunk(client, owner, item["id"], 0).status_code == 200

    class Wire:
        def __init__(self, socket):
            self.socket = socket

        def recv(self, timeout=15):
            return self.socket.receive_text()

        def send(self, packet):
            self.socket.send_bytes(packet)

    class Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            kwargs.pop("timeout", None)
            response = client.request(method, path, headers=owner, **kwargs)
            if response.status_code >= 400:
                raise ClientError(response.json()["code"])
            return response.json()

        @contextmanager
        def voice_socket(self, path):
            with client.websocket_connect(path, headers=owner) as socket:
                yield Wire(socket)

    pipeline = importlib.import_module("tacit_cli.voice_pipeline")
    monkeypatch.setattr(pipeline, "TacitClient", Bridge)
    monkeypatch.setattr(pipeline, "api_url", lambda: "http://localhost:8000")
    retained.meta["author"] = "other"
    retained.save()
    with pytest.raises(ClientError, match="different account"):
        resume(Spool(retained.id))
    assert speech.calls == 0
    retained.meta["author"] = "gh-1"
    retained.save()
    result = resume(Spool(retained.id))
    assert result["status"] == "ready_for_review" and speech.calls == 1
    assert not retained.path.exists()


def test_local_partial_requires_consent_and_exports_without_network(monkeypatch, tmp_path):
    from tacit_cli.voice_pipeline import resume

    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    retained = Spool.create(repo="gh-101", source={}, server="http://localhost:8000", author="gh-1")
    retained.append(b"\0\0" * 100)
    retained.meta["status"] = "partial"
    retained.save()
    with pytest.raises(AudioError, match="accept-partial"):
        resume(retained)
    cli = importlib.import_module("tacit_cli.main")
    result = CliRunner().invoke(
        cli.app, ["voice", "export", retained.id, "--output", str(tmp_path / "heard.wav")]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "heard.wav").stat().st_mode & 0o777 == 0o600
    result = CliRunner().invoke(cli.app, ["voice", "forget", retained.id])
    assert result.exit_code == 0 and not retained.path.exists()


def test_websocket_revocation_and_malformed_frames_stop_upload(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    item, _ = start(client, owner)
    route = f"/v1/repos/gh-101/voice-sessions/{item['id']}/upload"
    with client.websocket_connect(route, headers=owner) as socket:
        socket.receive_json()
        socket.send_bytes(b"invalid")
        assert socket.receive_json()["code"] == "invalid_audio_chunk"
    # Removing the source repository is observed before another chunk is retained.
    from test_access_management import webhook

    with client.websocket_connect(route, headers=owner) as socket:
        socket.receive_json()
        assert (
            webhook(
                client, dict(installation=dict(id=10), repositories_removed=[dict(id=101)])
            ).status_code
            == 200
        )
        socket.send_bytes((0).to_bytes(8, "big") + b"\0\0" * 16000)
        assert socket.receive_json()["code"] == "not_found"
    assert speech.calls == 0


def test_operation_ids_are_exclusive_across_voice_and_text(voice_setup):
    from test_voice import send, voice_body

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    item, body = start(client, owner)
    clip = voice_body()
    clip["operation_id"] = body["operation_id"]
    assert send(client, owner, clip).status_code == 409
    text = {k: v for k, v in body.items() if k != "language"} | {"statement": "A decision"}
    assert (
        client.post("/v1/repos/gh-101/teaching-sessions", headers=owner, json=text).status_code
        == 409
    )
    assert speech.calls == 0
    clip["operation_id"] = str(uuid4())
    assert send(client, owner, clip).status_code == 200
    body["operation_id"] = clip["operation_id"]
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body).status_code == 409
    )
    assert speech.calls == 1
    assert chunk(client, owner, item["id"], 0).status_code == 200
    assert finish(client, owner, item["id"], 1, b"\0\0" * 16000).status_code == 200
    assert stt(client, owner, item["id"]).status_code == 200
    assert speech.calls == 2
    text["operation_id"] = str(uuid4())
    assert (
        client.post("/v1/repos/gh-101/teaching-sessions", headers=owner, json=text).status_code
        == 200
    )
    body["operation_id"] = text["operation_id"]
    clip["operation_id"] = text["operation_id"]
    assert send(client, owner, clip).status_code == 409
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body).status_code == 409
    )
    assert speech.calls == 2


def test_uncertain_sessions_retain_quota_until_cancelled(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    speech.fail = True
    for _ in range(3):
        item, body = start(client, owner)
        assert chunk(client, owner, item["id"], 0).status_code == 200
        assert finish(client, owner, item["id"], 1, b"\0\0" * 16000).status_code == 200
        assert stt(client, owner, item["id"]).status_code == 503
    body["operation_id"] = str(uuid4())
    response = client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body)
    assert response.status_code == 409 and response.json()["code"] == "voice_quota"
    assert (
        client.delete(f"/v1/repos/gh-101/voice-sessions/{item['id']}", headers=owner).status_code
        == 200
    )
    assert (
        client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body).status_code == 200
    )
    assert speech.calls == 3


@pytest.mark.parametrize("state", ["ready", "processing", "uncertain"])
def test_resume_finalized_session_never_reuploads_or_finishes(monkeypatch, tmp_path, state):
    from tacit_cli.voice_pipeline import resume

    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    spool = Spool.create(repo="gh-101", source={}, server="http://localhost:8000", author="gh-1")
    spool.append(b"\0\0" * 100)
    spool.meta["status"] = "recorded"
    spool.save()
    calls = []

    class Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, route, **kwargs):
            calls.append(route)
            if route == "/v1/auth/me":
                return {"user_id": "gh-1", "agent": False}
            if route.endswith("/transcribe"):
                return {"status": "completed", "draft": {"id": "recovered"}}
            assert route.endswith("/voice-sessions")
            return {
                "id": "session",
                "status": state,
                "audio_sha256": spool.digest(),
                "pcm_bytes": 200,
                "chunks": 1,
            }

        def voice_socket(self, *args):
            raise AssertionError("Must not upload attempted audio")

    pipeline = importlib.import_module("tacit_cli.voice_pipeline")
    monkeypatch.setattr(pipeline, "TacitClient", Bridge)
    monkeypatch.setattr(pipeline, "api_url", lambda: "http://localhost:8000")
    assert resume(spool) == {"id": "recovered"}
    assert len(calls) == 3 and not spool.path.exists()


def test_real_socket_upload_and_cli_recovery_use_saved_transcript(
    voice_setup, monkeypatch, tmp_path
):
    import socket
    import threading
    import time

    import uvicorn
    from tacit_api.publication import PublicationService
    from tacit_cli.voice_pipeline import resume
    from tacit_client.client import ClientError
    from tacit_domain.access import DomainError

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("TACIT_TOKEN", owner["Authorization"].removeprefix("Bearer "))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TACIT_API_URL", url)
    server = uvicorn.Server(
        uvicorn.Config(client.app, log_level="error", lifespan="off", ws_max_size=32768)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    original = PublicationService.create
    attempts = 0

    def fail_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DomainError("temporary_draft_failure", "Fixture temporary failure", 503)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PublicationService, "create", fail_once)
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        body = dict(
            operation_id=str(uuid4()),
            language="en-IN",
            source_commit=COMMIT,
            files=["worker.py"],
            expected_snapshot_id=None,
        )
        spool = Spool.create(repo="gh-101", source=body, server=url, author="gh-1")
        spool.append(b"\0\0" * 16000)
        spool.meta.update(status="recorded", capture_complete=True)
        spool.save()
        with pytest.raises(ClientError, match="temporary_draft_failure"):
            resume(spool)
        assert speech.calls == 1
        with Session(database) as db:
            stored = db.scalar(select(VoiceSession))
            assert stored.status == "uncertain"
            assert db.scalar(select(func.count()).select_from(VoiceChunk)) == 1
        result = resume(Spool(spool.id))
        assert result["status"] == "ready_for_review" and speech.calls == 1
        assert not spool.path.exists()
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_foreign_cached_transcript_is_rejected_without_deleting_audio(voice_setup):
    from tacit_api.db import VoiceAttempt, VoiceTranscript
    from test_voice import send

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    assert send(client, owner).status_code == 200
    item, body = start(client, owner)
    sid = item["id"]
    assert chunk(client, owner, sid, 0).status_code == 200
    assert finish(client, owner, sid, 1, b"\0\0" * 16000).status_code == 200
    # Simulate data written by the old vulnerable cross-transport path.
    with Session(database) as db, db.begin():
        original = db.scalar(select(VoiceTranscript))
        payload = json.loads(json.dumps(original.payload))
        payload["request"]["operation_id"] = body["operation_id"]
        payload["voice"].update(operation_id=body["operation_id"], duration_seconds=1.0)
        db.add(
            VoiceAttempt(
                workspace_id=original.workspace_id,
                repository_id=original.repository_id,
                author_id=original.author_id,
                operation_id=body["operation_id"],
                request_digest="0" * 64,
            )
        )
        db.flush()
        db.add(
            VoiceTranscript(
                workspace_id=original.workspace_id,
                repository_id=original.repository_id,
                author_id=original.author_id,
                operation_id=body["operation_id"],
                payload=payload,
            )
        )
    response = stt(client, owner, sid)
    assert response.status_code == 409 and response.json()["code"] == "idempotency_conflict"
    assert speech.calls == 1
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(VoiceChunk)) == 1
        assert db.scalar(select(VoiceSession)).status == "ready"


def test_full_local_quota_does_not_allocate_backend_session(monkeypatch, tmp_path):
    from tacit_cli.voice_pipeline import record_session
    from tacit_contracts.models import VoiceSessionRequest

    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))
    for _ in range(3):
        Spool.create(repo="gh-101", source={}, server="http://localhost:8000", author="gh-1")

    class Bridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, route, **kwargs):
            assert method == "GET", "No server allocation when local capacity is full"
            return (
                {"configured": True}
                if route.endswith("voice-capabilities")
                else {"agent": False, "user_id": "gh-1"}
            )

    pipeline = importlib.import_module("tacit_cli.voice_pipeline")
    monkeypatch.setattr(pipeline, "TacitClient", Bridge)
    request = VoiceSessionRequest(
        operation_id=str(uuid4()),
        source_commit=COMMIT,
        files=["worker.py"],
        expected_snapshot_id=None,
        language="en-IN",
    )
    with pytest.raises(AudioError, match="Three local recordings"):
        record_session("gh-101", request, seconds=1, device=None, audio_file=None)


def test_concurrent_local_starts_cannot_exceed_retained_quota(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "spool"))

    def create(_):
        try:
            return Spool.create(
                repo="gh-101", source={}, server="http://localhost:8000", author="gh-1"
            ).id
        except AudioError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create, range(6)))
    assert sum(item is not None for item in results) == 3
    assert len(sessions()) == 3


def test_text_websocket_packet_is_rejected_cleanly(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    item, _ = start(client, owner)
    with client.websocket_connect(
        f"/v1/repos/gh-101/voice-sessions/{item['id']}/upload", headers=owner
    ) as socket:
        socket.receive_json()
        socket.send_text("unexpected text")
        assert socket.receive_json()["code"] == "invalid_audio_chunk"
    assert speech.calls == 0


def test_postgres_cross_transport_reservation_race(voice_setup):
    from concurrent.futures import ThreadPoolExecutor

    from test_voice import send, voice_body

    client, provider, database, speech = voice_setup
    if database.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    owner = login(client, provider, database)
    from sqlalchemy import event

    errors = []

    @event.listens_for(database, "handle_error")
    def capture_error(context):
        errors.append(str(context.original_exception))

    clip = voice_body()
    body = {k: v for k, v in clip.items() if k != "audio_base64"}

    def request(kind):
        return (
            send(client, owner, clip)
            if kind == "clip"
            else client.post("/v1/repos/gh-101/voice-sessions", headers=owner, json=body)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ["clip", "session"]))
    assert sorted(r.status_code for r in results) == [200, 409], errors
    assert speech.calls == (1 if results[0].status_code == 200 else 0)
