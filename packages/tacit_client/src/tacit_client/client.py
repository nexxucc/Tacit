from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from tacit_client.credentials import CredentialError, api_url, load_token


class ClientError(RuntimeError):
    pass


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
            headers={"Authorization": f"Bearer {token}"},
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
            response.raise_for_status()
            return response.json()
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

    @contextmanager
    def voice_socket(self, path: str) -> Iterator[Any]:
        url = str(self.http.base_url).rstrip("/")
        url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            with connect(
                url + path,
                additional_headers={"Authorization": self.http.headers["Authorization"]},
                open_timeout=10,
                max_size=65536,
                proxy=None,
            ) as socket:
                yield socket
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise ClientError("Voice upload disconnected; local audio retained") from exc

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "TacitClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
