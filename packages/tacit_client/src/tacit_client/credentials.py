"""Credentials are scoped to the API origin; no plaintext storage fallback."""

import os
from urllib.parse import urlparse

import keyring
from keyring.errors import KeyringError


class CredentialError(RuntimeError):
    pass


def api_url() -> str:
    url = os.environ.get("TACIT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        parsed = urlparse(url)
        if not parsed.hostname or parsed.port == 0:
            raise ValueError
    except ValueError:
        raise CredentialError("API URL requires a valid hostname and port") from None
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise CredentialError("Credentials require HTTPS except on loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CredentialError("API URL must not contain credentials, query or fragment")
    return url


def secure_backend() -> None:
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if module not in {
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "keyring.backends.Windows",
        "keyring.backends.macOS",
    }:
        raise CredentialError("An OS secure keyring is required; plaintext storage is disabled")


def load_token(agent: bool) -> str:
    value = os.environ.get("TACIT_AGENT_TOKEN" if agent else "TACIT_TOKEN", "")
    if value or agent:
        return value
    try:
        secure_backend()
        return keyring.get_password("tacit:" + api_url(), "human") or ""
    except (KeyringError, CredentialError):
        return ""


def save_token(value: str) -> None:
    try:
        secure_backend()
        keyring.set_password("tacit:" + api_url(), "human", value)
    except KeyringError:
        raise CredentialError("Cannot save the credential in the OS keyring") from None


def delete_token() -> None:
    try:
        secure_backend()
        if keyring.get_password("tacit:" + api_url(), "human"):
            keyring.delete_password("tacit:" + api_url(), "human")
    except KeyringError:
        raise CredentialError("Cannot remove the credential from the OS keyring") from None
