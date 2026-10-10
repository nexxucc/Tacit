import os
from urllib.parse import urlparse

import httpx


class ClientError(RuntimeError):
    pass


class TacitClient:
    def __init__(self, *, agent: bool = False, transport: httpx.BaseTransport | None = None):
        token = os.environ.get("TACIT_AGENT_TOKEN" if agent else "TACIT_TOKEN", "")
        if not token:
            raise ClientError(
                "Set an explicit Tacit credential; agent mode requires TACIT_AGENT_TOKEN"
            )
        url = os.environ.get("TACIT_API_URL", "http://127.0.0.1:8000")
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ClientError("Credentials require HTTPS except on loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ClientError("API URL must not contain credentials, query or fragment")
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
                code = exc.response.json().get("code", "request_failed")
            except ValueError:
                code = "request_failed"
            raise ClientError(f"API error: {code} ({exc.response.status_code})") from None
        except httpx.HTTPError:
            raise ClientError("Cannot reach the Tacit API") from None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "TacitClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
