"""Evon adapter and complete consumer routes; live GPU checks are explicitly opt-in."""

import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tacit_api.app import Settings, create_app
from tacit_api.db import DecisionRevision, Snapshot, SnapshotMember
from tacit_api.github import GitHub
from tacit_api.providers import ProviderUnavailable
from tacit_api.reasoning import Evon, generation_schema
from tacit_contracts.models import CandidateSet, CitedAnswer, Decision, Evidence
from tacit_domain.rules import matches
from test_access_management import Provider, login
from test_access_management import engine as engine

COMMIT = "a" * 40
TEXT = "Expired workers must not publish results."
SOURCE = (
    'def publish(lease_expired):\n    if lease_expired:\n        raise PermissionError("expired")\n'
)


def completion(content, **changes):
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        **changes,
    }


def test_adapter_constrains_schema_and_disables_thinking():
    requests = []

    def respond(request):
        if request.url.path.endswith("/props"):
            return httpx.Response(404)
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"answer":"No.","revision_ids":["rev-1"]}'))

    model = Evon("http://localhost/v1", "fixture-secret", transport=httpx.MockTransport(respond))
    result, usage = asyncio.run(model.structured("Answer from evidence", {}, CitedAnswer))
    assert result.revision_ids == ("rev-1",)
    assert usage.reasoning_calls == 1 and usage.input_tokens == 10 and usage.output_tokens == 20
    assert requests[0]["response_format"]["schema"]["required"] == ["answer", "revision_ids"]
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    schema = generation_schema(CandidateSet)
    assert schema["$defs"]["Rule"]["properties"]["domain"]["const"] == "scheduler-v1"
    assert schema["$defs"]["Rule"]["properties"]["when"]["minItems"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"choices": []},
        completion("{}"),
        completion('{"answer":"No.","revision_ids":["rev-1"]}', usage={"prompt_tokens": True}),
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"answer":"No.","revision_ids":["rev-1"]}'},
                }
            ]
        },
    ],
)
def test_invalid_or_truncated_model_response_never_retries(payload, caplog):
    calls = 0

    def respond(request):
        if request.url.path.endswith("/props"):
            return httpx.Response(404)
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            Evon(
                "http://localhost/v1", "fixture-secret", transport=httpx.MockTransport(respond)
            ).structured("Read", {}, CitedAnswer)
        )
    assert calls == 1
    assert "fixture-secret" not in caplog.text


def test_input_budget_refuses_network_call():
    def unreachable(request):
        raise AssertionError("Oversized input must not be sent")

    with pytest.raises(ProviderUnavailable, match="input budget"):
        asyncio.run(
            Evon(
                "http://localhost/v1",
                "secret",
                max_input_chars=5,
                transport=httpx.MockTransport(unreachable),
            ).structured("Read", {"text": "too long"}, CitedAnswer)
        )


class SourceProvider(Provider):
    def request(self, request):
        if "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": "worker.py",
                    "sha": "b" * 40,
                    "size": len(SOURCE),
                    "encoding": "base64",
                    "content": base64.b64encode(SOURCE.encode()).decode(),
                },
            )
        return super().request(request)


@pytest.fixture
def live_backend(engine):
    key_file = os.environ.get("TACIT_LIVE_EVON_KEY_FILE")
    if not key_file:
        pytest.skip("Explicit live Evon credentials required; no model calls in default CI")
    provider = SourceProvider()
    settings = Settings(
        github_client_id="fixture",
        github_app_id=100,
        credential_encryption_key=Fernet.generate_key().decode(),
        evon_url=os.environ.get("TACIT_LIVE_EVON_URL", "http://127.0.0.1:8001/v1"),
        evon_api_key=Path(key_file).read_text().strip(),
    )
    app = create_app(
        settings,
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
    )
    with TestClient(app) as client:
        owner = login(client, provider, engine)
        yield client, provider, engine, owner


def test_live_grounded_extraction_clarification_draft_and_no_replay(
    live_backend, monkeypatch, tmp_path
):
    client, provider, db, owner = live_backend
    body = {
        "operation_id": str(uuid4()),
        "source_commit": COMMIT,
        "files": ["worker.py"],
        "statement": TEXT,
        "expected_snapshot_id": None,
    }
    from tacit_cli import teaching
    from typer.testing import CliRunner

    def bridge(method, path, payload=None):
        if method == "POST":
            body.clear()
            body.update(payload)
        response = client.request(
            method, path, headers=owner, **({"json": payload} if payload is not None else {})
        )
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(teaching, "request", bridge)
    intent = tmp_path / "intent.txt"
    intent.write_text(TEXT)
    invoked = CliRunner().invoke(
        teaching.app,
        [
            "extract",
            "--repo",
            "gh-101",
            "--commit",
            COMMIT,
            "--file",
            "worker.py",
            "--text-file",
            str(intent),
            "--operation-id",
            body["operation_id"],
        ],
        input="y\n",
    )
    assert invoked.exit_code == 0, invoked.output
    view = json.loads(invoked.output[invoked.output.index("{") :])
    assert view["usage"]["reasoning_calls"] == 1
    assert view["usage"]["input_tokens"] > 0 and view["usage"]["output_tokens"] > 0
    candidate = view["candidates"][0]
    assert candidate["transcript_quote"] in TEXT
    assert candidate["citations"][0]["path"] == "worker.py"
    assert candidate["citations"][0]["blob_sha"] == "b" * 40
    from tacit_contracts.models import Rule

    rule = Rule.model_validate(candidate["rule"])
    assert rule.domain == "scheduler-v1" and rule.outcome == "forbidden"
    assert matches(rule, {"lease_expired": True}, "publish_result")
    sid = view["id"]
    # An exact retry returns the saved candidates rather than invoking the GPU again.
    assert (
        client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body).json()["id"] == sid
    )
    answers = {
        key: "Yes, expired workers are forbidden to publish."
        for key in view["questions"]
        if key.startswith(candidate["id"] + ":")
    }
    for index, scenario in enumerate(view["scenarios"]):
        answers[f"scenario:{index}"] = (
            rule.outcome if matches(rule, scenario["facts"], scenario["action"]) else "unknown"
        )
    answer = {
        "operation_id": str(uuid4()),
        "expected_version": view["version"],
        "candidate_id": candidate["id"],
        "answers": answers,
        "accept_assumptions": True,
    }
    clarified = client.post(
        f"/v1/repos/gh-101/teaching-runs/{sid}/answers", headers=owner, json=answer
    )
    assert clarified.status_code == 200, clarified.text
    assert clarified.json()["status"] == "ready_for_review"
    prepared_body = {"operation_id": str(uuid4()), "expected_version": clarified.json()["version"]}
    prepared = client.post(
        f"/v1/repos/gh-101/teaching-runs/{sid}/draft", headers=owner, json=prepared_body
    )
    assert prepared.status_code == 200, prepared.text
    draft = prepared.json()["draft"]
    assert draft["grounding"]["candidate"]["transcript_quote"] in TEXT
    assert draft["grounding"]["run_id"] == sid
    assert draft["status"] == "ready_for_review"
    assert (
        client.post(
            f"/v1/repos/gh-101/teaching-runs/{sid}/draft", headers=owner, json=prepared_body
        ).json()["draft"]["id"]
        == draft["id"]
    )
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


def test_live_agent_cited_answer_cache_and_zero_speech(live_backend, monkeypatch):
    client, provider, db, owner = live_backend
    # Seed synthetic confirmed context directly in the isolated schema, never publish to a repository.
    decision = Decision(
        id="expired",
        revision_id="rev-expired",
        statement=TEXT,
        files=("worker.py",),
        source_commit=COMMIT,
        evidence=(
            Evidence(
                decision_id="expired",
                revision_id="rev-expired",
                segment_id="fixture-text",
                kind="text",
            ),
        ),
    )
    with Session(db) as session, session.begin():
        session.add(
            DecisionRevision(
                workspace_id="gh-1",
                repository_id="gh-101",
                id=decision.revision_id,
                decision_id=decision.id,
                confirmed_by="gh-1",
                content=decision.model_dump(mode="json"),
            )
        )
        session.add(
            Snapshot(
                workspace_id="gh-1",
                repository_id="gh-101",
                id="fixture-snapshot",
                source_commit=COMMIT,
                digest="fixture-digest",
            )
        )
        session.flush()
        session.add(
            SnapshotMember(
                workspace_id="gh-1",
                repository_id="gh-101",
                snapshot_id="fixture-snapshot",
                revision_id=decision.revision_id,
            )
        )
    delegated = client.post(
        "/v1/auth/agent-tokens", headers=owner, json={"repository_ids": ["gh-101"]}
    ).json()
    agent = {"Authorization": "Bearer " + delegated["token"]}
    body = {
        "snapshot_id": "fixture-snapshot",
        "source_commit": COMMIT,
        "question": "May an expired worker publish results?",
        "reason": True,
        "operation_id": str(uuid4()),
    }
    response = client.post("/v1/repos/gh-101/questions", headers=agent, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer_kind"] == "cited_reasoning"
    assert "rev-expired" in result["answer"]
    assert result["usage"]["reasoning_calls"] == 1
    assert result["usage"]["stt_calls"] == result["usage"]["tts_calls"] == 0
    assert client.post("/v1/repos/gh-101/questions", headers=agent, json=body).json() == result

    from tacit_mcp import server as mcp

    class Bridge:
        def __init__(self, **kwargs):
            assert kwargs["agent"] is True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            kwargs.pop("timeout", None)
            response = client.request(method, path, headers=agent, **kwargs)
            assert response.status_code == 200, response.text
            return response.json()

    monkeypatch.setattr(mcp, "TacitClient", Bridge)
    content = asyncio.run(
        mcp.create_server().call_tool(
            "ask_repository",
            {
                "repository_id": "gh-101",
                "snapshot_id": body["snapshot_id"],
                "source_commit": COMMIT,
                "question": body["question"],
                "reason": True,
                "operation_id": body["operation_id"],
            },
        )
    )
    assert content[1] == result

    # Invalid revision IDs are rejected even if the provider emits valid JSON.
    attempted = []

    async def wrong_citation(self, instruction, data, schema):
        from tacit_contracts.models import Usage

        attempted.append(1)
        return CitedAnswer(answer="Invented", revision_ids=("not-in-this-snapshot",)), Usage(
            reasoning_calls=1
        )

    monkeypatch.setattr(Evon, "structured", wrong_citation)
    failed_body = {**body, "operation_id": str(uuid4())}
    failure = client.post("/v1/repos/gh-101/questions", headers=agent, json=failed_body)
    assert failure.status_code == 503, failure.text
    retry = client.post("/v1/repos/gh-101/questions", headers=agent, json=failed_body)
    assert retry.status_code == 409, retry.text
    assert len(attempted) == 1
    changed = {**body, "question": "Changed intent"}
    assert client.post("/v1/repos/gh-101/questions", headers=agent, json=changed).status_code == 409
    denied = client.post(
        "/v1/repos/gh-101/teaching-runs",
        headers=agent,
        json={
            "operation_id": str(uuid4()),
            "source_commit": COMMIT,
            "files": ["worker.py"],
            "statement": TEXT,
            "expected_snapshot_id": None,
        },
    )
    assert denied.status_code == 403
    assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


@pytest.mark.parametrize("prompt_tokens,expected", [(100, 892), (800, None)])
def test_native_context_budget_counts_template_before_generation(prompt_tokens, expected):
    generations = []

    def respond(request):
        path = request.url.path
        if path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 1024}})
        if path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered fixture"})
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * prompt_tokens})
        generations.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"answer":"No.","revision_ids":["rev-1"]}'))

    model = Evon("http://localhost/v1", "secret", transport=httpx.MockTransport(respond))
    if expected is None:
        with pytest.raises(ProviderUnavailable):
            asyncio.run(model.structured("Read", {}, CitedAnswer))
        assert not generations
    else:
        asyncio.run(model.structured("Read", {}, CitedAnswer))
        assert generations[0]["max_tokens"] == expected


@pytest.mark.parametrize("defect", ["quote", "blob", "line", "path"])
def test_ungrounded_extraction_is_retained_as_uncertain_without_replay(engine, monkeypatch, defect):
    provider = SourceProvider()
    app = create_app(
        Settings(
            github_client_id="fixture",
            github_app_id=100,
            credential_encryption_key=Fernet.generate_key().decode(),
            evon_url="http://localhost/v1",
            evon_api_key="fixture",
        ),
        github=GitHub("fixture", 100, httpx.MockTransport(provider.request)),
        database=engine,
    )
    calls = []
    citation = {"path": "worker.py", "blob_sha": "b" * 40, "start_line": 1, "end_line": 2}
    quote = TEXT
    if defect == "quote":
        quote = "Invented human intent"
    if defect == "blob":
        citation["blob_sha"] = "c" * 40
    if defect == "line":
        citation["end_line"] = 99
    if defect == "path":
        citation["path"] = "unselected.py"

    async def structured(self, instruction, data, schema):
        from tacit_contracts.models import Usage

        calls.append(1)
        return CandidateSet.model_validate(
            {
                "candidates": [
                    {
                        "id": "fixture",
                        "statement": TEXT,
                        "transcript_quote": quote,
                        "citations": [citation],
                    }
                ]
            }
        ), Usage(reasoning_calls=1)

    monkeypatch.setattr(Evon, "structured", structured)
    with TestClient(app) as client:
        owner = login(client, provider, engine)
        body = {
            "operation_id": str(uuid4()),
            "source_commit": COMMIT,
            "files": ["worker.py"],
            "statement": TEXT,
            "expected_snapshot_id": None,
        }
        result = client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body)
        assert result.status_code == 503, result.text
        retry = client.post("/v1/repos/gh-101/teaching-runs", headers=owner, json=body)
        assert retry.status_code == 409, retry.text
        assert len(calls) == 1
        from sqlalchemy import select
        from tacit_api.db import TeachingRun

        with Session(engine) as session:
            run = session.scalar(
                select(TeachingRun).where(TeachingRun.operation_id == body["operation_id"])
            )
            assert run.status == "uncertain"
        assert client.get("/v1/repos/gh-101/head", headers=owner).json()["snapshot_id"] is None


def test_live_oversized_context_never_generates(live_backend):
    class ObservedTransport(httpx.AsyncHTTPTransport):
        def __init__(self):
            super().__init__()
            self.paths = []

        async def handle_async_request(self, request):
            self.paths.append(request.url.path)
            return await super().handle_async_request(request)

    transport = ObservedTransport()
    model = Evon(
        os.environ.get("TACIT_LIVE_EVON_URL", "http://127.0.0.1:8001/v1"),
        Path(os.environ["TACIT_LIVE_EVON_KEY_FILE"]).read_text().strip(),
        transport=transport,
    )
    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            model.structured("Answer from evidence", {"question": "worker " * 6000}, CitedAnswer)
        )
    assert "/props" in transport.paths and "/tokenize" in transport.paths
    assert "/v1/chat/completions" not in transport.paths
