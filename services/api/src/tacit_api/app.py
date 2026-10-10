from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.responses import Response
from tacit_contracts.models import (
    ContextRequest,
    ContextResponse,
    ErrorResponse,
    EvaluationResponse,
    QuestionRequest,
    ScenarioRequest,
)
from tacit_domain.access import DomainError, Principal
from tacit_domain.knowledge import KnowledgeService

from tacit_api.demo import DemoStore, demo_principals
from tacit_api.github import verify_webhook


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TACIT_", extra="ignore")
    demo: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    github_webhook_secret: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Tacit API", version="0.1.0")
    # No production store/auth fallback. Only explicit demo mode creates fixture data.
    service = KnowledgeService(DemoStore()) if settings.demo else None
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
        if credentials is None or credentials.credentials not in tokens:
            raise DomainError("unauthenticated", "A valid Tacit credential is required", 401)
        return tokens[credentials.credentials]

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
        return {"status": "demo", "persistence": "ephemeral"}

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
    @app.post("/v1/auth/device/poll")
    def login() -> None:
        raise DomainError("not_implemented", "GitHub device pairing is not implemented", 501)

    @app.post("/v1/github/webhooks")
    async def webhook(request: Request) -> None:
        if not settings.github_webhook_secret:
            raise DomainError("service_unavailable", "Webhook integration is not configured", 503)
        if not verify_webhook(
            await request.body(),
            request.headers.get("X-Hub-Signature-256", ""),
            settings.github_webhook_secret,
        ):
            raise DomainError("unauthenticated", "Invalid webhook signature", 401)
        # Never acknowledge durable acceptance without a persisted inbox/outbox transaction.
        raise DomainError(
            "not_implemented", "Durable webhook reconciliation is not implemented", 501
        )

    return app
