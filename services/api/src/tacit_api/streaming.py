"""Gnani realtime framing for retained PCM; no automatic provider reconnection."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from tacit_api.providers import ProviderUnavailable

logger = logging.getLogger("uvicorn.error.tacit.prisma")


@dataclass(frozen=True)
class StreamResult:
    text: str
    segments: tuple[dict[str, str | int], ...]
    complete: bool = True


async def transcribe_pcm(
    key: str,
    pcm: bytes,
    language: str,
    *,
    connector: Any = connect,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    drain_timeout: float = 90,
    idle_timeout: float = 5,
    total_timeout: float | None = None,
) -> StreamResult:
    # The drain deadline alone doesn't bound a stalled send or websocket shutdown.
    deadline = total_timeout if total_timeout is not None else len(pcm) / 32000 + 120
    try:
        async with asyncio.timeout(deadline):
            return await _transcribe_pcm(
                key,
                pcm,
                language,
                connector=connector,
                pause=pause,
                drain_timeout=drain_timeout,
                idle_timeout=idle_timeout,
            )
    except TimeoutError as exc:
        logger.warning("Prisma realtime total deadline exceeded: seconds=%.1f", deadline)
        raise ProviderUnavailable("Realtime deadline exceeded; audio will not be replayed") from exc


async def _transcribe_pcm(
    key: str,
    pcm: bytes,
    language: str,
    *,
    connector: Any,
    pause: Callable[[float], Awaitable[None]],
    drain_timeout: float,
    idle_timeout: float,
) -> StreamResult:
    if not key or not pcm or len(pcm) % 2 or len(pcm) > 895 * 32000:
        raise ProviderUnavailable("Invalid or unconfigured realtime transcription")
    segments: dict[int, dict[str, str | int]] = {}
    ids: dict[str, int] = {}
    processing = 0
    finished = asyncio.Event()
    headers = {
        "x-api-key-id": key,
        "lang_code": language,
        "x-sample-rate": "16000",
        "x-format": "verbatim",
        "x-enable-substitution": "false",
    }
    stage = "connect"
    try:
        async with connector(
            "wss://api.vachana.ai/stt/v3/stream",
            additional_headers=headers,
            open_timeout=15,
            max_size=262144,
            max_queue=16,
            proxy=None,
            close_timeout=5,
        ) as ws:
            stage = "handshake"
            logger.info("Prisma realtime connected; waiting for handshake")
            ready = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if (
                ready.get("type") != "connected"
                or ready.get("config", {}).get("sample_rate") != 16000
                or ready.get("config", {}).get("chunk_size") != 512
            ):
                raise ValueError("Unexpected provider handshake")

            async def receive() -> None:
                nonlocal processing
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), idle_timeout)
                    except TimeoutError:
                        if finished.is_set() and len(segments) >= processing:
                            return
                        continue
                    event = json.loads(message)
                    if event.get("type") == "error":
                        raise ValueError("Provider error")
                    if event.get("type") == "processing":
                        processing += 1
                    if event.get("type") != "transcript":
                        continue
                    index, sid, text = (
                        event.get("segment_index"),
                        event.get("segment_id"),
                        event.get("text"),
                    )
                    if (
                        type(index) is not int
                        or not 0 <= index < 10000
                        or not isinstance(sid, str)
                        or not sid
                        or len(sid) > 256
                        or not isinstance(text, str)
                        or not text.strip()
                    ):
                        raise ValueError("Invalid transcript segment")
                    item: dict[str, str | int] = {
                        "segment_index": index,
                        "segment_id": sid,
                        "text": text,
                    }
                    if (index in segments and segments[index] != item) or (
                        sid in ids and ids[sid] != index
                    ):
                        raise ValueError("Conflicting transcript segment")
                    segments[index] = item
                    ids[sid] = index
                    if sum(len(str(s["text"])) for s in segments.values()) > 120000:
                        raise ValueError("Transcript exceeds bounded session limit")

            receiver = asyncio.create_task(receive())
            try:
                stage = "audio_send"
                logger.info("Prisma realtime sending saved audio: seconds=%.2f", len(pcm) / 32000)
                # Exact provider framing; padding and final silence are never source evidence.
                framed = pcm + bytes((-len(pcm)) % 1024) + bytes(1024 * 32)
                for offset in range(0, len(framed), 1024):
                    if receiver.done():
                        await receiver
                        raise ValueError("Provider ended before all audio was delivered")
                    await asyncio.wait_for(ws.send(framed[offset : offset + 1024]), 10)
                    await pause(0.032)
                finished.set()
                stage = "transcript_drain"
                logger.info("Prisma realtime audio sent; waiting for final transcripts")
                await asyncio.wait_for(receiver, drain_timeout)
            finally:
                if not receiver.done():
                    receiver.cancel()
                    try:
                        await receiver
                    except asyncio.CancelledError:
                        pass
        stage = "segment_validation"
        if not segments:
            raise ValueError("Empty or missing transcript segments")
        indexes = sorted(segments)
        complete = indexes == list(range(len(segments)))
        if not complete:
            logger.warning(
                "Prisma realtime transcript retained for correction: segment_indexes=%s processing=%d",
                indexes,
                processing,
            )
        ordered = tuple(segments[i] for i in indexes)
        logger.info(
            "Prisma realtime received transcript: segments=%d complete=%s", len(ordered), complete
        )
        return StreamResult("\n".join(str(s["text"]) for s in ordered), ordered, complete)
    except (
        OSError,
        ValueError,
        TimeoutError,
        RuntimeError,
        WebSocketException,
        AttributeError,
        TypeError,
    ) as exc:
        # Exception text, close reasons and provider payloads may contain private data.
        reason = str(exc) if type(exc) is ValueError else "transport_or_protocol"
        if reason not in {
            "Unexpected provider handshake",
            "Provider error",
            "Invalid transcript segment",
            "Conflicting transcript segment",
            "Transcript exceeds bounded session limit",
            "Provider ended before all audio was delivered",
            "Empty or missing transcript segments",
        }:
            reason = "transport_or_protocol"
        status = getattr(getattr(exc, "response", None), "status_code", None)
        status = status if type(status) is int else None
        logger.warning(
            "Prisma realtime failed: stage=%s exception=%s reason=%s http_status=%s "
            "segments=%d processing=%d",
            stage,
            type(exc).__name__,
            reason,
            status,
            len(segments),
            processing,
        )
        raise ProviderUnavailable(
            "Realtime transcription failed; it will not be automatically replayed"
        ) from exc
