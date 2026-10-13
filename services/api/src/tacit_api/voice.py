"""Bounded short-clip transcription. Never silently replay a billable request."""

import base64
import binascii
import hashlib
import io
import logging
import wave
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tacit_contracts.models import DraftRequest, DraftView, VoiceEvidence, VoiceRequest
from tacit_domain.access import DomainError, Principal

from tacit_api.db import TeachingDraft, VoiceAttempt, VoiceSession, VoiceTranscript
from tacit_api.providers import ProviderUnavailable, SpeechToText
from tacit_api.publication import PublicationService, fingerprint
from tacit_api.streaming import StreamResult, transcribe_pcm

MAX_AUDIO_BYTES = 961_024
MAX_REQUEST_BYTES = 1_300_000
logger = logging.getLogger(__name__)


def validate_audio(audio: bytes) -> float:
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Audio exceeds the 30-second REST limit; use durable voice sessions")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
                1,
                2,
                16000,
                "NONE",
            ):
                raise ValueError("Use mono 16 kHz PCM16 WAV audio")
            if not 0 < frames <= 480_000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError(
                    "Audio is empty, truncated, or longer than the 30-second REST limit"
                )
            return frames / 16000
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid PCM WAV audio") from exc


class Prisma:
    def __init__(self, key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.key = key
        self.transport = transport

    async def transcribe_stream(self, pcm: bytes, *, language: str = "en-IN") -> "StreamResult":
        return await transcribe_pcm(self.key, pcm, language)

    async def transcribe(self, audio: bytes, sample_rate: int, *, language: str = "en-IN") -> str:
        if not self.key:
            raise ProviderUnavailable("Set TACIT_GNANI_API_KEY on the backend to enable Prisma")
        validate_audio(audio)
        stage = "request"
        status: int | None = None
        try:
            async with httpx.AsyncClient(
                timeout=90, trust_env=False, follow_redirects=False, transport=self.transport
            ) as http:
                response = await http.post(
                    "https://api.vachana.ai/stt/v3",
                    headers={"X-API-Key-ID": self.key},
                    files={"audio_file": ("recording.wav", audio, "audio/wav")},
                    data={
                        "language_code": language,
                        "format": "verbatim",
                        "enable_substitution": "false",
                    },
                )
                status = response.status_code
                stage = "http_status"
                response.raise_for_status()
                stage = "response_json"
                result = response.json()
            stage = "transcript_validation"
            text = result.get("transcript")
            if (
                result.get("success") is not True
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 8000
            ):
                raise ValueError("Invalid transcript")
            return text
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            # Never echo provider bodies, credentials, transcripts, or automatically retry.
            logger.warning(
                "Prisma REST failed: stage=%s exception=%s http_status=%s",
                stage,
                type(exc).__name__,
                status,
            )
            raise ProviderUnavailable(
                "Prisma failed or returned an invalid transcript; request outcome may be uncertain"
            ) from exc


class VoiceService:
    def __init__(self, publications: PublicationService, provider: SpeechToText):
        self.publications = publications
        self.provider = provider

    def check_session(
        self, db: Session, p: Principal, rid: str, body: VoiceRequest, session_id: str | None
    ) -> None:
        session = db.scalar(
            select(VoiceSession).where(
                VoiceSession.repository_id == rid,
                VoiceSession.author_id == p.user_id,
                VoiceSession.operation_id == body.operation_id,
            )
        )
        if session is None and session_id is None:
            return
        if session is None or session.id != session_id or session.status != "processing":
            raise DomainError(
                "idempotency_conflict", "Operation belongs to a durable voice session", 409
            )
        context = body.model_dump(mode="json", exclude={"audio_base64"})
        with wave.open(io.BytesIO(base64.b64decode(body.audio_base64)), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        if context != session.context or hashlib.sha256(pcm).hexdigest() != session.audio_sha256:
            raise DomainError(
                "idempotency_conflict", "Voice session audio or source binding changed", 409
            )

    def reserve(
        self, p: Principal, rid: str, token: str, body: VoiceRequest, session_id: str | None = None
    ) -> DraftView | tuple[DraftRequest, VoiceEvidence] | None:
        manager = self.publications
        request_digest = fingerprint(body.model_dump(mode="json"))
        with Session(manager.engine) as db, db.begin():
            repo = manager.write_repository(db, p, rid, token)
            self.check_session(db, p, rid, body, session_id)
            attempt = db.get(VoiceAttempt, (repo.workspace_id, rid, p.user_id, body.operation_id))
            if attempt:
                if attempt.request_digest != request_digest:
                    raise DomainError(
                        "idempotency_conflict",
                        "Voice operation ID was used with different input",
                        409,
                    )
                prior = db.scalar(
                    select(TeachingDraft).where(
                        TeachingDraft.repository_id == rid,
                        TeachingDraft.author_id == p.user_id,
                        TeachingDraft.operation_id == body.operation_id,
                    )
                )
                if prior:
                    return manager.view(db, prior)
                transcript = db.get(
                    VoiceTranscript, (repo.workspace_id, rid, p.user_id, body.operation_id)
                )
                if transcript:
                    return (
                        DraftRequest.model_validate(transcript.payload["request"]),
                        VoiceEvidence.model_validate(transcript.payload["voice"]),
                    )
                raise DomainError(
                    "voice_outcome_uncertain",
                    "This transcription was already attempted. It will not be replayed; a new operation explicitly starts a new potentially billable request",
                    409,
                )
            if db.scalar(
                select(TeachingDraft.id).where(
                    TeachingDraft.repository_id == rid,
                    TeachingDraft.author_id == p.user_id,
                    TeachingDraft.operation_id == body.operation_id,
                )
            ):
                raise DomainError(
                    "idempotency_conflict", "Operation ID already belongs to another draft", 409
                )
            manager.current(db, repo, body.expected_snapshot_id)
            credential = manager.access.credential(db, hashlib.sha256(token.encode()).hexdigest())
            provider_token = credential.provider_token
            if not provider_token:
                raise DomainError("unauthenticated", "GitHub credential unavailable", 401)
            full_name = repo.full_name
        # Reject invalid source bindings before any billable transcription.
        manager.access.github.source_bindings(
            manager.access.decrypt(provider_token), full_name, body.source_commit, body.files
        )
        with Session(manager.engine) as db, db.begin():
            repo = manager.write_repository(db, p, rid, token)
            self.check_session(db, p, rid, body, session_id)
            manager.current(db, repo, body.expected_snapshot_id)
            if db.scalar(
                select(TeachingDraft.id).where(
                    TeachingDraft.repository_id == rid,
                    TeachingDraft.author_id == p.user_id,
                    TeachingDraft.operation_id == body.operation_id,
                )
            ):
                raise DomainError("idempotency_conflict", "Operation belongs to another draft", 409)
            if db.get(VoiceAttempt, (repo.workspace_id, rid, p.user_id, body.operation_id)):
                raise DomainError(
                    "voice_outcome_uncertain",
                    "Concurrent transcription already reserved this operation; retry to retrieve its completed draft",
                    409,
                )
            db.add(
                VoiceAttempt(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    request_digest=request_digest,
                )
            )
        return None

    async def create(
        self,
        p: Principal,
        rid: str,
        token: str,
        body: VoiceRequest,
        *,
        before_save: Callable[[], None] | None = None,
        session_id: str | None = None,
        capture_complete: bool = True,
    ) -> DraftView:
        from starlette.concurrency import run_in_threadpool

        try:
            audio = base64.b64decode(body.audio_base64, validate=True)
            duration = validate_audio(audio)
        except (ValueError, binascii.Error) as exc:
            raise DomainError("invalid_audio", str(exc), 422) from exc
        prior = await run_in_threadpool(self.reserve, p, rid, token, body, session_id)
        if isinstance(prior, DraftView):
            return prior
        if isinstance(prior, tuple):
            return await run_in_threadpool(
                self.publications.create, p, rid, token, prior[0], voice=prior[1]
            )
        try:
            transcript = await self.provider.transcribe(audio, 16000, language=body.language)
            request = DraftRequest(
                operation_id=body.operation_id,
                statement=transcript,
                source_commit=body.source_commit,
                files=body.files,
                expected_snapshot_id=body.expected_snapshot_id,
            )
        except (ProviderUnavailable, ValueError) as exc:
            raise DomainError(
                "transcription_failed",
                "Transcription failed; no draft was published. Reusing this operation will not send audio again",
                503,
            ) from exc
        if before_save is not None:
            await run_in_threadpool(before_save)
        voice = VoiceEvidence(
            original_transcript=transcript,
            audio_sha256=hashlib.sha256(audio).hexdigest(),
            language=body.language,
            operation_id=body.operation_id,
            session_id=session_id,
            capture_complete=capture_complete,
            duration_seconds=duration,
        )
        # Refresh GitHub access after the external call, then publication rechecks local grants.
        fresh = await run_in_threadpool(self.publications.access.principal, token)
        await run_in_threadpool(
            self.save_transcript, fresh, rid, token, body.operation_id, request, voice
        )
        return await run_in_threadpool(
            self.publications.create, fresh, rid, token, request, voice=voice
        )

    def save_transcript(
        self,
        p: Principal,
        rid: str,
        token: str,
        operation: str,
        request: DraftRequest,
        voice: VoiceEvidence,
    ) -> None:
        with Session(self.publications.engine) as db, db.begin():
            repo = self.publications.write_repository(db, p, rid, token)
            if voice.session_id is not None:
                session = db.get(VoiceSession, (repo.workspace_id, rid, voice.session_id))
                if (
                    session is None
                    or session.author_id != p.user_id
                    or session.operation_id != operation
                    or session.status != "processing"
                ):
                    raise DomainError("voice_state", "Voice session was cancelled or expired", 409)
            db.add(
                VoiceTranscript(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=operation,
                    payload={
                        "request": request.model_dump(mode="json"),
                        "voice": voice.model_dump(mode="json"),
                    },
                )
            )

    def transcript(self, p: Principal, rid: str, token: str, operation: str) -> dict[str, object]:
        with Session(self.publications.engine) as db, db.begin():
            repo = self.publications.write_repository(db, p, rid, token)
            result = db.get(VoiceTranscript, (repo.workspace_id, rid, p.user_id, operation))
            if result is None:
                raise DomainError("not_found", "Completed transcript unavailable", 404)
            return result.payload
