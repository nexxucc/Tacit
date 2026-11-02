"""Fixture acceptance for synthesis, playback and terminal review; no billed calls/devices."""

import asyncio
import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tacit_api.access import AccessService
from tacit_api.app import Settings, create_app
from tacit_api.artifacts import Artifacts, Objects
from tacit_api.db import Artifact, ModelOperation
from tacit_api.github import GitHub
from tacit_api.publication import PublicationService
from tacit_api.timbre import Timbre
from tacit_cli import audio, tui
from tacit_contracts.models import SpeakRequest
from tacit_domain.access import DomainError
from test_access_management import Provider, login
from test_access_management import engine as engine
from textual.widgets import Static, TextArea


def pcm_wav(frames=48, rate=48000):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames)
    return out.getvalue()


@pytest.fixture
def synthesis(engine, tmp_path):
    provider = Provider()
    key = Fernet.generate_key().decode()
    github = GitHub("fixture", 100, httpx.MockTransport(provider.request))
    access = AccessService(engine, github, key)
    artifacts = Artifacts(PublicationService(access), Objects(str(tmp_path / "audio")), 3600)
    app = create_app(
        Settings(github_client_id="fixture", github_app_id=100, credential_encryption_key=key),
        github=github,
        database=engine,
    )
    with TestClient(app) as client:
        owner = login(client, provider, engine)
        token = owner["Authorization"].removeprefix("Bearer ")
        yield access.principal(token), token, artifacts, engine, provider


def speak_body(**updates):
    return SpeakRequest(
        operation_id="synthesis-fixture",
        text="May an expired worker publish?",
        voice="Kaveri",
        **updates,
    )


def test_timbre_contract_encrypted_artifact_exact_retry_and_input_conflict(synthesis):
    principal, token, artifacts, database, provider = synthesis
    calls = []
    expected = pcm_wav()

    def response(request):
        calls.append(request)
        assert str(request.url) == "https://api.vachana.ai/api/v1/tts/inference"
        assert request.headers["X-API-Key-ID"] == "fixture-only"
        assert json.loads(request.content) == {
            "text": "May an expired worker publish?",
            "voice": "Kaveri",
            "model": "timbre-v2.5",
            "language": "en-IN",
            "audio_config": {
                "encoding": "linear_pcm",
                "container": "wav",
                "sample_rate": 48000,
                "num_channels": 1,
                "sample_width": 2,
            },
        }
        return httpx.Response(200, content=expected)

    timbre = Timbre("fixture-only", artifacts, transport=httpx.MockTransport(response))
    body = speak_body()
    result = asyncio.run(timbre.speak(principal, "gh-101", token, body))
    assert result["usage"] == {"tts_calls": 1}
    assert artifacts.read(principal, "gh-101", token, result["artifact_id"]) == expected
    assert asyncio.run(timbre.speak(principal, "gh-101", token, body)) == result
    assert len(calls) == 1
    with Session(database) as db:
        artifact = db.scalar(select(Artifact))
        assert artifacts.objects.get(artifact.object_key) != expected
        assert db.scalar(select(func.count()).select_from(ModelOperation)) == 1
    with pytest.raises(DomainError) as error:
        asyncio.run(
            timbre.speak(principal, "gh-101", token, body.model_copy(update={"text": "Changed"}))
        )
    assert error.value.code == "idempotency_conflict"
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", [b"RIFF", pcm_wav(0), pcm_wav()[:-2], pcm_wav(rate=16000)])
def test_timbre_rejects_invalid_audio_without_artifact_or_paid_retry(synthesis, invalid):
    principal, token, artifacts, database, provider = synthesis
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, content=invalid)

    timbre = Timbre("fixture-only", artifacts, transport=httpx.MockTransport(response))
    with pytest.raises(DomainError) as error:
        asyncio.run(timbre.speak(principal, "gh-101", token, speak_body()))
    assert error.value.code == "synthesis_failed"
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0
        assert db.scalar(select(ModelOperation)).status == "processing"
    with pytest.raises(DomainError) as error:
        asyncio.run(timbre.speak(principal, "gh-101", token, speak_body()))
    assert error.value.code == "model_outcome_uncertain"
    assert len(calls) == 1


def test_timbre_rechecks_provider_access_before_storing_audio(synthesis):
    principal, token, artifacts, database, provider = synthesis

    def response(request):
        provider.access = False
        return httpx.Response(200, content=pcm_wav())

    with pytest.raises(DomainError):
        asyncio.run(
            Timbre("fixture", artifacts, transport=httpx.MockTransport(response)).speak(
                principal, "gh-101", token, speak_body()
            )
        )
    with Session(database) as db:
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0


class Output:
    def __init__(self, error=None):
        self.error, self.closed, self.written = error, False, []

    def __enter__(self):
        if self.error == "start":
            raise RuntimeError("fixture output device unavailable")
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def write(self, data):
        if self.error == "write":
            raise RuntimeError("fixture playback device lost")
        self.written.append(data)


@pytest.mark.parametrize("error", [None, "start", "write"])
def test_playback_closes_device_and_reports_device_errors(monkeypatch, error):
    stream = Output(error)
    monkeypatch.setattr(
        audio, "backend", lambda: SimpleNamespace(RawOutputStream=lambda **kw: stream)
    )
    if error:
        with pytest.raises(audio.AudioError, match="output device"):
            audio.play(pcm_wav())
    else:
        audio.play(pcm_wav())
        assert b"".join(stream.written) == b"\x01\x00" * 48
    assert stream.closed


@pytest.mark.parametrize("invalid", [pcm_wav(0), pcm_wav()[:-2], b"RIFF"])
def test_playback_rejects_empty_or_truncated_audio(monkeypatch, invalid):
    stream = Output()
    monkeypatch.setattr(
        audio, "backend", lambda: SimpleNamespace(RawOutputStream=lambda **kw: stream)
    )
    with pytest.raises(audio.AudioError):
        audio.play(invalid)


def test_headless_review_refreshes_versions_and_requires_separate_confirmation(monkeypatch):
    payload = {
        "id": "run",
        "repository_id": "gh-101",
        "author_id": "gh-1",
        "version": 1,
        "status": "needs_clarification",
        "candidates": [
            {
                "id": "candidate",
                "statement": "Expired workers must not publish.",
                "transcript_quote": "Expired workers must not publish.",
                "citations": [
                    {"path": "worker.py", "blob_sha": "b" * 40, "start_line": 1, "end_line": 1}
                ],
            }
        ],
        "source": {
            "operation_id": "extract",
            "source_commit": "a" * 40,
            "files": ["worker.py"],
            "statement": "Expired workers must not publish.",
            "expected_snapshot_id": None,
        },
    }
    captured, calls = [], []
    monkeypatch.setattr(tui.App, "run", lambda self: captured.append(self))

    def request(method, path, body):
        calls.append((method, path, body))
        assert body["expected_version"] == len(calls)
        return {
            **payload,
            "version": len(calls) + 1,
            "status": "ready_for_review" if path.endswith("/answers") else "drafted",
        }

    monkeypatch.setattr(tui, "request", request)
    tui.review_teaching("gh-101", payload)

    async def exercise():
        async with captured[0].run_test(size=(120, 90)) as pilot:
            captured[0].query_one("#save").scroll_visible(immediate=True)
            await pilot.pause()
            captured[0].query_one("#save").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert (
                json.loads(captured[0].query_one("#answers", TextArea).text)["expected_version"]
                == 2
            )
            captured[0].query_one("#draft").scroll_visible(immediate=True)
            await pilot.pause()
            captured[0].query_one("#draft").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert "tacit confirm" in str(captured[0].query_one("#result", Static).render())

    asyncio.run(exercise())
    assert [call[1] for call in calls] == [
        "/v1/repos/gh-101/teaching-runs/run/answers",
        "/v1/repos/gh-101/teaching-runs/run/draft",
    ]
    assert calls[0][2]["operation_id"] != calls[1][2]["operation_id"]


def test_unknown_length_pcm_wav_is_finalized_without_changing_samples():
    from tacit_api.timbre import finalize_wav_lengths

    expected = pcm_wav()
    streamed = expected[:4] + b"\xff" * 4 + expected[8:40] + b"\xff" * 4 + expected[44:]
    assert finalize_wav_lengths(streamed) == expected
    assert finalize_wav_lengths(expected[:-2]) == expected[:-2]  # Never repair ordinary truncation.
    with pytest.raises(ValueError):
        finalize_wav_lengths(streamed[:-1])
