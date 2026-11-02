"""Streamable HTTP bridge to the same six tools, using per-request OAuth context."""

import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.routing import Route
from tacit_contracts.versions import protocol_headers
from tacit_domain.access import DomainError
from tacit_mcp.server import create_server

from tacit_api.mcp_oauth import SCOPE, MCPAuth, RemoteAccessToken


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=40, max_length=64)


class Consent(ConsentRequest):
    repository_ids: list[str] = Field(min_length=1, max_length=100)


class RemoteBoundary:
    """Validate Host/Origin for discovery and OAuth as well as MCP; bind token resource."""

    def __init__(self, app: Any, provider: MCPAuth, origins: list[str]):
        self.app, self.provider = app, provider
        self.host = urlsplit(provider.issuer).netloc
        self.origins = {provider.issuer, *origins}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        headers = request.headers
        if (
            headers.getlist("host") != [self.host]
            or len(headers.getlist("authorization")) > 1
            or len(headers.getlist("origin")) > 1
            or (headers.get("origin") is not None and headers["origin"] not in self.origins)
        ):
            await JSONResponse({"error": "invalid_request"}, status_code=403)(scope, receive, send)
            return
        if scope["path"] == "/token" and scope["method"] == "POST":
            body = await request.body()
            # OAuth SDK currently validates PKCE but does not enforce token-request resource.
            # Reject duplicates/missing/wrong resource here, before consuming the code.
            try:
                values = parse_qs(body.decode("ascii"), keep_blank_values=True, max_num_fields=20)
                valid = (
                    len(body) <= 16384
                    and all(len(v) == 1 for v in values.values())
                    and values.get("resource") == [self.provider.resource]
                )
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                await JSONResponse(
                    {"error": "invalid_target"},
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            consumed = False

            async def replay() -> Any:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
            return
        await self.app(scope, receive, send)


# No localStorage/cookies contain the human credential. It exists only in this page's memory.
# Text is rendered with textContent, never provider-controlled HTML.
CONSENT_JS = r"""
const request = new URL(location.href).searchParams.get('request');
let token = null, cancelled = false;
const status = document.getElementById('status'), choices = document.getElementById('repos');
async function call(path, body) {
  const headers = {'Content-Type':'application/json'};
  if (token) headers.Authorization = 'Bearer ' + token;
  const r = await fetch(path, {method:body ? 'POST':'GET', headers,
    body:body ? JSON.stringify(body):undefined, credentials:'same-origin', cache:'no-store'});
  if (!r.ok) throw new Error('Request failed (' + r.status + '). Start authorization again if expired.');
  return await r.json();
}
document.getElementById('signin').onclick = async function() {
  this.disabled = true;
  try {
    const start = await call('/v1/auth/device/start', {});
    // GitHub's fixed verification page is independent of any returned provider URL.
    status.textContent = 'Open github.com/login/device and enter: ' + start.user_code;
    const deadline = Date.now() + Math.min(start.expires_in, 600)*1000;
    while (Date.now() < deadline && !cancelled) {
      await new Promise(resolve => setTimeout(resolve, Math.max(start.interval, 1)*1000));
      const result = await call('/v1/auth/device/poll', {pairing_token:start.pairing_token});
      if (cancelled) {
        if (result.token) { token=result.token; try { await call('/v1/auth/logout', {}); } catch(e) {} token=null; }
        return;
      }
      if (result.status === 'denied') throw new Error('GitHub denied authorization.');
      if (result.status !== 'authorized') { if (result.interval) start.interval=result.interval; continue; }
      token = result.token;
      const me = await call('/v1/auth/me');
      if (me.agent) throw new Error('A human account must approve this connection.');
      status.textContent = 'Signed in as ' + me.user_id + '. Select repositories for this client.';
      const workspaces = await call('/v1/workspaces');
      for (const w of workspaces) {
        const repos = await call('/v1/workspaces/' + encodeURIComponent(w.id) + '/repos');
        for (const repo of repos) {
          const label = document.createElement('label'), input = document.createElement('input');
          input.type='checkbox'; input.value=repo.id; label.append(input, document.createTextNode(repo.full_name));
          choices.append(label, document.createElement('br'));
        }
      }
      document.getElementById('approve').disabled=false;
      return;
    }
    throw new Error('Sign-in expired. Restart authorization.');
  } catch(e) { status.textContent=e.message; }
};
document.getElementById('approve').onclick = async function() {
  this.disabled=true;
  document.getElementById('deny').disabled=true;
  try {
    const repository_ids = [...choices.querySelectorAll('input:checked')].map(x=>x.value);
    const result = await call('/oauth/consent', {request, repository_ids});
    token=null;
    location.assign(result.redirect);
  } catch(e) { status.textContent=e.message; this.disabled=false; document.getElementById('deny').disabled=false; }
};
document.getElementById('deny').onclick = async function() {
  cancelled=true;
  if (token) { try { await call('/v1/auth/logout', {}); } catch(e) {} }
  token=null; status.textContent='Cancelled. No client access was granted.';
  try { const result=await call('/oauth/consent/deny', {request}); location.assign(result.redirect); }
  catch(e) { status.textContent=e.message; }
  document.getElementById('approve').disabled=true;
  document.getElementById('signin').disabled=true;
};
"""


def install_remote(app: FastAPI, provider: MCPAuth, origins: list[str]) -> FastMCP:
    async def bridge(method: str, path: str, body: object) -> object:
        token = get_access_token()
        if not isinstance(token, RemoteAccessToken):
            raise DomainError("unauthenticated", "MCP OAuth credential required", 401)
        # No process environment mutation, keyring fallback, public HTTP hop or OAuth-token passthrough.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://tacit.internal",
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                path,
                headers={"Authorization": "Bearer " + token.delegated_token, **protocol_headers()},
                json=body,
            )
        if not response.is_success:
            # Don't relay provider response bodies/credentials or tracebacks to MCP clients.
            raise ValueError(f"Tacit tool request rejected ({response.status_code})")
        result: object = response.json()
        return result

    server = create_server(
        bridge,
        stateless_http=True,
        json_response=True,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(provider.issuer),
            resource_server_url=AnyHttpUrl(provider.resource),
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=False, valid_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlsplit(provider.issuer).netloc],
            allowed_origins=[provider.issuer, *origins],
        ),
    )

    @app.get("/oauth/consent", response_class=HTMLResponse)
    def consent_page(request: Request) -> Response:
        if request.headers.get("host") != urlsplit(provider.issuer).netloc:
            raise DomainError("invalid_request", "Invalid consent host", 400)
        ticket = request.query_params.get("request", "")
        if len(ticket) > 64 or len(ticket) < 40:
            raise DomainError("invalid_request", "Invalid authorization request", 400)
        cookie_name = "tacit_mcp_" + ticket[:12]
        cookie, name = provider.browser(ticket, request.cookies.get(cookie_name))
        import html

        nonce = secrets.token_urlsafe(20)
        response = HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8"><title>Connect Tacit</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<h1>Connect " + html.escape(name) + "</h1>"
            "<p>This client can read only the repositories you select. Access expires within one hour. "
            "It cannot teach or publish knowledge. Optional reasoning tools may consume model resources.</p>"
            '<a href="https://github.com/login/device" target="_blank" rel="noopener noreferrer">GitHub device sign-in</a>'
            '<p><button id="signin">Sign in with GitHub</button></p>'
            '<p id="status">Sign in, then select repositories.</p><div id="repos"></div>'
            '<button id="approve" disabled>Allow selected repositories</button> '
            '<button id="deny">Cancel</button>'
            f'<script nonce="{nonce}">' + CONSENT_JS + "</script></html>",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            },
        )
        response.set_cookie(
            cookie_name,
            cookie,
            max_age=600,
            httponly=True,
            secure=provider.issuer.startswith("https:"),
            samesite="strict",
            path="/oauth/consent",
        )
        return response

    @app.post("/oauth/consent")
    async def approve(request: Request, body: Consent) -> JSONResponse:
        if (
            request.headers.getlist("origin") != [provider.issuer]
            or request.headers.get("host") != urlsplit(provider.issuer).netloc
        ):
            raise DomainError("invalid_request", "Consent requires a same-origin browser", 400)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise DomainError("unauthenticated", "Human sign-in required", 401)
        cookie_name = "tacit_mcp_" + body.request[:12]
        redirect = await run_in_threadpool(
            provider.approve,
            body.request,
            request.cookies.get(cookie_name, ""),
            authorization[7:],
            body.repository_ids,
        )
        response = JSONResponse({"redirect": redirect}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(cookie_name, path="/oauth/consent")
        return response

    @app.post("/oauth/consent/deny")
    async def deny(request: Request, body: ConsentRequest) -> JSONResponse:
        if (
            request.headers.getlist("origin") != [provider.issuer]
            or request.headers.get("host") != urlsplit(provider.issuer).netloc
        ):
            raise DomainError("invalid_request", "Consent requires a same-origin browser", 400)
        cookie_name = "tacit_mcp_" + body.request[:12]
        redirect = await run_in_threadpool(
            provider.deny, body.request, request.cookies.get(cookie_name, "")
        )
        response = JSONResponse({"redirect": redirect}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(cookie_name, path="/oauth/consent")
        return response

    async def revoke(request: Request) -> JSONResponse:
        # The SDK revocation request model requires client_secret even for public clients.
        # All registered clients here use auth method "none"; validate their client ID
        # and revoke atomically without depending on GitHub availability or token validity.
        form = await request.form()
        cid, value = form.get("client_id"), form.get("token")
        if (
            not isinstance(cid, str)
            or cid not in provider.clients
            or not isinstance(value, str)
            or not 1 <= len(value) <= 256
            or any(len(form.getlist(key)) != 1 for key in form)
        ):
            return JSONResponse(
                {"error": "invalid_request"}, status_code=400, headers={"Cache-Control": "no-store"}
            )
        await provider.revoke_token(
            RemoteAccessToken(
                token=value,
                client_id=cid,
                scopes=[SCOPE],
                resource=provider.resource,
                delegated_token="",
            )
        )
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    async def metadata(request: Request) -> JSONResponse:
        # SDK defaults advertise confidential clients and refresh tokens. This deployment
        # deliberately supports public pre-registered clients and authorization codes only.
        document = build_metadata(
            AnyHttpUrl(provider.issuer),
            None,
            ClientRegistrationOptions(enabled=False, valid_scopes=[SCOPE]),
            RevocationOptions(enabled=True),
        )
        document.grant_types_supported = ["authorization_code"]
        document.token_endpoint_auth_methods_supported = ["none"]
        document.revocation_endpoint_auth_methods_supported = ["none"]
        return JSONResponse(
            document.model_dump(mode="json", exclude_none=True),
            headers={"Cache-Control": "no-store"},
        )

    remote = server.streamable_http_app()
    remote.router.routes.insert(
        0, Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"])
    )
    # Keep SDK discovery URLs while specializing the public-client metadata and revocation handler.
    remote.router.routes.insert(0, Route("/revoke", revoke, methods=["POST"]))
    if origins:
        remote.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "MCP-Protocol-Version",
                "MCP-Session-ID",
            ],
            expose_headers=["WWW-Authenticate", "MCP-Session-ID", "MCP-Protocol-Version"],
        )

    remote.add_middleware(RemoteBoundary, provider=provider, origins=origins)
    app.mount("/", remote)
    return server
