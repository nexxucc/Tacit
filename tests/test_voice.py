"""Voice boundary tests: fixture PCM and provider responses, never microphone or paid API."""

import asyncio
import base64
import importlib
import io
import json
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from tacit_api.app import Settings, create_app
from tacit_api.github import GitHub
from tacit_api.providers import ProviderUnavailable
from tacit_api.voice import Prisma, validate_audio
from tacit_cli.audio import AudioError, RecordingCancelled, record
from test_access_management import Provider, login, webhook
from test_access_management import engine as engine
from test_publication import publish
from typer.testing import CliRunner

COMMIT = "a" * 40


def wav(frames=1600, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


class Speech:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.hook = None

    async def transcribe(self, audio, sample_rate, *, language="en-IN"):
        assert sample_rate == 16000
        assert validate_audio(audio) > 0
        self.calls += 1
        if self.hook:
            from starlette.concurrency import run_in_threadpool

            await run_in_threadpool(self.hook)
        if self.fail:
            raise ProviderUnavailable("fixture failure with SECRET that must not leak")
        return "Only human maintainers publish."


@pytest.fixture
def voice_setup(engine):
    provider, speech = Provider(), Speech()
    settings = Settings(
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=Fernet.generate_key().decode(),
        github_webhook_secret="fixture-secret",
    )
    app = create_app(
        settings,
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
        speech=speech,
    )
    with TestClient(app) as client:
        yield client, provider, engine, speech


def voice_body(**updates):
    return dict(
        operation_id="voice-op",
        audio_base64=base64.b64encode(wav()).decode(),
        language="en-IN",
        source_commit=COMMIT,
        files=["worker.py"],
        expected_snapshot_id=None,
        **updates,
    )


def send(client, owner, body=None):
    return client.post("/v1/repos/gh-101/voice-drafts", headers=owner, json=body or voice_body())


def test_voice_correction_publication_and_exact_retry(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    response = send(client, owner)
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["voice"]["original_transcript"] == original["statement"]
    assert original["voice"]["corrected"] is False
    assert send(client, owner).json() == original
    assert speech.calls == 1
    changed = voice_body()
    changed["language"] = "hi-IN"
    assert send(client, owner, changed).json()["code"] == "idempotency_conflict"
    assert speech.calls == 1
    request = dict(
        operation_id="correction",
        statement="Only human maintainers may publish repository knowledge.",
        source_commit=COMMIT,
        files=["worker.py"],
        expected_snapshot_id=None,
        transcript_draft_id=original["id"],
    )
    correction = client.post(
        "/v1/repos/gh-101/teaching-sessions", headers=owner, json=request
    ).json()
    assert correction["voice"]["original_transcript"] == original["statement"]
    assert correction["voice"]["original_draft_id"] == original["id"]
    assert correction["voice"]["corrected"] is True
    assert correction["review_digest"] != original["review_digest"]
    unchanged = client.get(
        f"/v1/repos/gh-101/teaching-sessions/{original['id']}", headers=owner
    ).json()
    assert unchanged == original
    request["files"] = ["other.py"]
    assert (
        client.post("/v1/repos/gh-101/teaching-sessions", headers=owner, json=request).status_code
        == 422
    )
    result = publish(client, owner, correction)
    assert result.status_code == 200, result.text
    context = client.post(
        "/v1/repos/gh-101/context",
        headers=owner,
        json=dict(snapshot_id=result.json()["snapshot_id"], source_commit=COMMIT),
    ).json()
    assert context["decisions"][0]["statement"] == correction["statement"]
    assert context["evidence"][0]["kind"] == "transcript"
    assert context["usage"] == dict(
        stt_calls=0, tts_calls=0, reasoning_calls=0, input_tokens=0, output_tokens=0
    )
    assert speech.calls == 1
    recovered = client.get("/v1/repos/gh-101/voice-transcripts/voice-op", headers=owner)
    assert recovered.status_code == 200
    assert recovered.json()["voice"]["original_transcript"] == original["statement"]
    assert client.get("/v1/repos/gh-101/voice-transcripts/voice-op", headers={}).status_code == 401
    if database.dialect.name == "postgresql":
        with pytest.raises(Exception, match="Tacit immutable record"):
            with database.begin() as conn:
                conn.execute(text("UPDATE voice_attempts SET request_digest = 'tampered'"))
        with pytest.raises(Exception, match="Tacit immutable record"):
            with database.begin() as conn:
                conn.execute(text("DELETE FROM voice_transcripts"))


def test_denial_validation_failure_and_no_provider_replay(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    agent = client.post(
        "/v1/auth/agent-tokens", headers=owner, json=dict(repository_ids=["gh-101"])
    ).json()
    assert send(client, {"Authorization": "Bearer " + agent["token"]}).status_code == 403
    assert send(client, {}).status_code == 401
    assert (
        client.post("/v1/repos/gh-202/voice-drafts", headers=owner, json=voice_body()).status_code
        == 403
    )
    bad = voice_body()
    bad["audio_base64"] = base64.b64encode(wav(rate=8000)).decode()
    assert send(client, owner, bad).status_code == 422
    bad["audio_base64"] = "invalid!"
    assert send(client, owner, bad).status_code == 422
    bad = voice_body()
    bad["files"] = ["missing.py"]
    assert send(client, owner, bad).json()["code"] == "source_unbound"
    assert (
        client.post(
            "/v1/repos/gh-101/voice-drafts", headers=owner, content=b"x" * 2_580_001
        ).status_code
        == 413
    )
    assert speech.calls == 0
    speech.fail = True
    response = send(client, owner)
    assert response.status_code == 503 and "SECRET" not in response.text
    speech.fail = False
    assert send(client, owner).json()["code"] == "voice_outcome_uncertain"
    assert speech.calls == 1
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


def test_revocation_during_transcription_does_not_create_draft(voice_setup):
    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    speech.hook = lambda: webhook(
        client, dict(installation=dict(id=10), repositories_removed=[dict(id=101)])
    )
    result = send(client, owner)
    assert result.status_code == 404
    assert speech.calls == 1


def test_prisma_transport_contract_and_failure_redaction(caplog):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url == "https://api.vachana.ai/stt/v3"
        assert request.headers["X-API-Key-ID"] == "fixture-key"
        assert b'name="audio_file"' in request.content
        assert b"en-IN" in request.content and b"verbatim" in request.content
        return httpx.Response(200, json=dict(success=True, transcript="Exact fixture text."))

    assert (
        asyncio.run(
            Prisma("fixture-key", transport=httpx.MockTransport(respond)).transcribe(wav(), 16000)
        )
        == "Exact fixture text."
    )
    assert len(calls) == 1
    for response in [
        httpx.Response(429, text="fixture-key SECRET"),
        httpx.Response(200, json=dict(success=True, transcript="")),
        httpx.Response(200, json=[]),
    ]:
        with pytest.raises(ProviderUnavailable) as error:
            asyncio.run(
                Prisma(
                    "fixture-key",
                    transport=httpx.MockTransport(lambda request, response=response: response),
                ).transcribe(wav(), 16000)
            )
        assert "SECRET" not in str(error.value) and "fixture-key" not in str(error.value)
    assert "stage=http_status exception=HTTPStatusError http_status=429" in caplog.text
    assert "stage=transcript_validation" in caplog.text
    assert "SECRET" not in caplog.text and "fixture-key" not in caplog.text

    calls.clear()

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("SECRET fixture-key", request=request)

    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            Prisma("fixture-key", transport=httpx.MockTransport(timeout)).transcribe(wav(), 16000)
        )
    assert len(calls) == 1
    assert "stage=request exception=ReadTimeout http_status=None" in caplog.text
    assert "SECRET" not in caplog.text and "fixture-key" not in caplog.text


class Capture:
    def __init__(self, mode="ok"):
        self.mode, self.closed = mode, False

    def check_input_settings(self, **kwargs):
        assert kwargs["samplerate"] == 16000

    def RawInputStream(self, **kwargs):
        self.callback = kwargs["callback"]
        return self

    def __enter__(self):
        if self.mode == "start_cancel":
            raise KeyboardInterrupt
        if self.mode == "cancel":
            # Interrupt during consumption so context manager must close.
            return self
        for _ in range(65 if self.mode == "overflow" else 1):
            self.callback(b"\0\0" * 1600, 1600, None, self.mode == "device")
        return self

    def close(self):
        self.closed = True

    def __exit__(self, *args):
        self.close()


def test_capture_valid_wav_bounded_queue_errors_and_cancel(monkeypatch):
    device = Capture()
    assert validate_audio(record(0.01, sd=device, interactive=False)) == 0.01
    assert device.closed
    for mode in ["overflow", "device"]:
        device = Capture(mode)
        with pytest.raises(AudioError, match="discarded"):
            record(0.01, sd=device, interactive=False)
        assert device.closed
    device = Capture("start_cancel")
    with pytest.raises(RecordingCancelled):
        record(0.01, sd=device, interactive=False)
    assert device.closed
    device = Capture("cancel")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("tacit_cli.audio.queue.Queue.get", interrupt)
    with pytest.raises(RecordingCancelled):
        record(0.01, sd=device, interactive=False)
    assert device.closed
    assert validate_audio(wav(480000)) == 30
    for data in [b"bad", wav(0), wav(480001), wav()[:-1]]:
        with pytest.raises(ValueError):
            validate_audio(data)


def test_cli_voice_upload_cancel_and_file_input(voice_setup, monkeypatch, tmp_path):
    from tacit_client.client import ClientError

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)

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

    cli = importlib.import_module("tacit_cli.main")
    monkeypatch.setattr(cli, "TacitClient", Bridge)
    path = tmp_path / "fixture.wav"
    path.write_bytes(wav())
    args = [
        "teach-voice",
        "--clip",
        "--repo",
        "gh-101",
        "--commit",
        COMMIT,
        "--file",
        "worker.py",
        "--audio-file",
        str(path),
    ]
    runner = CliRunner()
    result = runner.invoke(cli.app, args, input="n\n")
    assert result.exit_code == 0, result.output
    assert speech.calls == 0 and path.exists()
    result = runner.invoke(cli.app, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ready_for_review"
    assert speech.calls == 1 and path.exists()


def test_postgres_simultaneous_voice_retry_calls_provider_once(voice_setup):
    client, provider, database, speech = voice_setup
    if database.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    owner = login(client, provider, database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(client, owner), range(2)))
    assert any(r.status_code == 200 for r in results)
    assert all(r.status_code in (200, 409) for r in results)
    assert speech.calls == 1


def test_transcript_survives_draft_failure_and_retry_does_not_bill(voice_setup, monkeypatch):
    from tacit_api.publication import PublicationService
    from tacit_domain.access import DomainError

    client, provider, database, speech = voice_setup
    owner = login(client, provider, database)
    original_create = PublicationService.create

    def conflict(*args, **kwargs):
        raise DomainError("snapshot_conflict", "Fixture head change", 409)

    monkeypatch.setattr(PublicationService, "create", conflict)
    assert send(client, owner).status_code == 409
    transcript = client.get("/v1/repos/gh-101/voice-transcripts/voice-op", headers=owner)
    assert transcript.json()["voice"]["original_transcript"] == "Only human maintainers publish."
    monkeypatch.setattr(PublicationService, "create", original_create)
    assert send(client, owner).status_code == 200
    assert speech.calls == 1
