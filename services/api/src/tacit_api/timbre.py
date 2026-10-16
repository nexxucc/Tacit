"""Explicit human clarification playback, with an at-most-once synthesis reservation."""

import asyncio
import io
import wave

import httpx
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from tacit_contracts.models import SpeakRequest
from tacit_domain.access import DomainError, Principal

from tacit_api.artifacts import Artifacts
from tacit_api.db import ModelOperation
from tacit_api.publication import fingerprint


class Timbre:
    def __init__(self, key: str, artifacts: Artifacts):
        self.key, self.artifacts = key, artifacts

    def reserve(
        self, p: Principal, rid: str, token: str, body: SpeakRequest
    ) -> dict[str, object] | None:
        manager = self.artifacts.manager
        with Session(manager.engine) as db, db.begin():
            repo = manager.write_repository(db, p, rid, token)
            key = (repo.workspace_id, rid, p.user_id, body.operation_id, "tts")
            prior = db.get(ModelOperation, key)
            hashed = fingerprint(body.model_dump(mode="json"))
            if prior:
                if prior.request_digest != hashed:
                    raise DomainError("idempotency_conflict", "Synthesis input changed", 409)
                if prior.status != "completed":
                    raise DomainError(
                        "model_outcome_uncertain",
                        "Synthesis was attempted; it will not be replayed",
                        409,
                    )
                return prior.result
            db.add(
                ModelOperation(
                    workspace_id=repo.workspace_id,
                    repository_id=rid,
                    author_id=p.user_id,
                    operation_id=body.operation_id,
                    kind="tts",
                    request_digest=hashed,
                    status="processing",
                )
            )
        return None

    async def speak(
        self, p: Principal, rid: str, token: str, body: SpeakRequest
    ) -> dict[str, object]:
        if not self.key:
            raise DomainError("provider_unconfigured", "Timbre is not configured", 503)
        prior = await run_in_threadpool(self.reserve, p, rid, token, body)
        if prior is not None:
            return prior
        try:
            async with asyncio.timeout(90):
                async with httpx.AsyncClient(timeout=85, trust_env=False) as client:
                    async with client.stream(
                        "POST",
                        "https://api.vachana.ai/api/v1/tts/inference",
                        headers={"X-API-Key-ID": self.key},
                        json={
                            "text": body.text,
                            "voice": body.voice,
                            "model": "timbre-v2.5",
                            "language": body.language,
                            "audio_config": {
                                "encoding": "linear_pcm",
                                "container": "wav",
                                "sample_rate": 48000,
                                "num_channels": 1,
                                "sample_width": 2,
                            },
                        },
                    ) as response:
                        response.raise_for_status()
                        audio = bytearray()
                        async for chunk in response.aiter_bytes():
                            audio.extend(chunk)
                            if len(audio) > 20000000:
                                raise ValueError("Audio exceeds budget")
            with wave.open(io.BytesIO(audio), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 48000):
                    raise ValueError("Invalid output format")
            fresh = await run_in_threadpool(self.artifacts.manager.access.principal, token)
            result = await run_in_threadpool(self.artifacts.create, fresh, rid, token, bytes(audio))
            result = {**result, "usage": {"tts_calls": 1}}
            await run_in_threadpool(self.finish, fresh, rid, token, body, result)
            return result
        except (httpx.HTTPError, TimeoutError, ValueError, wave.Error) as exc:
            raise DomainError(
                "synthesis_failed",
                "Synthesis failed; this operation will not be automatically replayed",
                503,
            ) from exc

    def finish(
        self, p: Principal, rid: str, token: str, body: SpeakRequest, result: dict[str, object]
    ) -> None:
        manager = self.artifacts.manager
        with Session(manager.engine) as db, db.begin():
            repo = manager.write_repository(db, p, rid, token)
            item = db.get(
                ModelOperation, (repo.workspace_id, rid, p.user_id, body.operation_id, "tts")
            )
            assert item is not None
            item.status, item.result = "completed", result
