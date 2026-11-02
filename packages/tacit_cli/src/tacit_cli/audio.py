"""Native PortAudio capture; callbacks never perform network or filesystem work."""

import importlib
import io
import platform
import queue
import select
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from typing import Any


class AudioError(RuntimeError):
    pass


class RecordingCancelled(AudioError):
    pass


def backend() -> Any:
    if platform.system() not in {"Linux", "Darwin", "Windows"}:
        raise AudioError(
            "Native audio supports Linux, macOS and Windows; use file input on this platform"
        )
    try:
        return importlib.import_module("sounddevice")
    except (ImportError, OSError) as exc:
        raise AudioError(audio_setup_hint()) from exc


def audio_setup_hint() -> str:
    """Installation guidance without importing or opening an audio device."""
    system = platform.system()
    if system == "Windows":
        return (
            "Install the audio extra (uv sync --extra audio). Native Windows sounddevice "
            "wheels include PortAudio; use a supported Python/Windows architecture."
        )
    if system == "Darwin":
        return (
            "Install the audio extra (uv sync --extra audio). macOS sounddevice wheels "
            "include PortAudio; if unavailable, install PortAudio with your package manager "
            "and use a Python build matching the Mac architecture."
        )
    return (
        "Install PortAudio using your Linux package manager "
        "(Arch: sudo pacman -S portaudio), then uv sync --extra audio"
    )


def device_access_hint(*, recording: bool) -> str:
    system = platform.system()
    if system == "Windows":
        if recording:
            return (
                "Check the selected input and Windows Settings > Privacy & security > "
                "Microphone; allow microphone access for desktop apps and your terminal. "
                "Run native Windows Python for Windows audio devices."
            )
        return "Check the selected output and Windows Settings > System > Sound."
    if system == "Darwin":
        if recording:
            return (
                "Check the selected input and System Settings > Privacy & Security > "
                "Microphone; allow access for the terminal or IDE hosting Tacit, then "
                "restart that application if required."
            )
        return "Check the selected output in System Settings > Sound."
    return "Check the selected device, PipeWire/PulseAudio/ALSA and device access."


class RecordingControls:
    """Poll the current terminal without a background reader surviving capture.

    POSIX terminals provide line input through select(). Windows select() only
    supports sockets, so its console is read one available wide character at a
    time. Both paths keep the same p/r + Enter and empty Enter commands.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stdin is not None and sys.stdin.isatty()
        self.windows = platform.system() == "Windows"
        self.line: list[str] = []
        self.extended = False
        self.skip_lf = False

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        try:
            if not self.windows:
                if select.select([sys.stdin], [], [], 0)[0]:
                    return sys.stdin.readline().strip().lower()
                return None
            console = importlib.import_module("msvcrt")
            while console.kbhit():
                char = console.getwch()
                if self.extended:
                    self.extended = False
                    continue
                if char in {"\x00", "\xe0"}:
                    self.extended = True
                    continue
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\n" and self.skip_lf:
                    self.skip_lf = False
                    continue
                self.skip_lf = False
                if char in {"\r", "\n"}:
                    self.skip_lf = char == "\r"
                    value = "".join(self.line).strip().lower()
                    self.line.clear()
                    print(file=sys.stderr, flush=True)
                    return value
                if char == "\b":
                    if self.line:
                        self.line.pop()
                        print("\b \b", end="", file=sys.stderr, flush=True)
                elif char.isprintable() and len(self.line) < 128:
                    self.line.append(char)
                    print(char, end="", file=sys.stderr, flush=True)
            return None
        except (ImportError, OSError, ValueError) as exc:
            raise AudioError(
                "Cannot read recording controls from this terminal; use a native terminal "
                "or provide an audio file instead"
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
            "Cannot enumerate audio devices. " + device_access_hint(recording=True)
        ) from exc


def output_devices() -> list[dict[str, object]]:
    """List playback devices separately; input and output defaults can differ."""
    try:
        sd = backend()
        default = int(sd.default.device[1])
        return [
            {
                "id": i,
                "name": str(item["name"]),
                "output_channels": int(item["max_output_channels"]),
                "default": i == default,
            }
            for i, item in enumerate(sd.query_devices())
            if item["max_output_channels"] > 0
        ]
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Cannot enumerate output devices. " + device_access_hint(recording=False)
        ) from exc


def record(
    seconds: float = 30,
    device: int | None = None,
    *,
    sd: Any = None,
    interactive: bool = True,
    on_state: Callable[[str], None] | None = None,
) -> bytes:
    if not 0 < seconds <= 60:
        raise AudioError("Recording duration must be between 0 and 60 seconds")
    pcm = b"".join(
        capture_chunks(seconds, device, sd=sd, interactive=interactive, on_state=on_state)
    )
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
    seconds: float,
    device: int | None = None,
    *,
    sd: Any = None,
    interactive: bool = True,
    on_state: Callable[[str], None] | None = None,
) -> "Iterator[bytes]":
    """Yield bounded PCM chunks; paused wall time and microphone input are excluded."""
    if not 0 < seconds <= 895:
        raise AudioError("Recording duration must be between 0 and 895 seconds")
    sd = sd if sd is not None else backend()
    queued: queue.Queue[bytes] = queue.Queue(maxsize=64)
    failed = threading.Event()
    remaining = int(seconds * 16000) * 2
    pending = bytearray()
    stream = None
    accepting = threading.Event()
    accepting.set()
    controls = RecordingControls(interactive)

    def callback(data: Any, frames: int, timing: Any, status: Any) -> None:
        # Gate first, including status flags emitted as a stopped stream settles.
        if not accepting.is_set():
            return
        if status:
            failed.set()
            return
        try:
            queued.put_nowait(bytes(data))
        except queue.Full:
            failed.set()

    def state(value: str) -> None:
        if on_state is not None:
            on_state(value)
        elif interactive:
            message = (
                "Recording paused; r + Enter resumes, Enter finishes, Ctrl+C cancels."
                if value == "paused"
                else "Recording resumed; p + Enter pauses, Enter finishes, Ctrl+C cancels."
            )
            print(message, file=sys.stderr, flush=True)

    def append(data: bytes) -> None:
        nonlocal remaining
        if not data or len(data) % 2:
            raise AudioError("Invalid PCM16 frame")
        part = data[:remaining]
        pending.extend(part)
        remaining -= len(part)

    try:
        try:
            sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=16000)
        except Exception as exc:
            raise AudioError(
                "The selected input cannot open mono PCM16 at 16000 Hz. "
                + device_access_hint(recording=True)
                + " Choose another input with audio devices, or provide a mono 16 kHz "
                "PCM16 WAV using --audio-file."
            ) from exc
        stream = sd.RawInputStream(
            device=device,
            channels=1,
            dtype="int16",
            samplerate=16000,
            blocksize=1600,
            callback=callback,
        )
        with stream:
            active_started = time.monotonic()
            active_elapsed = 0.0
            last_frame = active_started
            paused = False
            while remaining > 0:
                elapsed = active_elapsed + (0.0 if paused else time.monotonic() - active_started)
                if elapsed >= seconds:
                    break
                if failed.is_set():
                    raise AudioError(
                        "Capture overflow/device loss; retained recording is incomplete"
                    )
                control = controls.poll()
                if control == "":
                    break
                if control == "p" and not paused:
                    # Freeze the active clock and stop the physical input before yielding
                    # pre-pause buffers to the spool. Callbacks after this boundary discard data.
                    active_elapsed += time.monotonic() - active_started
                    accepting.clear()
                    stream.stop()
                    paused = True
                    while True:
                        try:
                            append(queued.get_nowait())
                        except queue.Empty:
                            break
                    state("paused")
                elif control == "r" and paused:
                    # A backend must never replay stale microphone buffers after a pause.
                    while True:
                        try:
                            queued.get_nowait()
                        except queue.Empty:
                            break
                    active_started = time.monotonic()
                    last_frame = active_started
                    accepting.set()
                    stream.start()
                    paused = False
                    state("recording")
                elif control not in {None, "p", "r"}:
                    print(
                        "Use p + Enter to pause, r + Enter to resume, or Enter to finish.",
                        file=sys.stderr,
                        flush=True,
                    )
                if not paused:
                    try:
                        append(queued.get(timeout=0.05))
                        last_frame = time.monotonic()
                    except queue.Empty:
                        if time.monotonic() - last_frame > 3:
                            raise AudioError(
                                "Microphone stopped delivering frames; retained recording is incomplete"
                            ) from None
                while len(pending) >= 32000:
                    yield bytes(pending[:32000])
                    del pending[:32000]
                if paused:
                    time.sleep(0.05)
            accepting.clear()
            # Enter or the deadline can arrive with callback frames still queued.
            # Stop input before draining to retain the final words without adding
            # microphone data after the capture boundary.
            stop = getattr(stream, "stop", None)
            if not paused and callable(stop):
                stop()
            while True:
                try:
                    append(queued.get_nowait())
                except queue.Empty:
                    break
            if failed.is_set():
                raise AudioError("Capture overflow/device loss; retained recording is incomplete")
            while len(pending) >= 32000:
                yield bytes(pending[:32000])
                del pending[:32000]
            if pending:
                yield bytes(pending)
    except KeyboardInterrupt as exc:
        raise RecordingCancelled("Recording cancelled") from exc
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Capture failed; retained recording is incomplete. "
            + device_access_hint(recording=True)
            + " Select an input supporting mono PCM16 at 16000 Hz with audio devices."
        ) from exc
    finally:
        accepting.clear()
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def play(audio: bytes, device: int | None = None) -> None:
    """Explicit PCM WAV playback after capture has stopped."""
    import io
    import wave

    stream = None
    try:
        sd = backend()

        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise AudioError("Playback requires PCM16 WAV")
            stream = sd.RawOutputStream(
                device=device,
                samplerate=wav.getframerate(),
                channels=wav.getnchannels(),
                dtype="int16",
            )
            with stream as output:
                remaining = wav.getnframes()
                if not remaining:
                    raise AudioError("Playback audio is empty")
                while remaining:
                    frames = min(remaining, 4096)
                    data = wav.readframes(frames)
                    if len(data) != frames * wav.getnchannels() * 2:
                        raise AudioError("Playback audio is truncated")
                    output.write(data)
                    remaining -= frames
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError(
            "Audio playback unavailable on the selected output device. "
            + device_access_hint(recording=False)
            + " "
            + audio_setup_hint()
        ) from exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
