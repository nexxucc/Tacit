"""GitHub App user authentication; installation tokens never stand in for user access."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError
from tacit_domain.access import DomainError


def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected, signature)


class DeviceCode(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = Field(gt=0, le=3600)
    interval: int = Field(ge=1, le=60)


class Identity(BaseModel):
    id: int
    login: str


class Owner(Identity):
    type: str


class GitHubInstallation(BaseModel):
    id: int
    app_id: int
    account: Owner
    suspended_at: str | None = None


class GitHubRepository(BaseModel):
    id: int
    full_name: str
    owner: Owner


@dataclass(frozen=True)
class EligibleInstallation:
    installation: GitHubInstallation
    repositories: tuple[GitHubRepository, ...]
    member: bool
    owner: bool


class GitHub:
    def __init__(self, client_id: str, app_id: int, transport: httpx.BaseTransport | None = None):
        self.client_id, self.app_id, self.transport = client_id, app_id, transport

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        base = "https://api.github.com" if path.startswith("/") else ""
        headers = {
            "Accept": "application/vnd.github+json" if base else "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(
                timeout=15, transport=self.transport, follow_redirects=False, trust_env=False
            ) as client:
                response = client.request(method, base + path, headers=headers, data=data)
        except httpx.HTTPError:
            raise DomainError("provider_unavailable", "GitHub is unreachable", 503) from None
        if response.status_code == 401:
            raise DomainError("unauthenticated", "GitHub authorization expired; sign in again", 401)
        if response.status_code not in {200, 201, 404}:
            raise DomainError("provider_unavailable", "GitHub access could not be verified", 503)
        return response

    def oauth(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        response = self.request("POST", "https://github.com" + path, data=data)
        try:
            result = response.json()
        except ValueError:
            raise DomainError("provider_unavailable", "Invalid GitHub response", 503) from None
        if not isinstance(result, dict):
            raise DomainError("provider_unavailable", "Invalid GitHub response", 503)
        return result

    def start(self) -> DeviceCode:
        try:
            return DeviceCode.model_validate(
                self.oauth("/login/device/code", {"client_id": self.client_id})
            )
        except ValidationError:
            raise DomainError(
                "provider_unavailable", "GitHub device flow is unavailable", 503
            ) from None

    def poll(self, device_code: str) -> dict[str, Any]:
        return self.oauth(
            "/login/oauth/access_token",
            {
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )

    def pages(self, path: str, token: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        # Construct fixed-origin page URLs ourselves; never follow provider-supplied URLs.
        for page in range(1, 101):
            response = self.request("GET", f"{path}?per_page=100&page={page}", token=token)
            if response.status_code == 404:
                return []
            try:
                batch = response.json()[key]
            except (ValueError, KeyError, TypeError):
                raise DomainError(
                    "provider_unavailable", "Invalid GitHub access response", 503
                ) from None
            if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
                raise DomainError("provider_unavailable", "Invalid GitHub access response", 503)
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise DomainError("provider_unavailable", "GitHub access pagination limit exceeded", 503)

    def identity(self, token: str) -> Identity:
        try:
            return Identity.model_validate(self.request("GET", "/user", token=token).json())
        except (ValidationError, ValueError):
            raise DomainError(
                "provider_unavailable", "Invalid GitHub identity response", 503
            ) from None

    def eligibility(self, token: str) -> tuple[EligibleInstallation, ...]:
        result = []
        try:
            for item in self.pages("/user/installations", token, "installations"):
                install = GitHubInstallation.model_validate(item)
                if install.app_id != self.app_id or install.suspended_at:
                    continue
                repositories = tuple(
                    GitHubRepository.model_validate(r)
                    for r in self.pages(
                        f"/user/installations/{install.id}/repositories", token, "repositories"
                    )
                )
                member = owner = False
                if install.account.type == "Organization":
                    response = self.request(
                        "GET", f"/user/memberships/orgs/{install.account.login}", token=token
                    )
                    if response.status_code == 200:
                        membership = response.json()
                        member = membership.get("state") == "active"
                        owner = member and membership.get("role") == "admin"
                result.append(EligibleInstallation(install, repositories, member, owner))
        except (ValidationError, ValueError, AttributeError):
            raise DomainError(
                "provider_unavailable", "Invalid GitHub access response", 503
            ) from None
        return tuple(result)
