import httpx

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
