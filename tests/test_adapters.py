import asyncio
import json

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


def test_cli_machine_output_and_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Arch PortAudio" in json.loads(result.stdout)["audio"]
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
