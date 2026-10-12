from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from tacit_contracts.models import (
    AgentTokenRequest,
    ContextRequest,
    ContextResponse,
    DevicePoll,
    ErrorResponse,
    EvaluationResponse,
    InvitationRequest,
    QuestionRequest,
    RoleChange,
    ScenarioRequest,
)
from tacit_domain.access import DomainError, Principal
from tacit_domain.knowledge import KnowledgeService

from tacit_api.access import AccessService
from tacit_api.db import Credential, Installation, Membership, RoleAssignment
from tacit_api.demo import DemoStore, demo_principals
from tacit_api.github import GitHub, verify_webhook
from tacit_api.persistence import PostgreSQLStore
from tacit_api.webhooks import ingest


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TACIT_", extra="ignore")
    demo: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    github_webhook_secret: str = ""
    database_url: str = ""
    github_client_id: str = ""
    github_app_id: int = 0
    credential_encryption_key: str = ""


def create_app(
    settings: Settings | None = None,
    *,
    github: GitHub | None = None,
    database: Engine | None = None,
) -> FastAPI:
    settings = settings or Settings()
    engine = database or (
        create_engine(settings.database_url, pool_pre_ping=True)
        if settings.database_url and not settings.demo
        else None
    )
    if engine is not None and database is None and engine.dialect.name != "postgresql":
        raise ValueError("Production persistence requires PostgreSQL")
    access = None
    if (
        engine is not None
        and settings.github_client_id
        and settings.github_app_id
        and settings.credential_encryption_key
    ):
        access = AccessService(
            engine,
            github or GitHub(settings.github_client_id, settings.github_app_id),
            settings.credential_encryption_key,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None and database is None:
            engine.dispose()

    app = FastAPI(title="Tacit API", version="0.1.0", lifespan=lifespan)
    # No production store/auth fallback. Only explicit demo mode creates fixture data.
    service = (
        KnowledgeService(DemoStore())
        if settings.demo
        else (
            KnowledgeService(PostgreSQLStore(engine))
            if engine is not None and access is not None
            else None
        )
    )
    tokens = demo_principals() if settings.demo else {}
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error(request: Request, code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                code=code,
                message=message,
                request_id=getattr(request.state, "request_id", str(uuid4())),
            ).model_dump(),
        )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error(request, exc.code, str(exc), exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error(request, "unsupported_schema", "Request does not match the contract", 422)

    def principal(
        request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
    ) -> Principal:
        if (
            settings.demo
            and request.client
            and request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}
        ):
            raise DomainError("forbidden", "Demo mode is loopback-only")
        if credentials is not None and access is not None:
            return access.principal(credentials.credentials)
        if credentials is None or credentials.credentials not in tokens:
            raise DomainError("unauthenticated", "A valid Tacit credential is required", 401)
        return tokens[credentials.credentials]

    def access_management() -> AccessService:
        if access is None:
            raise DomainError(
                "service_unavailable", "Production GitHub authentication is not configured", 503
            )
        return access

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error(request, "service_unavailable", "Database operation unavailable", 503)

    def knowledge() -> KnowledgeService:
        if service is None:
            raise DomainError(
                "service_unavailable",
                "Production persistence and authentication are not configured",
                503,
            )
        return service

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        knowledge()
        if settings.demo:
            return {"status": "demo", "persistence": "ephemeral"}
        if engine is not None:
            with Session(engine) as db:
                db.execute(select(Credential.token_hash).limit(1))
                db.execute(select(Installation.github_id).limit(1))
        return {"status": "ready", "persistence": "postgresql"}

    @app.get("/v1/auth/me")
    def me(p: Principal = Depends(principal)) -> dict[str, object]:
        return {"user_id": p.user_id, "agent": p.agent}

    @app.get("/v1/workspaces")
    def workspaces(p: Principal = Depends(principal)) -> list[dict[str, str]]:
        return [{"id": w} for w in sorted(p.memberships)]

    @app.get("/v1/workspaces/{wid}/repos")
    def repositories(wid: str, p: Principal = Depends(principal)) -> list[dict[str, str]]:
        if wid not in p.memberships:
            raise DomainError("not_found", "Workspace unavailable", 404)
        result = []
        for r in knowledge().store.repositories():
            if r.workspace_id != wid:
                continue
            try:
                knowledge().repository(p, r.id)
            except DomainError:
                continue
            result.append({"id": r.id, "workspace_id": wid, "full_name": r.full_name})
        return result

    @app.post("/v1/repos/{rid}/context", response_model=ContextResponse)
    def context(
        rid: str, body: ContextRequest, p: Principal = Depends(principal)
    ) -> ContextResponse:
        return knowledge().context(p, rid, body)

    @app.post("/v1/repos/{rid}/questions", response_model=ContextResponse)
    def question(
        rid: str, body: QuestionRequest, p: Principal = Depends(principal)
    ) -> ContextResponse:
        result = knowledge().context(p, rid, body)
        # Exact statements only; no semantic-answer quality claim without a reasoner.
        return result.model_copy(
            update={
                "answer": "\n".join(d.statement for d in result.decisions) or None,
                "answer_kind": "confirmed_statements" if result.decisions else "none",
                "warnings": result.warnings
                + (
                    "Question reasoning is not implemented; these are scope-matched confirmed statements",
                ),
            }
        )

    @app.post("/v1/repos/{rid}/evaluate", response_model=EvaluationResponse)
    def scenario(
        rid: str, body: ScenarioRequest, p: Principal = Depends(principal)
    ) -> EvaluationResponse:
        return knowledge().evaluate(p, rid, body)

    @app.get("/v1/repos/{rid}/snapshots/{sid}")
    def snapshot(rid: str, sid: str, p: Principal = Depends(principal)) -> dict[str, object]:
        _, snap = knowledge().snapshot(p, rid, sid)
        return {
            "id": snap.id,
            "repository_id": rid,
            "source_commit": snap.source_commit,
            "revision_ids": [d.revision_id for d in snap.decisions],
        }

    @app.get("/v1/repos/{rid}/decisions/{did}/history")
    def history(
        rid: str, did: str, snapshot_id: str, p: Principal = Depends(principal)
    ) -> list[dict[str, object]]:
        _, snap = knowledge().snapshot(p, rid, snapshot_id)
        decisions = [d.model_dump(mode="json") for d in snap.decisions if d.id == did]
        if not decisions:
            raise DomainError("not_found", "Decision unavailable", 404)
        return decisions

    @app.post("/v1/repos/{rid}/teaching-sessions")
    @app.post("/v1/repos/{rid}/publications")
    def authoring(rid: str, p: Principal = Depends(principal)) -> None:
        knowledge().repository(p, rid, write=True)
        raise DomainError(
            "not_implemented", "Teaching and transactional publication are not implemented", 501
        )

    @app.post("/v1/auth/device/start")
    def device_start() -> dict[str, object]:
        return access_management().start()

    @app.post("/v1/auth/device/poll")
    def device_poll(body: DevicePoll) -> dict[str, object]:
        return access_management().poll(body.pairing_token)

    @app.post("/v1/auth/logout")
    def logout(request: Request, p: Principal = Depends(principal)) -> dict[str, str]:
        access_management().logout(request.headers["Authorization"].split(" ", 1)[1])
        return {"status": "revoked"}

    @app.post("/v1/auth/agent-tokens")
    def delegate(
        body: AgentTokenRequest, request: Request, p: Principal = Depends(principal)
    ) -> dict[str, object]:
        return access_management().delegate(
            request.headers["Authorization"].split(" ", 1)[1], p, body
        )

    @app.get("/v1/workspaces/{wid}/members")
    def members(wid: str, p: Principal = Depends(principal)) -> list[dict[str, object]]:
        manager = access_management()
        manager.admin(p, wid)
        with Session(manager.engine) as db:
            return [
                {
                    "user_id": m.user_id,
                    "active": m.active,
                    "roles": [
                        {"role": g.role, "repository_id": g.repository_id}
                        for g in db.scalars(
                            select(RoleAssignment).where(
                                RoleAssignment.workspace_id == wid,
                                RoleAssignment.user_id == m.user_id,
                            )
                        )
                    ],
                }
                for m in db.scalars(select(Membership).where(Membership.workspace_id == wid))
            ]

    @app.post("/v1/workspaces/{wid}/roles")
    def grant(wid: str, body: RoleChange, p: Principal = Depends(principal)) -> dict[str, str]:
        access_management().change_role(p, wid, body, False)
        return {"status": "granted"}

    @app.post("/v1/workspaces/{wid}/roles/revoke")
    def revoke(wid: str, body: RoleChange, p: Principal = Depends(principal)) -> dict[str, str]:
        access_management().change_role(p, wid, body, True)
        return {"status": "revoked"}

    @app.post("/v1/workspaces/{wid}/invitations")
    def invite(
        wid: str, body: InvitationRequest, p: Principal = Depends(principal)
    ) -> dict[str, str]:
        access_management().invite(p, wid, body)
        return {"status": "created"}

    @app.delete("/v1/workspaces/{wid}/members/{uid}")
    def suspend(wid: str, uid: str, p: Principal = Depends(principal)) -> dict[str, str]:
        access_management().suspend(p, wid, uid)
        return {"status": "suspended"}

    @app.post("/v1/github/webhooks")
    async def webhook(request: Request) -> dict[str, str]:
        if not settings.github_webhook_secret:
            raise DomainError("service_unavailable", "Webhook integration is not configured", 503)
        if not verify_webhook(
            await request.body(),
            request.headers.get("X-Hub-Signature-256", ""),
            settings.github_webhook_secret,
        ):
            raise DomainError("unauthenticated", "Invalid webhook signature", 401)
        if engine is None:
            raise DomainError(
                "not_implemented", "Durable webhook reconciliation is not configured", 501
            )
        return await run_in_threadpool(
            ingest,
            engine,
            request.headers.get("X-GitHub-Delivery", ""),
            request.headers.get("X-GitHub-Event", ""),
            await request.body(),
        )

    return app
