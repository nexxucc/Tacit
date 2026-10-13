import keyring
import pytest
from keyring.backend import KeyringBackend
from tacit_client.credentials import CredentialError, api_url, load_token, save_token


class PlaintextBackend(KeyringBackend):
    priority = 1

    def get_password(self, service, username):
        raise AssertionError("Must reject plaintext backend before reading")

    def set_password(self, service, username, password):
        raise AssertionError("Must reject plaintext backend before writing")

    def delete_password(self, service, username):
        pass


def test_rejects_untrusted_keyring_and_does_not_fall_back_for_agents(monkeypatch):
    monkeypatch.setattr(keyring, "get_keyring", lambda: PlaintextBackend())
    monkeypatch.delenv("TACIT_TOKEN", raising=False)
    monkeypatch.delenv("TACIT_AGENT_TOKEN", raising=False)
    with pytest.raises(CredentialError, match="plaintext"):
        save_token("secret")
    assert load_token(False) == ""
    monkeypatch.setenv("TACIT_TOKEN", "human")
    assert load_token(True) == ""


def test_keyring_identity_is_scoped_to_api_url(monkeypatch):
    monkeypatch.setenv("TACIT_API_URL", "https://tacit.example/api/")
    assert api_url() == "https://tacit.example/api"
    monkeypatch.setenv("TACIT_API_URL", "http://tacit.example")
    with pytest.raises(CredentialError, match="HTTPS"):
        api_url()


def test_cli_login_stores_tacit_token_without_printing_secrets(monkeypatch):
    import importlib

    import httpx
    from typer.testing import CliRunner

    cli = importlib.import_module("tacit_cli.main")
    saved = []
    original_client = httpx.Client
    calls = []

    def response(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/auth/device/start":
            return httpx.Response(
                200,
                json={
                    "user_code": "ABCD-EFGH",
                    "pairing_token": "pairing-secret",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        return httpx.Response(200, json={"status": "authorized", "token": "tacit-secret"})

    monkeypatch.setattr(cli, "secure_backend", lambda: None)
    monkeypatch.setattr(cli, "save_token", saved.append)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(response)),
    )
    monkeypatch.setenv("TACIT_API_URL", "http://127.0.0.1:8000")
    result = CliRunner().invoke(cli.app, ["login"])
    assert result.exit_code == 0, result.output
    assert saved == ["tacit-secret"]
    assert "ABCD-EFGH" in result.stdout
    assert "tacit-secret" not in result.output and "pairing-secret" not in result.output
    assert calls == ["/v1/auth/device/start", "/v1/auth/device/poll"]


def test_keyring_origin_is_used_for_storage_and_lookup(monkeypatch):
    from tacit_client.credentials import delete_token

    class OSBackend(PlaintextBackend):
        __module__ = "keyring.backends.SecretService"

    store = {}
    monkeypatch.setattr(keyring, "get_keyring", lambda: OSBackend())
    monkeypatch.setattr(
        keyring, "set_password", lambda service, username, value: store.update({service: value})
    )
    monkeypatch.setattr(keyring, "get_password", lambda service, username: store.get(service))
    monkeypatch.setattr(keyring, "delete_password", lambda service, username: store.pop(service))
    monkeypatch.delenv("TACIT_TOKEN", raising=False)
    monkeypatch.setenv("TACIT_API_URL", "https://one.example")
    save_token("secret")
    assert load_token(False) == "secret"
    monkeypatch.setenv("TACIT_API_URL", "https://two.example")
    assert load_token(False) == ""
    monkeypatch.setenv("TACIT_API_URL", "https://one.example")
    delete_token()
    assert load_token(False) == ""


@pytest.mark.parametrize(
    "url", ["https://", "https://host:bad", "https://host:99999", "http://[broken"]
)
def test_invalid_api_address_is_a_safe_configuration_error(monkeypatch, url):
    monkeypatch.setenv("TACIT_API_URL", url)
    with pytest.raises(CredentialError):
        api_url()
