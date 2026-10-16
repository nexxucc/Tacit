"""Sequenced, encrypted uploads with author-scoped access and bounded retention."""

import base64
import hashlib
import io
import wave
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from tacit_contracts.models import (
    DraftRequest,
    VoiceEvidence,
    VoiceFinishRequest,
    VoiceRequest,
    VoiceSessionRequest,
    VoiceSessionView,
)
from tacit_domain.access import DomainError, Principal

from tacit_api.access import now, valid_time
from tacit_api.db import (
    Repository,
    TeachingDraft,
    User,
    VoiceAttempt,
    VoiceChunk,
    VoiceSession,
    VoiceTranscript,
)
from tacit_api.providers import ProviderUnavailable, SpeechToText
from tacit_api.publication import PublicationService, fingerprint
from tacit_api.voice import Prisma, VoiceService

CHUNK_BYTES = 32000
MAX_PCM = 895 * CHUNK_BYTES


class VoiceSessions:
    def __init__(self, manager: PublicationService, provider: SpeechToText, retention: int = 86400):
        self.manager, self.provider, self.retention = manager, provider, retention

    def check_operation(self, db: Session, wid: str, rid: str, uid: str, operation: str) -> None:
        if db.get(VoiceAttempt, (wid, rid, uid, operation)) or db.scalar(
            select(TeachingDraft.id).where(
                TeachingDraft.repository_id == rid,
                TeachingDraft.author_id == uid,
                TeachingDraft.operation_id == operation,
            )
        ):
            raise DomainError(
                "idempotency_conflict",
                "Operation belongs to an earlier transcription or draft",
                409,
            )

    def scoped(self, db: Session, p: Principal, rid: str, token: str, sid: str) -> VoiceSession:
        repo = self.manager.write_repository(db, p, rid, token)
        item = db.get(VoiceSession, (repo.workspace_id, rid, sid), with_for_update=True)
        if item is None or item.author_id != p.user_id:
            raise DomainError("not_found", "Voice session unavailable", 404)
        if valid_time(item.expires_at) <= now() and item.status not in {
            "completed",
            "needs_correction",
            "cancelled",
            "expired",
        }:
            # Cleanup is committed by periodic prune, even when the calling request fails.
            raise DomainError("voice_expired", "Retained audio expired; start a new recording", 410)
        return item

    def view(self, item: VoiceSession, db: Session) -> VoiceSessionView:
        draft = None
        if item.status == "completed":
            from tacit_api.db import TeachingDraft

            row = db.scalar(
                select(TeachingDraft).where(
                    TeachingDraft.repository_id == item.repository_id,
                    TeachingDraft.author_id == item.author_id,
                    TeachingDraft.operation_id == item.operation_id,
                )
            )
            if row:
                draft = self.manager.view(db, row)
        return VoiceSessionView.model_validate(
            {
                "id": item.id,
                "operation_id": item.operation_id,
                "repository_id": item.repository_id,
                "status": item.status,
                "chunks": item.chunks,
                "pcm_bytes": item.pcm_bytes,
                "capture_complete": item.capture_complete,
                "audio_sha256": item.audio_sha256,
                "expires_at": valid_time(item.expires_at).isoformat(),
                "draft": draft,
            }
        )

    def start(
        self, p: Principal, rid: str, token: str, body: VoiceSessionRequest
    ) -> VoiceSessionView:
        digest = fingerprint(body.model_dump(mode="json"))
        with Session(self.manager.engine) as db, db.begin():
            # Serialize this user's quota without blocking FK KEY SHARE locks held
            # by concurrent draft/transcript inserts under the workspace lock.
            db.scalar(select(User).where(User.id == p.user_id).with_for_update(key_share=True))
            repo = self.manager.write_repository(db, p, rid, token)
            existing = db.scalar(
                select(VoiceSession).where(
                    VoiceSession.repository_id == rid,
                    VoiceSession.author_id == p.user_id,
                    VoiceSession.operation_id == body.operation_id,
                )
            )
            if existing:
                if existing.request_digest != digest:
                    raise DomainError("idempotency_conflict", "Voice session input changed", 409)
                return self.view(existing, db)
            self.check_operation(db, repo.workspace_id, rid, p.user_id, body.operation_id)
            self.manager.current(db, repo, body.expected_snapshot_id)
            count = db.scalar(
                select(func.count())
                .select_from(VoiceSession)
                .where(
                    VoiceSession.author_id == p.user_id,
                    VoiceSession.status.in_(["uploading", "ready", "processing", "uncertain"]),
                    VoiceSession.expires_at > now(),
                )
            )
            if count and count >= 3:
                raise DomainError(
                    "voice_quota", "Cancel an active voice session before starting another", 409
                )
            # Reuse source verification without creating a draft or invoking STT.
            from tacit_api.access import digest as token_digest

            credential = self.manager.access.credential(db, token_digest(token))
            if not credential.provider_token:
                raise DomainError("unauthenticated", "GitHub credential unavailable", 401)
            provider_token, full_name = credential.provider_token, repo.full_name
        self.manager.access.github.source_bindings(
            self.manager.access.decrypt(provider_token), full_name, body.source_commit, body.files
        )
        with Session(self.manager.engine) as db, db.begin():
            # Serialize this user's quota without blocking FK KEY SHARE locks held
            # by concurrent draft/transcript inserts under the workspace lock.
            db.scalar(select(User).where(User.id == p.user_id).with_for_update(key_share=True))
            repo = self.manager.write_repository(db, p, rid, token)
            existing = db.scalar(
                select(VoiceSession).where(
                    VoiceSession.repository_id == rid,
                    VoiceSession.author_id == p.user_id,
                    VoiceSession.operation_id == body.operation_id,
                )
            )
            if existing:
                if existing.request_digest != digest:
                    raise DomainError("idempotency_conflict", "Voice session input changed", 409)
                return self.view(existing, db)
            self.check_operation(db, repo.workspace_id, rid, p.user_id, body.operation_id)
            self.manager.current(db, repo, body.expected_snapshot_id)
            count = db.scalar(
                select(func.count())
                .select_from(VoiceSession)
                .where(
                    VoiceSession.author_id == p.user_id,
                    VoiceSession.status.in_(["uploading", "ready", "processing", "uncertain"]),
                    VoiceSession.expires_at > now(),
                )
            )
            if count and count >= 3:
                raise DomainError(
                    "voice_quota", "Cancel an active voice session before starting another", 409
                )
            item = VoiceSession(
                workspace_id=repo.workspace_id,
                repository_id=rid,
                id=str(uuid4()),
                author_id=p.user_id,
                operation_id=body.operation_id,
                request_digest=digest,
                context=body.model_dump(mode="json"),
                status="uploading",
                chunks=0,
                pcm_bytes=0,
                capture_complete=False,
                expires_at=now() + timedelta(seconds=self.retention),
            )
            db.add(item)
            db.flush()
            return self.view(item, db)

    def get(self, p: Principal, rid: str, token: str, sid: str) -> VoiceSessionView:
        with Session(self.manager.engine) as db, db.begin():
            return self.view(self.scoped(db, p, rid, token, sid), db)

    def append(
        self, p: Principal, rid: str, token: str, sid: str, sequence: int, pcm: bytes
    ) -> VoiceSessionView:
        if not 0 <= sequence < 900 or not pcm or len(pcm) > CHUNK_BYTES or len(pcm) % 2:
            raise DomainError("invalid_audio_chunk", "Use bounded, even-length PCM16 chunks", 422)
        hashed = hashlib.sha256(pcm).hexdigest()
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            prior = db.get(VoiceChunk, (item.workspace_id, rid, sid, sequence))
            if prior:
                if prior.sha256 != hashed:
                    raise DomainError(
                        "chunk_conflict", "Sequence was used with different audio", 409
                    )
                return self.view(item, db)
            if item.status != "uploading" or sequence != item.chunks:
                raise DomainError(
                    "chunk_sequence", "Upload must resume at the acknowledged sequence", 409
                )
            if item.pcm_bytes + len(pcm) > MAX_PCM:
                raise DomainError("audio_too_large", "Recording exceeds 14 minutes 55 seconds", 413)
            if item.chunks and item.pcm_bytes != item.chunks * CHUNK_BYTES:
                raise DomainError("chunk_sequence", "A partial chunk must be the final chunk", 409)
            db.add(
                VoiceChunk(
                    workspace_id=item.workspace_id,
                    repository_id=rid,
                    session_id=sid,
                    sequence=sequence,
                    sha256=hashed,
                    encrypted_pcm=self.manager.access.cipher.encrypt(pcm),
                )
            )
            item.chunks += 1
            item.pcm_bytes += len(pcm)
            return self.view(item, db)

    def pcm(self, db: Session, item: VoiceSession) -> bytes:
        chunks = list(
            db.scalars(
                select(VoiceChunk)
                .where(
                    VoiceChunk.workspace_id == item.workspace_id,
                    VoiceChunk.repository_id == item.repository_id,
                    VoiceChunk.session_id == item.id,
                )
                .order_by(VoiceChunk.sequence)
            )
        )
        if [c.sequence for c in chunks] != list(range(item.chunks)):
            raise DomainError("audio_missing", "Retained audio is incomplete", 409)
        audio = bytearray()
        for c in chunks:
            chunk = self.manager.access.cipher.decrypt(c.encrypted_pcm)
            if hashlib.sha256(chunk).hexdigest() != c.sha256:
                raise DomainError(
                    "audio_corrupt", "Retained audio failed integrity validation", 409
                )
            audio.extend(chunk)
        if len(audio) != item.pcm_bytes:
            raise DomainError("audio_missing", "Retained audio size mismatch", 409)
        return bytes(audio)

    def finish(
        self, p: Principal, rid: str, token: str, sid: str, body: VoiceFinishRequest
    ) -> VoiceSessionView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status not in {"uploading", "ready"}:
                raise DomainError("voice_state", "Session cannot be finalized", 409)
            pcm = self.pcm(db, item)
            hashed = hashlib.sha256(pcm).hexdigest()
            if body.chunks != item.chunks or body.audio_sha256 != hashed:
                raise DomainError(
                    "audio_missing", "Final recording manifest does not match uploaded audio", 409
                )
            if item.status == "ready" and item.capture_complete != body.capture_complete:
                raise DomainError("audio_missing", "Final capture manifest changed", 409)
            item.capture_complete = body.capture_complete
            item.audio_sha256, item.status = hashed, "ready"
            return self.view(item, db)

    def cancel(self, p: Principal, rid: str, token: str, sid: str) -> VoiceSessionView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            self.delete_audio(db, item)
            item.status = "cancelled"
            return self.view(item, db)

    def delete_audio(self, db: Session, item: VoiceSession) -> None:
        db.execute(
            delete(VoiceChunk).where(
                VoiceChunk.workspace_id == item.workspace_id,
                VoiceChunk.repository_id == item.repository_id,
                VoiceChunk.session_id == item.id,
            )
        )

    def audio(self, p: Principal, rid: str, token: str, sid: str) -> bytes:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if valid_time(item.expires_at) <= now():
                raise DomainError("voice_expired", "Audio retention expired", 410)
            if item.status not in {"ready", "uncertain"}:
                raise DomainError(
                    "voice_state", "Audio is available only for a finalized retained recording", 409
                )
            pcm = self.pcm(db, item)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)
            return output.getvalue()

    def prune(self) -> int:
        with Session(self.manager.engine) as db, db.begin():
            expired = list(
                db.scalars(
                    select(VoiceSession)
                    .where(
                        VoiceSession.expires_at <= now(),
                        VoiceSession.status.not_in(
                            ("expired", "completed", "needs_correction", "cancelled")
                        ),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            )
            for item in expired:
                self.delete_audio(db, item)
                if item.status not in {"completed", "needs_correction", "cancelled"}:
                    item.status = "expired"
            return len(expired)

    def prepare(
        self, p: Principal, rid: str, token: str, sid: str
    ) -> tuple[VoiceSessionRequest, bytes, dict[str, object] | None]:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            saved = db.get(VoiceTranscript, (item.workspace_id, rid, p.user_id, item.operation_id))
            if saved:
                voice = VoiceEvidence.model_validate(saved.payload["voice"])
                request = saved.payload["request"]
                assert isinstance(request, dict)
                if (
                    voice.session_id != item.id
                    or voice.operation_id != item.operation_id
                    or voice.language != item.context["language"]
                    or request.get("operation_id") != item.operation_id
                    or request.get("source_commit") != item.context["source_commit"]
                    or request.get("files") != item.context["files"]
                    or request.get("expected_snapshot_id") != item.context["expected_snapshot_id"]
                    or voice.duration_seconds != item.pcm_bytes / CHUNK_BYTES
                    or (
                        voice.audio_format == "pcm_s16le"
                        and voice.audio_sha256 != item.audio_sha256
                    )
                ):
                    raise DomainError(
                        "idempotency_conflict",
                        "Saved transcript belongs to different audio or source bindings",
                        409,
                    )
                if item.status in {"cancelled", "expired", "uploading", "ready"}:
                    raise DomainError("voice_state", "Session cannot recover transcription", 409)
                return VoiceSessionRequest.model_validate(item.context), b"", saved.payload
            if item.status != "ready":
                raise DomainError(
                    "voice_outcome_uncertain"
                    if item.status in {"processing", "uncertain"}
                    else "voice_state",
                    "Only a ready session may start transcription; attempted audio will not be replayed",
                    409,
                )
            repo = db.get(Repository, (item.workspace_id, rid))
            assert repo is not None
            context = VoiceSessionRequest.model_validate(item.context)
            self.manager.current(db, repo, context.expected_snapshot_id)
            pcm = self.pcm(db, item)
            item.status = "processing"
            return VoiceSessionRequest.model_validate(item.context), pcm, None

    def ensure_processing(self, p: Principal, rid: str, token: str, sid: str) -> None:
        with Session(self.manager.engine) as db, db.begin():
            if self.scoped(db, p, rid, token, sid).status != "processing":
                raise DomainError("voice_state", "Session was cancelled", 409)

    def set_state(
        self, p: Principal, rid: str, token: str, sid: str, status: str
    ) -> VoiceSessionView:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status in {"cancelled", "expired"}:
                raise DomainError("voice_state", "Session was cancelled or expired", 409)
            if item.status in {"completed", "needs_correction"}:
                return self.view(item, db)
            item.status = status
            if status in {"completed", "needs_correction"}:
                self.delete_audio(db, item)
            return self.view(item, db)

    def save_long(
        self,
        p: Principal,
        rid: str,
        token: str,
        sid: str,
        body: VoiceSessionRequest,
        text: str,
        segments: tuple[dict[str, str | int], ...],
        pcm: bytes,
        transcription_complete: bool = True,
    ) -> dict[str, object]:
        with Session(self.manager.engine) as db, db.begin():
            item = self.scoped(db, p, rid, token, sid)
            if item.status != "processing":
                raise DomainError("voice_state", "Transcription was cancelled", 409)
            hashed = hashlib.sha256(pcm).hexdigest()
            voice = VoiceEvidence(
                original_transcript=text,
                audio_sha256=hashed,
                audio_format="pcm_s16le",
                duration_seconds=len(pcm) / 32000,
                language=body.language,
                operation_id=body.operation_id,
                session_id=sid,
                capture_complete=item.capture_complete,
                transcription_complete=transcription_complete,
                segments=segments,
            )
            payload: dict[str, object] = {
                "request": {
                    **body.model_dump(mode="json", exclude={"language"}),
                    "statement": text,
                },
                "voice": voice.model_dump(mode="json"),
            }
            db.add(
                VoiceAttempt(
                    workspace_id=item.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    request_digest=fingerprint(body.model_dump(mode="json")),
                )
            )
            db.flush()
            db.add(
                VoiceTranscript(
                    workspace_id=item.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    payload=payload,
                )
            )
            return payload

    async def transcribe(self, p: Principal, rid: str, token: str, sid: str) -> VoiceSessionView:
        body, pcm, saved = await run_in_threadpool(self.prepare, p, rid, token, sid)
        capture = await run_in_threadpool(self.get, p, rid, token, sid)
        try:
            if saved is None and len(pcm) <= 30 * CHUNK_BYTES:
                wav = io.BytesIO()
                with wave.open(wav, "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(pcm)
                clip_request = VoiceRequest(
                    **body.model_dump(), audio_base64=base64.b64encode(wav.getvalue()).decode()
                )
                await VoiceService(self.manager, self.provider).create(
                    p,
                    rid,
                    token,
                    clip_request,
                    before_save=lambda: self.ensure_processing(p, rid, token, sid),
                    session_id=sid,
                    capture_complete=capture.capture_complete,
                )
            else:
                if saved is None:
                    if not isinstance(self.provider, Prisma) and not hasattr(
                        self.provider, "transcribe_stream"
                    ):
                        raise ProviderUnavailable("Realtime provider unavailable")
                    result = await self.provider.transcribe_stream(pcm, language=body.language)
                    fresh = await run_in_threadpool(self.manager.access.principal, token)
                    saved = await run_in_threadpool(
                        self.save_long,
                        fresh,
                        rid,
                        token,
                        sid,
                        body,
                        result.text,
                        result.segments,
                        pcm,
                        result.complete,
                    )
                voice = VoiceEvidence.model_validate(saved["voice"])
                if not voice.transcription_complete or len(voice.original_transcript) > 8000:
                    return await run_in_threadpool(
                        self.set_state, p, rid, token, sid, "needs_correction"
                    )
                request = DraftRequest.model_validate(saved["request"])
                fresh = await run_in_threadpool(self.manager.access.principal, token)
                await run_in_threadpool(
                    self.manager.create, fresh, rid, token, request, voice=voice
                )
            return await run_in_threadpool(self.set_state, p, rid, token, sid, "completed")
        except (ProviderUnavailable, ValueError) as exc:
            await run_in_threadpool(self.set_state, p, rid, token, sid, "uncertain")
            raise DomainError(
                "transcription_failed",
                "Transcription failed; retained audio was not silently replayed",
                503,
            ) from exc
        except DomainError:
            await run_in_threadpool(self.set_state, p, rid, token, sid, "uncertain")
            raise
