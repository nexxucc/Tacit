"""Provider seams. No invented Gnani endpoints or silent model substitutes."""

from typing import Protocol


class ProviderUnavailable(RuntimeError):
    pass


class SpeechToText(Protocol):
    async def transcribe(
        self, audio: bytes, sample_rate: int, *, language: str = "en-IN"
    ) -> str: ...


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class TextReasoner(Protocol):
    async def propose(self, transcript: str, source_context: str) -> str: ...


class UnconfiguredPrisma:
    async def transcribe(self, audio: bytes, sample_rate: int, *, language: str = "en-IN") -> str:
        raise ProviderUnavailable("Prisma transport is not configured")


class UnconfiguredTimbre:
    async def synthesize(self, text: str) -> bytes:
        raise ProviderUnavailable("Timbre transport is not configured")


class UnconfiguredEvon:
    async def propose(self, transcript: str, source_context: str) -> str:
        raise ProviderUnavailable("Evon self-hosted text transport is not configured")
