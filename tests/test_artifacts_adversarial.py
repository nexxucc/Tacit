"""Adversarial encrypted artifacts and explicit TTS with fixture transports only."""

import asyncio
import io
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_api.access import digest, now
from tacit_api.artifacts import Objects
from tacit_api.db import Artifact, Credential, ModelOperation, User
from tacit_api.timbre import Timbre
from tacit_contracts.models import SpeakRequest
from tacit_domain.access import DomainError
from test_access_management import engine as engine
from test_reliability import runtime as runtime
from test_voice_acceptance import pcm_wav


def artifact_row(access, aid):
    with Session(access.engine) as db:
        return db.get(Artifact, ("gh-1", "gh-101", aid))


@pytest.mark.parametrize("failure", ["ciphertext", "missing", "oversize"])
def test_unreadable_artifact_returns_safe_domain_failure(runtime, monkeypatch, failure):
    _, _, access, artifacts, principal, token = runtime
    result = artifacts.create(principal, "gh-101", token, b"private audio")
    row = artifact_row(access, result["artifact_id"])
    if failure == "ciphertext":
        artifacts.objects.local(row.object_key).write_bytes(b"private invalid token")
    elif failure == "missing":
        artifacts.objects.delete(row.object_key)
    else:
        monkeypatch.setattr(
            artifacts.objects,
            "get",
            lambda _: (_ for _ in ()).throw(ValueError("private storage bounds")),
        )
    with pytest.raises(DomainError) as raised:
        artifacts.read(principal, "gh-101", token, result["artifact_id"])
    assert raised.value.code == (
        "artifact_unavailable" if failure == "missing" else "artifact_corrupt"
    )
    assert "private" not in str(raised.value)


def test_scope_author_expiry_and_deletion_rejected_before_storage_read(runtime, monkeypatch):
    _, _, access, artifacts, principal, token = runtime
    result = artifacts.create(principal, "gh-101", token, b"audio")
    aid = result["artifact_id"]
    reads = []
    monkeypatch.setattr(artifacts.objects, "get", lambda key: reads.append(key))
    with Session(access.engine) as db, db.begin():
        db.add(User(id="other-author", github_id="900"))
        db.flush()
        db.get(Artifact, ("gh-1", "gh-101", aid)).author_id = "other-author"
    with pytest.raises(DomainError) as failed:
        artifacts.read(principal, "gh-101", token, aid)
    assert failed.value.status == 404 and not reads
    with Session(access.engine) as db, db.begin():
        row = db.get(Artifact, ("gh-1", "gh-101", aid))
        row.author_id = principal.user_id
        row.expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError) as failed:
        artifacts.read(principal, "gh-101", token, aid)
    assert failed.value.status == 410 and not reads
    with pytest.raises(DomainError):
        artifacts.read(principal, "gh-202", token, aid)
    assert not reads


def test_delete_failure_retains_retryable_row_and_object(runtime, monkeypatch):
    _, _, access, artifacts, principal, token = runtime
    result = artifacts.create(principal, "gh-101", token, b"audio")
    aid = result["artifact_id"]
    original = artifacts.objects.delete

    def failed(_):
        raise OSError("fixture storage failure")

    monkeypatch.setattr(artifacts.objects, "delete", failed)
    with pytest.raises(OSError):
        artifacts.delete(principal, "gh-101", token, aid)
    row = artifact_row(access, aid)
    assert row.deleted_at is None and artifacts.objects.local(row.object_key).exists()
    monkeypatch.setattr(artifacts.objects, "delete", original)
    artifacts.delete(principal, "gh-101", token, aid)
    artifacts.delete(principal, "gh-101", token, aid)
    assert not artifacts.objects.local(row.object_key).exists()


def test_revoke_during_object_write_never_returns_ready_audio(runtime, monkeypatch):
    _, _, access, artifacts, principal, token = runtime
    original = artifacts.objects.put

    def revoke(key, data):
        original(key, data)
        with Session(access.engine) as db, db.begin():
            db.get(Credential, digest(token)).revoked = True

    monkeypatch.setattr(artifacts.objects, "put", revoke)
    with pytest.raises(DomainError):
        artifacts.create(principal, "gh-101", token, b"audio")
    with Session(access.engine) as db:
        row = db.scalar(select(Artifact))
        assert row.status == "pending" and row.deleted_at is None


@pytest.mark.parametrize(
    "change", [{"voice": "Other"}, {"language": "hi-IN"}, {"text": "Changed question"}]
)
def test_synthesis_changed_retry_never_calls_provider(runtime, change):
    _, _, _, artifacts, principal, token = runtime
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=pcm_wav())

    tts = Timbre("fixture-key", artifacts, transport=httpx.MockTransport(respond))
    body = SpeakRequest(operation_id=str(uuid4()), text="Question?", voice="Kaveri")
    asyncio.run(tts.speak(principal, "gh-101", token, body))
    with pytest.raises(DomainError) as failed:
        asyncio.run(tts.speak(principal, "gh-101", token, body.model_copy(update=change)))
    assert failed.value.code == "idempotency_conflict" and len(calls) == 1


@pytest.mark.parametrize("failure", ["redirect", "partial_body", "store_failure"])
def test_synthesis_failure_never_replays_billable_operation(runtime, monkeypatch, failure):
    _, _, access, artifacts, principal, token = runtime
    calls = []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield pcm_wav()[:20]
            raise httpx.ReadError("private provider failure")

    def respond(request):
        calls.append(request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid/private"})
        if failure == "partial_body":
            return httpx.Response(200, stream=Broken())
        return httpx.Response(200, content=pcm_wav())

    if failure == "store_failure":

        def fail_store(*args, **kwargs):
            raise DomainError("storage_failed", "Fixture storage unavailable", 503)

        monkeypatch.setattr(artifacts, "create", fail_store)
    tts = Timbre("fixture-key", artifacts, transport=httpx.MockTransport(respond))
    body = SpeakRequest(operation_id=str(uuid4()), text="Question?", voice="Kaveri")
    with pytest.raises(DomainError):
        asyncio.run(tts.speak(principal, "gh-101", token, body))
    with pytest.raises(DomainError) as failed:
        asyncio.run(tts.speak(principal, "gh-101", token, body))
    assert failed.value.code == "model_outcome_uncertain" and len(calls) == 1
    with Session(access.engine) as db:
        assert db.scalar(select(ModelOperation)).status == "processing"


def test_s3_stream_close_bounds_and_encrypted_object_contract(monkeypatch, tmp_path):
    calls = []
    stream = io.BytesIO(b"opaque ciphertext")

    class Client:
        def put_object(self, **kw):
            calls.append(("put", kw))

        def get_object(self, **kw):
            calls.append(("get", kw))
            return {"Body": stream}

        def delete_object(self, **kw):
            calls.append(("delete", kw))

    objects = Objects(str(tmp_path), "fixture-bucket", "http://fixture.invalid")
    monkeypatch.setattr(objects, "client", lambda: Client())
    objects.put("id", b"opaque ciphertext")
    assert objects.get("id") == b"opaque ciphertext" and stream.closed
    objects.delete("id")
    assert [name for name, _ in calls] == ["put", "get", "delete"]
    assert calls[0][1]["ContentType"] == "application/octet-stream"

    class Oversize:
        closed = False

        def read(self, limit):
            assert limit == 40000001
            return b"x" * limit

        def close(self):
            self.closed = True

    large = Oversize()
    monkeypatch.setattr(
        objects, "client", lambda: SimpleNamespace(get_object=lambda **_: {"Body": large})
    )
    with pytest.raises(ValueError, match="bound"):
        objects.get("id")
    assert large.closed


def test_corrupt_artifact_api_response_is_safe_and_correlated(runtime, monkeypatch):
    import tacit_api.app as application

    client, headers, access, artifacts, principal, token = runtime
    result = artifacts.create(principal, "gh-101", token, b"private audio")
    row = artifact_row(access, result["artifact_id"])
    artifacts.objects.local(row.object_key).write_bytes(b"private invalid token")
    monkeypatch.setattr(application, "Objects", lambda *args, **kwargs: artifacts.objects)
    response = client.get(f"/v1/repos/gh-101/artifacts/{row.id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "artifact_corrupt"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "private" not in response.text


def test_synthesis_http_failure_diagnostics_are_redacted(runtime, caplog):
    _, _, _, artifacts, principal, token = runtime

    def respond(request):
        return httpx.Response(403, json={"error": {"message": "SECRET provider details"}})

    tts = Timbre("SECRET api key", artifacts, transport=httpx.MockTransport(respond))
    body = SpeakRequest(operation_id=str(uuid4()), text="SECRET private text", voice="Kaveri")
    with pytest.raises(DomainError) as error:
        asyncio.run(tts.speak(principal, "gh-101", token, body))
    assert error.value.code == "synthesis_failed"
    assert (
        "stage=http_status exception=HTTPStatusError http_status=403 audio_bytes=0" in caplog.text
    )
    assert "SECRET" not in caplog.text
