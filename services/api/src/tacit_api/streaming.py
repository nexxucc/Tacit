"""Bounded Gnani PCM streams; provider attempts are never automatically replayed."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from tacit_api.providers import ProviderUnavailable

logger = logging.getLogger("uvicorn.error.tacit.prisma")
SegmentCallback = Callable[[dict[str, str | int]], Awaitable[None]]
MAX_PCM = 895 * 32000


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
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM:
        raise ProviderUnavailable("Invalid or unconfigured realtime transcription")

    async def retained() -> AsyncIterator[bytes]:
        for offset in range(0, len(pcm), 32000):
            yield pcm[offset : offset + 32000]

    return await transcribe_chunks(
        key,
        retained(),
        language,
        connector=connector,
        pause=pause,
        drain_timeout=drain_timeout,
        idle_timeout=idle_timeout,
        total_timeout=total_timeout if total_timeout is not None else len(pcm) / 32000 + 120,
        live=False,
    )


async def transcribe_chunks(
    key: str,
    chunks: AsyncIterator[bytes],
    language: str,
    *,
    on_segment: SegmentCallback | None = None,
    connector: Any = connect,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    drain_timeout: float = 90,
    idle_timeout: float = 5,
    total_timeout: float = 1015,
    live: bool = True,
) -> StreamResult:
    """Send incoming audio once while returning validated provider segments.

    The provider emits completed VAD segments, not token-level hypotheses. Live
    mode caps an uninterrupted speech segment at five seconds. Pauses send no
    synthetic microphone audio; an idle provider can terminate the attempt.
    """
    if not key:
        raise ProviderUnavailable("Invalid or unconfigured realtime transcription")
    try:
        async with asyncio.timeout(total_timeout):
            return await _transcribe_chunks(
                key,
                chunks,
                language,
                on_segment=on_segment,
                connector=connector,
                pause=pause,
                drain_timeout=drain_timeout,
                idle_timeout=idle_timeout,
                live=live,
            )
    except TimeoutError as exc:
        logger.warning("Prisma realtime total deadline exceeded: seconds=%.1f", total_timeout)
        raise ProviderUnavailable("Realtime deadline exceeded; audio will not be replayed") from exc


async def _transcribe_chunks(
    key: str,
    chunks: AsyncIterator[bytes],
    language: str,
    *,
    on_segment: SegmentCallback | None,
    connector: Any,
    pause: Callable[[float], Awaitable[None]],
    drain_timeout: float,
    idle_timeout: float,
    live: bool,
) -> StreamResult:
    segments: dict[int, dict[str, str | int]] = {}
    ids: dict[str, int] = {}
    transcript_chars = 0
    processing = 0
    finished = asyncio.Event()
    headers = {
        "x-api-key-id": key,
        "lang_code": language,
        "x-sample-rate": "16000",
        "x-format": "verbatim",
        "x-enable-substitution": "false",
    }
    if live:
        headers.update({"x-max-speech-s": "5", "x-idle-timeout-sec": "120"})
    stage = "audio_source"
    iterator = aiter(chunks)
    try:
        # An empty/invalid source does not open a potentially billable provider socket.
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            raise ValueError("Invalid PCM source") from None

        def validate_chunk(chunk: bytes, total: int) -> None:
            if (
                not isinstance(chunk, bytes)
                or not chunk
                or len(chunk) % 2
                or len(chunk) > 32000
                or total + len(chunk) > MAX_PCM
            ):
                raise ValueError("Invalid PCM source")

        validate_chunk(first, 0)
        stage = "connect"
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
                not isinstance(ready, dict)
                or ready.get("type") != "connected"
                or not isinstance(ready.get("config"), dict)
                or ready["config"].get("sample_rate") != 16000
                or ready["config"].get("chunk_size") != 512
            ):
                raise ValueError("Unexpected provider handshake")

            async def receive() -> None:
                nonlocal processing, transcript_chars
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), idle_timeout)
                    except TimeoutError:
                        if finished.is_set() and len(segments) >= processing:
                            return
                        continue
                    event = json.loads(message)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid provider message")
                    if event.get("type") == "error":
                        raise ValueError("Provider error")
                    if event.get("type") == "processing":
                        processing += 1
                        if processing > 10000:
                            raise ValueError("Too many provider segments")
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
                        or event.get("is_final", True) is not True
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
                    if index in segments:
                        continue
                    transcript_chars += len(text) + (1 if segments else 0)
                    if transcript_chars > 120000:
                        raise ValueError("Transcript exceeds bounded session limit")
                    segments[index] = item
                    ids[sid] = index
                    if on_segment is not None:
                        # Consumer backpressure is bounded independently from provider drain.
                        await asyncio.wait_for(on_segment(dict(item)), 10)

            async def send() -> None:
                nonlocal stage
                stage = "audio_send"
                pending = bytearray()
                total = 0

                async def append(chunk: bytes) -> None:
                    nonlocal total
                    validate_chunk(chunk, total)
                    total += len(chunk)
                    pending.extend(chunk)
                    while len(pending) >= 1024:
                        await asyncio.wait_for(ws.send(bytes(pending[:1024])), 10)
                        del pending[:1024]
                        # Cadence regulates buffered one-second capture chunks; audio
                        # arrives concurrently and is never replayed after capture.
                        await pause(0.032)

                await append(first)
                async for chunk in iterator:
                    await append(chunk)
                # Pad only the final partial frame and flush VAD with final silence.
                if pending:
                    await asyncio.wait_for(ws.send(bytes(pending) + bytes(1024 - len(pending))), 10)
                    await pause(0.032)
                for _ in range(32):
                    await asyncio.wait_for(ws.send(bytes(1024)), 10)
                    await pause(0.032)
                finished.set()
                stage = "transcript_drain"
                logger.info("Prisma realtime audio sent; waiting for final transcripts")

            receiver = asyncio.create_task(receive())
            sender = asyncio.create_task(send())
            try:
                done, _ = await asyncio.wait(
                    {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done and not sender.done():
                    await receiver
                    raise ValueError("Provider ended before all audio was delivered")
                await sender
                await asyncio.wait_for(receiver, drain_timeout)
            finally:
                for task in (sender, receiver):
                    if not task.done():
                        task.cancel()
                # Retrieve both errors, including on caller cancellation, to avoid
                # abandoned receive/source tasks and misleading unhandled errors.
                await asyncio.gather(sender, receiver, return_exceptions=True)
        stage = "segment_validation"
        if not segments:
            raise ValueError("Empty or missing transcript segments")
        indexes = sorted(segments)
        # Current Gnani uses 1-based segment indexes. Retain the legacy 0-based
        # fixtures/provider format without rewriting source evidence indexes.
        start = 0 if indexes[0] == 0 else 1
        complete = indexes == list(range(start, start + len(segments)))
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
            "Invalid PCM source",
            "Invalid provider message",
            "Too many provider segments",
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
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with suppress(Exception):
                await asyncio.wait_for(close(), 5)
