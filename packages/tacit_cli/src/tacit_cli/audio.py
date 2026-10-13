"""Arch/PortAudio capture: callback never performs network or filesystem work."""

import importlib
import io
import platform
import queue
import select
import sys
import threading
import time
import wave
from collections.abc import Iterator
from typing import Any


class AudioError(RuntimeError):
    pass


class RecordingCancelled(AudioError):
    pass


def backend() -> Any:
    if platform.system() != "Linux":
        raise AudioError(
            "Microphone capture currently supports Arch Linux only; file input remains available"
        )
    try:
        return importlib.import_module("sounddevice")
    except (ImportError, OSError) as exc:
        raise AudioError(
            "Install Arch portaudio (sudo pacman -S portaudio), then uv sync --extra audio"
        ) from exc


def devices() -> list[dict[str, object]]:
    try:
        sd = backend()
        default = int(sd.default.device[0])
        return [
            {
                "id": i,
                "name": str(d["name"]),
                "input_channels": int(d["max_input_channels"]),
                "default": i == default,
            }
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0
        ]
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Cannot enumerate audio devices; check PipeWire/PulseAudio and microphone access"
        ) from exc


def record(
    seconds: float = 30, device: int | None = None, *, sd: Any = None, interactive: bool = True
) -> bytes:
    if not 0 < seconds <= 60:
        raise AudioError("Recording duration must be between 0 and 60 seconds")
    sd = sd if sd is not None else backend()
    chunks: queue.Queue[bytes] = queue.Queue(maxsize=64)
    failed = threading.Event()
    maximum = int(seconds * 16000) * 2
    pcm = bytearray()

    def callback(data: Any, frames: int, timing: Any, status: Any) -> None:
        if status:
            failed.set()
            return
        try:
            chunks.put_nowait(bytes(data))
        except queue.Full:
            failed.set()

    stream = None
    try:
        sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=16000)
        stream = sd.RawInputStream(
            device=device,
            channels=1,
            dtype="int16",
            samplerate=16000,
            blocksize=1600,
            callback=callback,
        )
        with stream:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and len(pcm) < maximum:
                if failed.is_set():
                    raise AudioError("Audio overflow/device error; incomplete recording discarded")
                try:
                    pcm.extend(chunks.get(timeout=0.05)[: maximum - len(pcm)])
                except queue.Empty:
                    pass
                if interactive and sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                    break
            if failed.is_set():
                raise AudioError("Audio overflow/device error; incomplete recording discarded")
    except KeyboardInterrupt as exc:
        raise RecordingCancelled("Recording cancelled; no audio uploaded") from exc
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Microphone capture failed; select an input with tacit audio devices"
        ) from exc
    finally:
        if stream is not None:
            # Also close a stream whose __enter__/start was interrupted.
            try:
                stream.close()
            except Exception:
                pass
    if not pcm:
        raise AudioError("No audio captured")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()


def capture_chunks(
    seconds: float, device: int | None = None, *, sd: Any = None, interactive: bool = True
) -> "Iterator[bytes]":
    """Yield one-second PCM chunks outside the callback; bounded independently of session length."""
    if not 0 < seconds <= 895:
        raise AudioError("Recording duration must be between 0 and 895 seconds")
    sd = sd if sd is not None else backend()
    queued: queue.Queue[bytes] = queue.Queue(maxsize=64)
    failed = threading.Event()
    remaining = int(seconds * 16000) * 2
    pending = bytearray()
    stream = None

    def callback(data: Any, frames: int, timing: Any, status: Any) -> None:
        if status:
            failed.set()
            return
        try:
            queued.put_nowait(bytes(data))
        except queue.Full:
            failed.set()

    try:
        sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=16000)
        stream = sd.RawInputStream(
            device=device,
            channels=1,
            dtype="int16",
            samplerate=16000,
            blocksize=1600,
            callback=callback,
        )
        with stream:
            deadline = time.monotonic() + seconds
            last_frame = time.monotonic()
            while remaining > 0 and time.monotonic() < deadline:
                if failed.is_set():
                    raise AudioError(
                        "Capture overflow/device loss; retained recording is incomplete"
                    )
                try:
                    data = queued.get(timeout=0.05)[:remaining]
                    if len(data) % 2:
                        raise AudioError("Invalid PCM16 frame")
                    last_frame = time.monotonic()
                    pending.extend(data)
                    remaining -= len(data)
                    while len(pending) >= 32000:
                        yield bytes(pending[:32000])
                        del pending[:32000]
                except queue.Empty:
                    if time.monotonic() - last_frame > 3:
                        raise AudioError(
                            "Microphone stopped delivering frames; retained recording is incomplete"
                        ) from None
                if interactive and sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                    break
            if failed.is_set():
                raise AudioError("Capture overflow/device loss; retained recording is incomplete")
            if pending:
                yield bytes(pending)
    except KeyboardInterrupt as exc:
        raise RecordingCancelled(
            "Recording cancelled; captured audio has not been transcribed"
        ) from exc
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError("Capture failed; retained recording is incomplete") from exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
