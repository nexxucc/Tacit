"""One-shot live transcription transport; previews are never authoritative evidence."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from tacit_contracts.models import VoiceFinishRequest, VoiceSessionView
from tacit_domain.access import DomainError, Principal

from tacit_api.providers import ProviderUnavailable
from tacit_api.voice_sessions import CHUNK_BYTES, VoiceSessions

logger = logging.getLogger(__name__)


async def live_capture(socket: WebSocket, manager: VoiceSessions, rid: str, sid: str) -> None:
    """Durably bind PCM before forwarding it; a disconnected attempt is never replayed."""
    token = socket.headers.get("Authorization", "").removeprefix("Bearer ")
    p: Principal | None = None
    reserved = False
    accepted = False
    completed = False
    tasks: list[asyncio.Task[Any]] = []
    sends = asyncio.Lock()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4)
    finalized = False

    async def authorized(*, statuses: set[str] | None = None) -> Principal:
        fresh = await run_in_threadpool(manager.manager.access.principal, token)
        view = await run_in_threadpool(manager.get, fresh, rid, token, sid)
        allowed = statuses if statuses is not None else {"processing"}
        if view.status not in allowed:
            raise DomainError("voice_state", "Live attempt is no longer active", 409)
        return fresh

    async def send(event: dict[str, object]) -> None:
        # Serialize ACKs and previews; refresh authorization immediately before
        # each disclosure and never include provider errors/raw payloads.
        async with sends:
            statuses = (
                {"completed", "needs_correction"}
                if event.get("type") == "result"
                else {"processing", "uncertain"}
                if event.get("type") == "error"
                else {"processing"}
            )
            await authorized(statuses=statuses)
            if len(json.dumps(event).encode("utf-8")) > 900_000:
                raise DomainError(
                    "transcript_too_large", "Live response exceeds transport limit", 413
                )
            await asyncio.wait_for(socket.send_json(event), 10)

    async def chunks() -> AsyncIterator[bytes]:
        while True:
            data = await queue.get()
            if data is None:
                return
            fresh = await authorized()
            await run_in_threadpool(manager.ensure_processing, fresh, rid, token, sid)
            yield data

    async def segment(item: dict[str, str | int]) -> None:
        # The provider adapter validates and deduplicates segments. They are
        # transient previews until the final immutable result passes validation.
        await send({"type": "transcript", "segment": item})

    async def receive() -> None:
        nonlocal finalized
        while True:
            message = await asyncio.wait_for(socket.receive(), 20)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            packet = message.get("bytes")
            if packet is not None:
                if finalized:
                    raise DomainError("voice_state", "Capture was already finalized", 409)
                if not isinstance(packet, bytes) or not 8 < len(packet) <= CHUNK_BYTES + 8:
                    raise DomainError("invalid_audio_chunk", "Invalid live PCM packet", 422)
                fresh = await authorized()
                view = await run_in_threadpool(
                    manager.append,
                    fresh,
                    rid,
                    token,
                    sid,
                    int.from_bytes(packet[:8], "big"),
                    packet[8:],
                    live=True,
                )
                # A bounded queue supplies provider backpressure; acknowledged
                # bytes remain durable even if the provider subsequently fails.
                await asyncio.wait_for(queue.put(packet[8:]), 10)
                await send({"type": "ack", "chunks": view.chunks})
                continue
            text = message.get("text")
            if not isinstance(text, str) or len(text) > 1024:
                raise DomainError("invalid_live_event", "Invalid live control event", 422)
            event = json.loads(text)
            if not isinstance(event, dict):
                raise DomainError("invalid_live_event", "Invalid live control event", 422)
            if event == {"type": "ping"}:
                await send({"type": "pong"})
            elif event.get("type") == "finish" and not finalized:
                finish = VoiceFinishRequest.model_validate(
                    {key: value for key, value in event.items() if key != "type"}
                )
                fresh = await authorized()
                await run_in_threadpool(manager.finish_live, fresh, rid, token, sid, finish)
                finalized = True
                await asyncio.wait_for(queue.put(None), 10)
                # Keep this sole reader alive during final provider drain so
                # disconnect/cancel can stop the provider promptly.
            else:
                raise DomainError("invalid_live_event", "Invalid live control event", 422)

    async def transcribe(body: Any) -> VoiceSessionView:
        await authorized()
        result = await manager.provider.transcribe_live(  # type: ignore[attr-defined]
            chunks(), language=body.language, on_segment=segment
        )
        if not finalized:
            raise ProviderUnavailable("Live provider completed before capture was finalized")
        fresh = await authorized()
        pcm = await run_in_threadpool(manager.live_pcm, fresh, rid, token, sid)
        await run_in_threadpool(
            manager.save_long,
            fresh,
            rid,
            token,
            sid,
            body,
            result.text,
            result.segments,
            pcm,
            result.complete,
            reserved_live=True,
        )
        # This resumes from the just-saved immutable transcript, never invoking
        # a provider again. Existing correction/draft/human-confirmation rules apply.
        fresh = await authorized()
        return await manager.transcribe(fresh, rid, token, sid)

    try:
        if not callable(getattr(manager.provider, "transcribe_live", None)):
            raise DomainError(
                "live_unavailable", "Provider does not support live transcription", 503
            )
        if hasattr(manager.provider, "key") and not manager.provider.key:
            raise DomainError("provider_unconfigured", "Live provider is not configured", 503)
        p = await run_in_threadpool(manager.manager.access.principal, token)
        body, view = await run_in_threadpool(manager.reserve_live, p, rid, token, sid)
        reserved = True
        await socket.accept()
        accepted = True
        await send({"type": "session", "session": view.model_dump(mode="json")})
        # Includes capture pauses and final drain; no unbounded billable session.
        async with asyncio.timeout(1030):
            receiver = asyncio.create_task(receive())
            provider = asyncio.create_task(transcribe(body))
            tasks.extend((receiver, provider))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Prefer disconnect/protocol failure even if both tasks finish together.
            if receiver in done:
                await receiver
                raise DomainError("voice_state", "Live capture ended unexpectedly", 409)
            result = await provider
            await send({"type": "result", "session": result.model_dump(mode="json")})
            completed = True
            await socket.close(code=1000)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except (
        DomainError,
        ProviderUnavailable,
        ValidationError,
        ValueError,
        TimeoutError,
        OSError,
        RuntimeError,
    ):
        # Never echo exception text: SDK/provider payloads may contain transcripts
        # or credentials. An attempted operation stays reserved even on failure.
        if accepted:
            with suppress(Exception):
                await send({"type": "error", "code": "live_transcription_failed"})
            with suppress(Exception):
                await socket.close(code=1008)
        else:
            with suppress(Exception):
                await socket.close(code=1008)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if reserved and not completed and p is not None:
            try:
                await run_in_threadpool(manager.abandon_live, p.user_id, rid, sid)
            except Exception:
                # Retention still cleans a processing row after a storage outage.
                logger.warning("Live attempt cleanup unavailable; retained session will expire")
