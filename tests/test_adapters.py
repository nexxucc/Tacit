import asyncio
import json
import platform

import httpx
import pytest
from tacit_cli.main import app
from tacit_client.client import ClientError, TacitClient
from tacit_mcp.server import create_server
from typer.testing import CliRunner


def test_agent_credential_cannot_fall_back_to_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TACIT_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("TACIT_TOKEN", "human")
    with pytest.raises(ClientError, match="TACIT_AGENT_TOKEN"):
        TacitClient(agent=True)


def test_agent_refuses_human_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TACIT_AGENT_TOKEN", "human")
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    with pytest.raises(ClientError, match="refuses human"):
        TacitClient(
            agent=True,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"agent": False})),
        )


def test_non_loopback_plaintext_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TACIT_TOKEN", "fixture")
    monkeypatch.setenv("TACIT_API_URL", "http://example.com")
    with pytest.raises(ClientError, match="HTTPS"):
        TacitClient()


def test_mcp_read_surface_only() -> None:
    tools = asyncio.run(create_server().list_tools())
    assert {t.name for t in tools} == {
        "list_workspaces",
        "list_repositories",
        "get_context",
        "ask_repository",
        "evaluate_scenario",
        "get_decision_history",
    }
    assert all(t.annotations.readOnlyHint for t in tools)
    assert all(t.annotations.destructiveHint is False for t in tools)


def test_cli_machine_output_and_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    diagnostic = json.loads(result.stdout)
    assert diagnostic["platform"] == platform.system()
    assert "PortAudio" in diagnostic["audio"]
    assert diagnostic["config"] and diagnostic["voice_spool"]
    monkeypatch.delenv("TACIT_AGENT_TOKEN", raising=False)
    result = CliRunner().invoke(app, ["workspaces", "list", "--agent"])
    assert result.exit_code == 1 and not result.stdout
    assert "TACIT_AGENT_TOKEN" in result.stderr


@pytest.mark.parametrize(
    "status,payload", [(200, b"not json"), (502, b"[]"), (400, b'{"code": {"private": "secret"}}')]
)
def test_client_handles_malformed_responses_without_reflecting_payload(
    monkeypatch, status, payload
):
    monkeypatch.setenv("TACIT_TOKEN", "fixture")
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    with TacitClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=payload))
    ) as client:
        with pytest.raises(ClientError) as error:
            client.request("GET", "/v1/auth/me")
        assert "secret" not in str(error.value)


@pytest.mark.parametrize("reason", [False, True])
@pytest.mark.parametrize("agent", [False, True])
def test_ask_timeout_covers_reasoning_and_preserves_operation_id(monkeypatch, reason, agent):
    import tacit_cli.main as cli

    monkeypatch.setenv("TACIT_TOKEN", "fixture-human")
    monkeypatch.setenv("TACIT_AGENT_TOKEN", "fixture-agent")
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    observed = []

    def respond(request):
        if request.url.path == "/v1/auth/me":
            return httpx.Response(200, json={"agent": True})
        body = json.loads(request.content)
        timeout = request.extensions["timeout"]["read"]
        observed.append((body, timeout))
        # Simulate a response taking 20 seconds without a slow wall-clock test.
        if body["reason"] and timeout < 20:
            raise httpx.ReadTimeout("Simulated model latency", request=request)
        return httpx.Response(200, json={"answer": "Fixture answer"})

    monkeypatch.setattr(
        cli,
        "TacitClient",
        lambda **kwargs: TacitClient(**kwargs, transport=httpx.MockTransport(respond)),
    )
    args = [
        "ask",
        "May workers publish?",
        "--repo",
        "gh-101",
        "--snapshot",
        "fixture",
        "--commit",
        "a" * 40,
    ]
    if agent:
        args += ["--agent"]
    if reason:
        args += ["--reason", "--operation-id", "stable-question-operation"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["answer"] == "Fixture answer"
    assert observed[0][1] == (150 if reason else 15)
    assert observed[0][0]["operation_id"] == ("stable-question-operation" if reason else None)


@pytest.mark.parametrize("payload", ["[]", "null", '"not an object"', "12"])
def test_evaluate_rejects_non_object_json_before_api(tmp_path, monkeypatch, payload):
    import tacit_cli.main as cli

    scenario = tmp_path / "scenario.json"
    scenario.write_text(payload)
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: pytest.fail("Must not call API"))
    result = CliRunner().invoke(
        cli.app,
        [
            "evaluate",
            "--scenario",
            str(scenario),
            "--repo",
            "fixture",
            "--snapshot",
            "s",
            "--commit",
            "a" * 40,
        ],
    )
    assert result.exit_code == 2
    assert "Scenario must be a JSON object" in result.stderr
