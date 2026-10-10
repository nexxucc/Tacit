import hashlib
import hmac

from fastapi.testclient import TestClient
from tacit_api.app import Settings, create_app


def client(token: str = "demo-agent") -> TestClient:
    return TestClient(create_app(Settings(demo=True)), headers={"Authorization": f"Bearer {token}"})


def test_default_fails_closed() -> None:
    with TestClient(create_app(Settings(demo=False))) as c:
        assert c.get("/health/live").status_code == 200
        assert c.get("/health/ready").status_code == 503
        assert (
            c.get("/v1/workspaces", headers={"Authorization": "Bearer demo-agent"}).status_code
            == 401
        )


def test_text_read_and_evaluation_contract() -> None:
    with client() as c:
        data = {"snapshot_id": "snapshot-1", "source_commit": "demo-commit-1"}
        context = c.post("/v1/repos/scheduler/context", json=data)
        assert context.status_code == 200
        assert context.json()["evidence"][0]["segment_id"] == "segment-1"
        assert context.json()["usage"] == {"stt_calls": 0, "tts_calls": 0, "reasoning_calls": 0}
        result = c.post(
            "/v1/repos/scheduler/evaluate", json={**data, "facts": {"lease_expired": True}}
        )
        assert result.json()["decision"] == "forbidden"
        answer = c.post("/v1/repos/scheduler/questions", json={**data, "question": "Why?"}).json()
        assert answer["answer_kind"] == "confirmed_statements"
        assert answer["warnings"]


def test_write_denial_precedes_unimplemented_path() -> None:
    for token in ("demo-agent", "demo-learner"):
        with client(token) as c:
            for route in ("publications", "teaching-sessions"):
                assert c.post(f"/v1/repos/scheduler/{route}").status_code == 403
    with client("demo-maintainer") as c:
        assert c.post("/v1/repos/scheduler/publications").status_code == 501


def test_validation_errors_are_safe_and_correlated() -> None:
    with client() as c:
        response = c.post("/v1/repos/scheduler/context", json={"secret": "do-not-reflect"})
        assert response.status_code == 422
        assert "do-not-reflect" not in response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_webhook_not_acknowledged_without_durable_processing() -> None:
    secret, body = "fixture-secret", b'{"action":"created"}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with TestClient(create_app(Settings(github_webhook_secret=secret))) as c:
        assert c.post("/v1/github/webhooks", content=body).status_code == 401
        assert (
            c.post(
                "/v1/github/webhooks", content=body, headers={"X-Hub-Signature-256": signature}
            ).status_code
            == 501
        )


def test_demo_rejects_remote_clients() -> None:
    with TestClient(create_app(Settings(demo=True)), client=("192.0.2.1", 5000)) as c:
        assert (
            c.get("/v1/workspaces", headers={"Authorization": "Bearer demo-agent"}).status_code
            == 403
        )
