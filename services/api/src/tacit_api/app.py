import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
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
    DraftRequest,
    DraftView,
    ErrorResponse,
    EvaluationResponse,
    InvitationRequest,
    PublicationRequest,
    PublicationResponse,
    QuestionRequest,
    RoleChange,
    ScenarioRequest,
    VoiceFinishRequest,
    VoiceRequest,
    VoiceSessionRequest,
    VoiceSessionView,
)
from tacit_domain.access import DomainError, Principal
from tacit_domain.knowledge import KnowledgeService

from tacit_api.access import AccessService
from tacit_api.db import (
    Credential,
    Installation,
    Membership,
    PublicationReceipt,
    RoleAssignment,
    TeachingDraft,
    VoiceAttempt,
    VoiceChunk,
    VoiceSession,
    VoiceTranscript,
)
from tacit_api.demo import DemoStore, demo_principals
from tacit_api.github import GitHub, verify_webhook
from tacit_api.persistence import PostgreSQLStore
from tacit_api.providers import SpeechToText
from tacit_api.publication import PublicationService
from tacit_api.voice import MAX_REQUEST_BYTES, Prisma, VoiceService
from tacit_api.voice_sessions import CHUNK_BYTES, VoiceSessions
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
    gnani_api_key: str = ""
    voice_retention_seconds: int = 86400


def create_app(
    settings: Settings | None = None,
    *,
    github: GitHub | None = None,
    database: Engine | None = None,
    speech: SpeechToText | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if not 60 <= settings.voice_retention_seconds <= 604800:
        raise ValueError("Voice retention must be between one minute and seven days")
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
        async def cleanup() -> None:
            while True:
                if access is not None:
                    try:
                        await run_in_threadpool(
                            VoiceSessions(
                                PublicationService(access),
                                speech or Prisma(settings.gnani_api_key),
                                settings.voice_retention_seconds,
                            ).prune
                        )
                    except SQLAlchemyError:
                        # Readiness reports missing migrations; cleanup retries after recovery.
                        pass
                await asyncio.sleep(60)

        cleanup_task = asyncio.create_task(cleanup()) if access is not None else None
        try:
            yield
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
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
                db.execute(select(TeachingDraft.id).limit(1))
                db.execute(select(VoiceAttempt.operation_id).limit(1))
                db.execute(select(VoiceTranscript.operation_id).limit(1))
                db.execute(select(VoiceSession.id).limit(1))
                db.execute(select(VoiceChunk.sequence).limit(1))
                db.execute(select(PublicationReceipt.operation_id).limit(1))
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
            "parent_snapshot_id": snap.parent_snapshot_id,
            "manifest_digest": snap.manifest_digest,
        }

    @app.get("/v1/repos/{rid}/decisions/{did}/history")
    def history(
        rid: str,
        did: str,
        snapshot_id: str,
        cursor: int = Query(0, ge=0, le=1000),
        limit: int = Query(20, ge=1, le=100),
        p: Principal = Depends(principal),
    ) -> list[dict[str, object]]:
        return [
            d.model_dump(mode="json")
            for d in knowledge().history(p, rid, snapshot_id, did, cursor, limit)
        ]

    def maintainer(rid: str, p: Principal = Depends(principal)) -> Principal:
        knowledge().repository(p, rid, write=True)
        return p

    def publications() -> PublicationService:
        if settings.demo:
            raise DomainError(
                "not_implemented", "Publication is available only with PostgreSQL persistence", 501
            )
        return PublicationService(access_management())

    @app.get("/v1/repos/{rid}/head")
    def head(rid: str, p: Principal = Depends(principal)) -> dict[str, object]:
        knowledge().repository(p, rid)
        return publications().head(p, rid)

    @app.post("/v1/repos/{rid}/teaching-sessions", response_model=DraftView)
    def draft(
        rid: str,
        request: Request,
        body: DraftRequest | None = None,
        p: Principal = Depends(maintainer),
    ) -> DraftView:
        manager = publications()
        if body is None:
            raise DomainError("unsupported_schema", "A source-bound text draft is required", 422)
        return manager.create(p, rid, request.headers["Authorization"].split(" ", 1)[1], body)

    def voice_sessions() -> VoiceSessions:
        return VoiceSessions(
            publications(),
            speech if speech is not None else Prisma(settings.gnani_api_key),
            settings.voice_retention_seconds,
        )

    def request_token(request: Request) -> str:
        return request.headers["Authorization"].split(" ", 1)[1]

    @app.post("/v1/repos/{rid}/voice-sessions", response_model=VoiceSessionView)
    def start_voice_session(
        rid: str, body: VoiceSessionRequest, request: Request, p: Principal = Depends(maintainer)
    ) -> VoiceSessionView:
        return voice_sessions().start(p, rid, request_token(request), body)

    @app.get("/v1/repos/{rid}/voice-sessions/{sid}", response_model=VoiceSessionView)
    def get_voice_session(
        rid: str, sid: str, request: Request, p: Principal = Depends(maintainer)
    ) -> VoiceSessionView:
        return voice_sessions().get(p, rid, request_token(request), sid)

    @app.put(
        "/v1/repos/{rid}/voice-sessions/{sid}/chunks/{sequence}", response_model=VoiceSessionView
    )
    async def voice_chunk(
        rid: str, sid: str, sequence: int, request: Request, p: Principal = Depends(maintainer)
    ) -> VoiceSessionView:
        pcm = bytearray()
        async for data in request.stream():
            if len(pcm) + len(data) > CHUNK_BYTES:
                raise DomainError("audio_too_large", "Audio chunk exceeds one second", 413)
            pcm.extend(data)
        return await run_in_threadpool(
            voice_sessions().append, p, rid, request_token(request), sid, sequence, bytes(pcm)
        )

    @app.websocket("/v1/repos/{rid}/voice-sessions/{sid}/upload")
    async def voice_upload(socket: WebSocket, rid: str, sid: str) -> None:
        token = socket.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            manager = voice_sessions()
            p = await run_in_threadpool(access_management().principal, token)
            view = await run_in_threadpool(manager.get, p, rid, token, sid)
        except DomainError:
            await socket.close(code=1008)
            return
        await socket.accept()
        await socket.send_json(view.model_dump(mode="json"))
        checked = time.monotonic()
        try:
            while True:
                message = await asyncio.wait_for(socket.receive(), 20)
                if message["type"] == "websocket.disconnect":
                    return
                packet = message.get("bytes")
                if not isinstance(packet, bytes):
                    raise DomainError("invalid_audio_chunk", "Binary audio packet required", 422)
                if len(packet) <= 8 or len(packet) > CHUNK_BYTES + 8:
                    raise DomainError("invalid_audio_chunk", "Invalid sequenced audio packet", 422)
                if time.monotonic() - checked > 10:
                    p = await run_in_threadpool(access_management().principal, token)
                    checked = time.monotonic()
                view = await run_in_threadpool(
                    manager.append,
                    p,
                    rid,
                    token,
                    sid,
                    int.from_bytes(packet[:8], "big"),
                    packet[8:],
                )
                await socket.send_json(view.model_dump(mode="json"))
        except DomainError as exc:
            await socket.send_json({"code": exc.code})
            await socket.close(code=1008)
        except TimeoutError:
            await socket.close(code=1000)
        except WebSocketDisconnect:
            return

    @app.post("/v1/repos/{rid}/voice-sessions/{sid}/finish", response_model=VoiceSessionView)
    def finish_voice_session(
        rid: str,
        sid: str,
        body: VoiceFinishRequest,
        request: Request,
        p: Principal = Depends(maintainer),
    ) -> VoiceSessionView:
        return voice_sessions().finish(p, rid, request_token(request), sid, body)

    @app.get("/v1/repos/{rid}/voice-sessions/{sid}/audio")
    def voice_audio(
        rid: str, sid: str, request: Request, p: Principal = Depends(maintainer)
    ) -> Response:
        return Response(
            voice_sessions().audio(p, rid, request_token(request), sid),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/v1/repos/{rid}/voice-sessions/{sid}", response_model=VoiceSessionView)
    def cancel_voice_session(
        rid: str, sid: str, request: Request, p: Principal = Depends(maintainer)
    ) -> VoiceSessionView:
        return voice_sessions().cancel(p, rid, request_token(request), sid)

    @app.post("/v1/repos/{rid}/voice-sessions/{sid}/transcribe", response_model=VoiceSessionView)
    async def transcribe_voice_session(
        rid: str, sid: str, request: Request, p: Principal = Depends(maintainer)
    ) -> VoiceSessionView:
        if speech is None and not settings.gnani_api_key:
            raise DomainError(
                "provider_unconfigured", "Set TACIT_GNANI_API_KEY on the backend", 503
            )
        return await voice_sessions().transcribe(p, rid, request_token(request), sid)

    @app.get("/v1/repos/{rid}/voice-capabilities")
    def voice_capabilities(rid: str, p: Principal = Depends(maintainer)) -> dict[str, object]:
        publications()
        return {
            "configured": speech is not None or bool(settings.gnani_api_key),
            "max_seconds": 895,
            "rest_max_seconds": 30,
            "upload": "sequenced PCM; websocket or resumable HTTP",
            "sample_rate": 16000,
            "channels": 1,
            "format": "PCM16 WAV",
        }

    @app.get("/v1/repos/{rid}/voice-transcripts/{operation}")
    def voice_transcript(
        rid: str, operation: str, request: Request, p: Principal = Depends(maintainer)
    ) -> dict[str, object]:
        token = request.headers["Authorization"].split(" ", 1)[1]
        return VoiceService(publications(), Prisma("")).transcript(p, rid, token, operation)

    @app.post("/v1/repos/{rid}/voice-drafts", response_model=DraftView)
    async def voice_draft(
        rid: str, request: Request, p: Principal = Depends(maintainer)
    ) -> DraftView:
        provider = speech if speech is not None else Prisma(settings.gnani_api_key)
        if isinstance(provider, Prisma) and not provider.key:
            raise DomainError(
                "provider_unconfigured", "Set TACIT_GNANI_API_KEY on the backend", 503
            )
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_REQUEST_BYTES:
                raise DomainError(
                    "audio_too_large", "Voice request exceeds the short-clip limit", 413
                )
            content.extend(chunk)
        try:
            body = VoiceRequest.model_validate_json(bytes(content))
        except ValidationError as exc:
            raise DomainError(
                "invalid_voice_request", "Invalid voice metadata or audio encoding", 422
            ) from exc
        token = request.headers["Authorization"].split(" ", 1)[1]
        return await VoiceService(publications(), provider).create(p, rid, token, body)

    @app.get("/v1/repos/{rid}/teaching-sessions/{draft_id}", response_model=DraftView)
    def review(rid: str, draft_id: str, p: Principal = Depends(maintainer)) -> DraftView:
        return publications().review(p, rid, draft_id)

    @app.post("/v1/repos/{rid}/publications", response_model=PublicationResponse)
    def publish(
        rid: str,
        request: Request,
        body: PublicationRequest | None = None,
        p: Principal = Depends(maintainer),
    ) -> PublicationResponse:
        manager = publications()
        if body is None:
            raise DomainError(
                "unsupported_schema", "An explicit reviewed confirmation is required", 422
            )
        return manager.publish(p, rid, request.headers["Authorization"].split(" ", 1)[1], body)

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
