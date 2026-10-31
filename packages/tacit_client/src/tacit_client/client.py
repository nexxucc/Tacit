from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx
from tacit_contracts.versions import (
    EVALUATOR_VERSION,
    PROTOCOL_VERSIONS,
    compatibility_message,
    protocol_headers,
)
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from tacit_client.credentials import CredentialError, api_url, load_token


class ClientError(RuntimeError):
    pass


class CompatibilityError(ClientError):
    """A declared wire version cannot be consumed safely by this client."""


def validate_response_versions(
    headers: Mapping[str, str], *, status_code: int | None = None
) -> None:
    """Accept legacy versionless servers; fail closed on declared incompatibility."""
    if status_code == 426:
        raise CompatibilityError(compatibility_message())
    declared = []
    incompatible = []
    for dimension, header, version in PROTOCOL_VERSIONS:
        try:
            value = headers.get(header)
        except LookupError:
            # WebSocket headers reject duplicate fields rather than joining them.
            raise CompatibilityError(compatibility_message((dimension,))) from None
        if value is not None:
            declared.append(dimension)
            if value != version:
                incompatible.append(dimension)
    if incompatible:
        raise CompatibilityError(compatibility_message(incompatible))
    if declared and len(declared) != len(PROTOCOL_VERSIONS):
        raise CompatibilityError(
            "The Tacit backend declared an incomplete compatibility header set. "
            "Upgrade the client/backend together and preserve all X-Tacit version headers "
            "through the proxy; the request was not automatically retried."
        )


class TacitClient:
    def __init__(self, *, agent: bool = False, transport: httpx.BaseTransport | None = None):
        token = load_token(agent)
        if not token:
            raise ClientError(
                "Set an explicit Tacit credential; agent mode requires TACIT_AGENT_TOKEN"
            )
        try:
            url = api_url()
        except CredentialError as exc:
            raise ClientError(str(exc)) from None
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", **protocol_headers()},
            timeout=15,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            if agent:
                identity = self.request("GET", "/v1/auth/me")
                if not isinstance(identity, dict) or identity.get("agent") is not True:
                    raise ClientError("Agent adapter refuses human credentials")
        except Exception:
            self.close()
            raise

    def request(self, method: str, path: str, **kwargs: object) -> object:
        # Callers construct paths, never accept arbitrary URLs from retrieved text.
        try:
            response = self.http.request(method, path, **kwargs)  # type: ignore[arg-type]
            validate_response_versions(response.headers, status_code=response.status_code)
            response.raise_for_status()
            payload = response.json()
            if (
                isinstance(payload, dict)
                and "evaluator_version" in payload
                and payload["evaluator_version"] != EVALUATOR_VERSION
            ):
                raise CompatibilityError(compatibility_message(("evaluator",)))
            return payload
        except httpx.HTTPStatusError as exc:
            try:
                payload = exc.response.json()
                code = (
                    payload.get("code", "request_failed")
                    if isinstance(payload, dict)
                    else "request_failed"
                )
                if (
                    not isinstance(code, str)
                    or not code.replace("_", "").isalnum()
                    or len(code) > 80
                ):
                    code = "request_failed"
            except ValueError:
                code = "request_failed"
            raise ClientError(f"API error: {code} ({exc.response.status_code})") from None
        except ValueError:
            raise ClientError("Invalid JSON response from the Tacit API") from None
        except httpx.HTTPError:
            raise ClientError("Cannot reach the Tacit API") from None

    def audio(self, path: str) -> bytes:
        try:
            with self.http.stream("GET", path) as response:
                validate_response_versions(response.headers, status_code=response.status_code)
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 28640044:
                        raise ClientError("Audio artifact exceeds the client budget")
                return bytes(data)
        except httpx.HTTPError:
            raise ClientError(
                "Audio artifact unavailable; check permission, retention and connectivity"
            ) from None

    @contextmanager
    def voice_socket(self, path: str, *, max_size: int = 65536) -> Iterator[Any]:
        url = str(self.http.base_url).rstrip("/")
        url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            with connect(
                url + path,
                additional_headers={
                    "Authorization": self.http.headers["Authorization"],
                    **protocol_headers(),
                },
                open_timeout=10,
                max_size=max_size,
                proxy=None,
            ) as socket:
                if socket.response is not None:
                    validate_response_versions(
                        socket.response.headers, status_code=socket.response.status_code
                    )
                yield socket
        except InvalidStatus as exc:
            validate_response_versions(exc.response.headers, status_code=exc.response.status_code)
            raise ClientError("Voice upload handshake rejected; local audio retained") from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise ClientError("Voice upload disconnected; local audio retained") from exc

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "TacitClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
