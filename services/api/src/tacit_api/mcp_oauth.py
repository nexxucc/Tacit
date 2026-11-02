"""Opt-in MCP OAuth: explicit repository consent, PKCE, and revocable delegation.

The SDK owns wire-protocol OAuth validation. This provider owns persistent,
atomic grants and audience isolation. No refresh tokens or dynamic registration.
"""

import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from tacit_domain.access import DomainError, authorize

from tacit_api.access import AccessService, digest, now, valid_time
from tacit_api.db import Credential, MCPGrant, Repository

SCOPE = "tacit:read"


def safe_url(value: str, *, origin: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        not value.isascii()
        or "\\" in value
        or len(value) > (512 if origin else 2048)
        or any(ord(c) <= 32 for c in value)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.hostname
        or (origin and (parsed.path not in {"", "/"} or parsed.query))
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            )
        )
    ):
        raise ValueError(
            "MCP URLs require HTTPS (HTTP allowed only on loopback), without credentials or fragments"
        )
    # Accessing port validates malformed/out-of-range ports, too.
    _ = parsed.port
    canonical = str(AnyHttpUrl(value))
    return canonical.rstrip("/") if origin else canonical


class RegisteredClient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)


class RemoteAccessToken(AccessToken):
    delegated_token: str = Field(repr=False, exclude=True)


class MCPAuth(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, RemoteAccessToken]):
    def __init__(self, access: AccessService, issuer: str, clients: dict[str, RegisteredClient]):
        self.access = access
        self.issuer = safe_url(issuer, origin=True)
        self.resource = self.issuer + "/mcp"
        if not clients or len(clients) > 100:
            raise ValueError("Configure between one and 100 MCP clients")
        self.clients = {}
        for cid, value in clients.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid):
                raise ValueError("Invalid registered MCP client ID")
            redirects = [safe_url(uri) for uri in value.redirect_uris]
            self.clients[cid] = OAuthClientInformationFull(
                client_id=cid,
                client_name=value.name,
                redirect_uris=redirects,  # type: ignore[arg-type]
                grant_types=["authorization_code"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=SCOPE,
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Clients are registered by the deployment operator")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "The exact MCP resource is required")
        if params.scopes not in (None, [SCOPE]):
            raise AuthorizeError("invalid_scope", "Only tacit:read is supported")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "An S256 PKCE challenge is required")
        if params.state is not None and len(params.state) > 1024:
            raise AuthorizeError("invalid_request", "State exceeds the request budget")
        ticket = secrets.token_urlsafe(32)

        def save() -> None:
            with Session(self.access.engine) as db, db.begin():
                # Only expired OAuth state is removed. Credential lifecycle belongs to AccessService.
                db.execute(delete(MCPGrant).where(MCPGrant.expires_at <= now()))
                pending = db.scalar(
                    select(func.count())
                    .select_from(MCPGrant)
                    .where(
                        MCPGrant.issuer == self.issuer,
                        MCPGrant.client_id == client.client_id,
                        MCPGrant.status == "pending",
                    )
                )
                if pending is not None and pending >= 100:
                    raise AuthorizeError(
                        "temporarily_unavailable", "Too many pending authorizations; try later"
                    )
                db.add(
                    MCPGrant(
                        id=digest(ticket),
                        issuer=self.issuer,
                        client_id=client.client_id,
                        params=params.model_dump(mode="json"),
                        status="pending",
                        expires_at=now() + timedelta(minutes=10),
                    )
                )

        await run_in_threadpool(save)
        return self.issuer + "/oauth/consent?" + urlencode({"request": ticket})

    def flow(self, db: Session, ticket: str) -> MCPGrant:
        row = db.get(MCPGrant, digest(ticket))
        if (
            row is None
            or row.issuer != self.issuer
            or row.client_id not in self.clients
            or valid_time(row.expires_at) <= now()
            or row.status != "pending"
        ):
            raise DomainError("invalid_grant", "Authorization request expired or consumed", 400)
        return row

    def browser(self, ticket: str, cookie: str | None) -> tuple[str, str]:
        """Bind the consent form to one browser without overwriting a previous binding."""
        with Session(self.access.engine) as db, db.begin():
            row = self.flow(db, ticket)
            if row.cookie_hash is None:
                cookie = secrets.token_urlsafe(32)
                changed = db.execute(
                    update(MCPGrant)
                    .execution_options(synchronize_session="fetch")
                    .where(
                        MCPGrant.id == row.id,
                        MCPGrant.cookie_hash.is_(None),
                        MCPGrant.status == "pending",
                    )
                    .values(cookie_hash=digest(cookie))
                )
                if changed.rowcount != 1:  # type: ignore[attr-defined]
                    raise DomainError(
                        "invalid_grant", "Consent already opened in another browser", 400
                    )
            elif not cookie or not secrets.compare_digest(row.cookie_hash, digest(cookie)):
                raise DomainError("invalid_grant", "Consent belongs to another browser", 400)
            client = self.clients[row.client_id]
            return cookie, client.client_name or row.client_id

    def approve(self, ticket: str, cookie: str, human_token: str, repos: list[str]) -> str:
        if not repos or len(repos) > 100 or len(set(repos)) != len(repos):
            raise DomainError(
                "invalid_scope", "Select between one and 100 distinct repositories", 400
            )
        p = self.access.principal(human_token, fresh=True)
        if p.agent:
            raise DomainError("forbidden", "Human consent is required")
        with Session(self.access.engine) as db, db.begin():
            row = self.flow(db, ticket)
            if not row.cookie_hash or not secrets.compare_digest(row.cookie_hash, digest(cookie)):
                raise DomainError("invalid_grant", "Invalid consent browser", 400)
            parent = self.access.credential(db, digest(human_token))
            if parent.agent or parent.user_id != p.user_id:
                raise DomainError("forbidden", "Human consent is required")
            workspaces: set[str] = set()
            for rid in repos:
                repo = db.scalar(
                    select(Repository).where(Repository.id == rid, Repository.enabled.is_(True))
                )
                if repo is None:
                    raise DomainError("not_found", "Repository unavailable", 404)
                authorize(p, repo.workspace_id, rid)
                workspaces.add(repo.workspace_id)
            delegated, code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expiry = min(valid_time(parent.expires_at), now() + timedelta(hours=1))
            # Compare-and-set works on PostgreSQL and SQLite, including concurrent approvals.
            changed = db.execute(
                update(MCPGrant)
                .execution_options(synchronize_session="fetch")
                .where(
                    MCPGrant.id == row.id,
                    MCPGrant.status == "pending",
                    MCPGrant.expires_at > now(),
                )
                .values(
                    status="code",
                    code_hash=digest(code),
                    expires_at=min(expiry, now() + timedelta(minutes=2)),
                )
            )
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise DomainError("invalid_grant", "Consent already consumed", 400)
            db.add(
                Credential(
                    token_hash=digest(delegated),
                    user_id=p.user_id,
                    agent=True,
                    parent_hash=parent.token_hash,
                    repository_ids=sorted(repos),
                    expires_at=expiry,
                    revoked=False,
                )
            )
            db.flush()
            row.delegated_token = self.access.encrypt(delegated)
            row.credential_hash = digest(delegated)
            params = AuthorizationParams.model_validate(row.params)
            for wid in workspaces:
                self.access.audit(
                    db,
                    p.user_id,
                    wid,
                    "mcp.consent",
                    {"client_id": row.client_id, "repository_ids": sorted(repos)},
                )
            # SDK helper preserves an existing query string and encodes state/code safely.
            from mcp.server.auth.provider import construct_redirect_uri

            return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, ticket: str, cookie: str) -> str:
        from mcp.server.auth.provider import construct_redirect_uri

        with Session(self.access.engine) as db, db.begin():
            row = self.flow(db, ticket)
            if not row.cookie_hash or not secrets.compare_digest(row.cookie_hash, digest(cookie)):
                raise DomainError("invalid_grant", "Invalid consent browser", 400)
            params = AuthorizationParams.model_validate(row.params)
            changed = db.execute(
                update(MCPGrant)
                .execution_options(synchronize_session="fetch")
                .where(
                    MCPGrant.id == row.id, MCPGrant.status == "pending", MCPGrant.expires_at > now()
                )
                .values(status="denied", expires_at=now())
            )
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise DomainError("invalid_grant", "Consent already consumed", 400)
            return construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def load() -> AuthorizationCode | None:
            with Session(self.access.engine) as db:
                row = db.scalar(
                    select(MCPGrant).where(MCPGrant.code_hash == digest(authorization_code))
                )
                if (
                    row is None
                    or row.issuer != self.issuer
                    or row.client_id != client.client_id
                    or row.status != "code"
                    or valid_time(row.expires_at) <= now()
                ):
                    return None
                params = AuthorizationParams.model_validate(row.params)
                return AuthorizationCode(
                    code=authorization_code,
                    client_id=row.client_id,
                    scopes=[SCOPE],
                    expires_at=valid_time(row.expires_at).timestamp(),
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource,
                )

        return await run_in_threadpool(load)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        def exchange() -> OAuthToken:
            with Session(self.access.engine) as db, db.begin():
                row = db.scalar(
                    select(MCPGrant).where(MCPGrant.code_hash == digest(authorization_code.code))
                )
                if (
                    row is None
                    or row.client_id != client.client_id
                    or row.issuer != self.issuer
                    or row.status != "code"
                    or valid_time(row.expires_at) <= now()
                    or not row.credential_hash
                ):
                    raise TokenError("invalid_grant", "Code expired or consumed")
                credential = self.access.credential(db, row.credential_hash)
                if not credential.parent_hash:
                    raise TokenError("invalid_grant", "Invalid delegation")
                self.access.credential(db, credential.parent_hash)
                token = secrets.token_urlsafe(32)
                expiry = valid_time(credential.expires_at)
                changed = db.execute(
                    update(MCPGrant)
                    .execution_options(synchronize_session="fetch")
                    .where(
                        MCPGrant.id == row.id,
                        MCPGrant.status == "code",
                        MCPGrant.expires_at > now(),
                    )
                    .values(
                        status="token", token_hash=digest(token), code_hash=None, expires_at=expiry
                    )
                )
                if changed.rowcount != 1:  # type: ignore[attr-defined]
                    raise TokenError("invalid_grant", "Code already consumed")
                return OAuthToken(
                    access_token=token,
                    expires_in=max(0, int((expiry - now()).total_seconds())),
                    scope=SCOPE,
                )

        try:
            return await run_in_threadpool(exchange)
        except DomainError:
            raise TokenError("invalid_grant", "Delegation no longer valid") from None

    async def load_access_token(self, token: str) -> RemoteAccessToken | None:
        def load() -> RemoteAccessToken | None:
            with Session(self.access.engine) as db:
                row = db.scalar(select(MCPGrant).where(MCPGrant.token_hash == digest(token)))
                if (
                    row is None
                    or row.issuer != self.issuer
                    or row.client_id not in self.clients
                    or row.status != "token"
                    or valid_time(row.expires_at) <= now()
                    or not row.delegated_token
                ):
                    return None
                delegated = self.access.decrypt(row.delegated_token)
                expiry, cid = int(valid_time(row.expires_at).timestamp()), row.client_id
            p = self.access.principal(delegated, fresh=True)
            if not p.agent or not p.github_repositories:
                return None
            return RemoteAccessToken(
                token=token,
                client_id=cid,
                scopes=[SCOPE],
                resource=self.resource,
                expires_at=expiry,
                subject=p.user_id,
                delegated_token=delegated,
            )

        try:
            return await run_in_threadpool(load)
        except DomainError:
            return None

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Reauthorize after token expiry")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def revoke() -> None:
            with Session(self.access.engine) as db, db.begin():
                row = db.scalar(
                    select(MCPGrant).where(
                        MCPGrant.token_hash == digest(token.token),
                        MCPGrant.issuer == self.issuer,
                        MCPGrant.client_id == token.client_id,
                    )
                )
                if row is not None:
                    row.status = "revoked"
                    row.delegated_token = None
                    if row.credential_hash:
                        credential = db.get(Credential, row.credential_hash)
                        if credential:
                            credential.revoked = True

        await run_in_threadpool(revoke)
