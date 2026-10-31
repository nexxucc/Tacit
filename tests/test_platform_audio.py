"""Device-free adversarial native audio/storage coverage; no live keyring/provider calls."""

import io
import os
import stat
import sys
from collections import deque
from types import SimpleNamespace

import pytest
from tacit_cli import audio, local_storage, spool


class Input:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.stops = 0
        self.starts = 0

    def check_input_settings(self, **kwargs):
        self.settings = kwargs

    def RawInputStream(self, **kwargs):
        self.callback = kwargs["callback"]
        return self

    def __enter__(self):
        for data in self.chunks:
            self.callback(data, len(data) // 2, None, False)
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def stop(self):
        self.stops += 1
        # A late callback from a stopped device must not enter the capture.
        self.callback(b"P\0" * 1600, 1600, None, False)

    def start(self):
        self.starts += 1
        self.callback(b"B\0" * 1600, 1600, None, False)


def controls(monkeypatch, commands):
    commands = iter(commands)
    monkeypatch.setattr(
        audio, "RecordingControls", lambda enabled: SimpleNamespace(poll=lambda: next(commands))
    )


def test_enter_preserves_queued_audio_and_bounded_final_chunks(monkeypatch):
    source = Input([b"A\0" * 1600] * 11)
    controls(monkeypatch, [""])
    parts = list(audio.capture_chunks(2, sd=source))
    assert b"".join(parts) == b"A\0" * (1600 * 11)
    assert all(0 < len(part) <= 32000 for part in parts)
    assert source.closed


def test_duration_boundary_preserves_captured_frames(monkeypatch):
    source = Input([b"A\0" * 1600])
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(audio.time, "monotonic", lambda: next(ticks))
    assert b"".join(audio.capture_chunks(0.1, sd=source, interactive=False)) == b"A\0" * 1600
    assert source.closed


def test_pause_excludes_wall_time_and_late_callback_audio(monkeypatch):
    source = Input([b"A\0" * 1600])
    clock = [0.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audio.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 100))
    controls(monkeypatch, [None, "p", None, "r", ""])
    states = []
    result = b"".join(audio.capture_chunks(1, sd=source, on_state=states.append))
    assert result == b"A\0" * 1600 + b"B\0" * 1600
    assert states == ["paused", "recording"]
    assert clock[0] >= 200 and source.starts == 1 and source.closed


def test_windows_console_commands_do_not_call_select(monkeypatch):
    chars = deque("px\b\r\n\xe0Kr\r\x03")
    console = SimpleNamespace(kbhit=lambda: bool(chars), getwch=lambda: chars.popleft())
    monkeypatch.setattr(audio.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(audio.importlib, "import_module", lambda name: console)
    monkeypatch.setattr(audio.select, "select", lambda *args: pytest.fail("Windows stdin select"))
    commands = audio.RecordingControls(True)
    assert commands.poll() == "p"
    assert commands.poll() == "r"
    with pytest.raises(KeyboardInterrupt):
        commands.poll()


def test_no_stdin_disables_interactive_controls(monkeypatch):
    monkeypatch.setattr(audio.sys, "stdin", None)
    assert audio.RecordingControls(True).poll() is None


@pytest.mark.parametrize("platform", ["Windows", "Darwin", "Linux"])
def test_native_audio_backend_and_setup_guidance(monkeypatch, platform):
    monkeypatch.setattr(audio.platform, "system", lambda: platform)
    sentinel = object()
    monkeypatch.setattr(audio.importlib, "import_module", lambda name: sentinel)
    assert audio.backend() is sentinel
    assert "audio extra" in audio.audio_setup_hint() or "PortAudio" in audio.audio_setup_hint()
    assert audio.device_access_hint(recording=True)


def test_output_selection_and_playback_pcm(monkeypatch):
    written, settings = [], []

    class Output:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            written.append("closed")

        def write(self, value):
            written.append(value)

    def output(**kwargs):
        settings.append(kwargs)
        return Output()

    monkeypatch.setattr(audio, "backend", lambda: SimpleNamespace(RawOutputStream=output))
    import wave

    wav = io.BytesIO()
    with wave.open(wav, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(16000)
        file.writeframes(b"a\0" * 100)
    audio.play(wav.getvalue(), device=7)
    assert settings[0]["device"] == 7
    assert written == [b"a\0" * 100, "closed"]


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_native_paths_overrides_and_legacy_preservation(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(local_storage.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(local_storage.sys, "platform", platform)
    for key in (
        "TACIT_CONFIG",
        "TACIT_VOICE_SPOOL",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        monkeypatch.delenv(key, raising=False)
    config, voice = local_storage.config_path(), local_storage.voice_path()
    if platform == "win32":
        assert config == tmp_path / "AppData/Roaming/Tacit/config.json"
        assert voice == tmp_path / "AppData/Local/Tacit/voice"
    elif platform == "darwin":
        assert config == tmp_path / "Library/Application Support/Tacit/config.json"
        assert voice == tmp_path / "Library/Application Support/Tacit/voice"
    else:
        assert config == tmp_path / ".config/tacit/config.json"
        assert voice == tmp_path / ".local/state/tacit/voice"
    assert not config.exists() and not voice.exists()
    legacy_config = tmp_path / ".config/tacit/config.json"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text("{}")
    legacy_voice = tmp_path / ".local/state/tacit/voice"
    legacy_voice.mkdir(parents=True)
    assert local_storage.config_path() == legacy_config
    assert local_storage.voice_path() == legacy_voice
    monkeypatch.setenv("TACIT_CONFIG", str(tmp_path / "custom.json"))
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "custom-voice"))
    assert local_storage.config_path() == tmp_path / "custom.json"
    assert local_storage.voice_path() == tmp_path / "custom-voice"


def test_private_export_refuses_existing_target_and_cleans_failed_permission(monkeypatch, tmp_path):
    target = tmp_path / "audio.wav"
    with local_storage.create_private_file(target) as file:
        file.write(b"original")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        local_storage.create_private_file(target)
    assert target.read_bytes() == b"original"
    failed = tmp_path / "failed.wav"

    def deny(*args, **kwargs):
        raise OSError("no ACL support")

    monkeypatch.setattr(local_storage, "make_private", deny)
    with pytest.raises(OSError, match="ACL"):
        local_storage.create_private_file(failed)
    assert not failed.exists()


def test_spool_tamper_symlink_and_quota_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "voice"))
    entries = [
        spool.Spool.create(repo="repo", source={}, server="https://fixture", author="author")
        for _ in range(3)
    ]
    with pytest.raises(audio.AudioError, match="Three"):
        spool.Spool.create(repo="repo", source={}, server="https://fixture", author="author")
    entries[0].append(b"a\0")
    path = entries[0].path / "0000.pcm"
    path.write_bytes(b"b\0")
    with pytest.raises(audio.AudioError, match="integrity"):
        entries[0].digest()
    path.unlink()
    outside = tmp_path / "outside.pcm"
    outside.write_bytes(b"a\0")
    path.symlink_to(outside)
    with pytest.raises(audio.AudioError, match="Invalid"):
        entries[0].chunk(0)
    assert outside.read_bytes() == b"a\0"


class NativeCall:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def test_windows_acl_is_protected_and_cleans_handles_on_failure(monkeypatch, tmp_path):
    import ctypes

    calls, freed = [], []

    def token(process, access, out):
        out._obj.value = 88
        return 1

    def token_info(handle, kind, buffer, size, needed):
        needed._obj.value = 32
        if buffer is None:
            return 0
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(7)
        return 1

    def sid(pointer, out):
        out._obj.value = "S-1-5-21-fixture"
        return 1

    def descriptor(sddl, revision, out, size):
        calls.append(sddl)
        out._obj.value = 9
        return 1

    def dacl(descriptor, present, pointer, defaulted):
        present._obj.value = True
        pointer._obj.value = 10
        return 1

    kernel = SimpleNamespace(
        GetCurrentProcess=NativeCall(lambda: 123),
        CloseHandle=NativeCall(lambda handle: freed.append(("handle", handle.value))),
        LocalFree=NativeCall(lambda pointer: freed.append(("memory", bool(pointer)))),
    )
    deny = [False]
    advapi = SimpleNamespace(
        OpenProcessToken=NativeCall(token),
        GetTokenInformation=NativeCall(token_info),
        ConvertSidToStringSidW=NativeCall(sid),
        ConvertStringSecurityDescriptorToSecurityDescriptorW=NativeCall(descriptor),
        GetSecurityDescriptorDacl=NativeCall(dacl),
        SetNamedSecurityInfoW=NativeCall(lambda *args: 5 if deny[0] else 0),
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kwargs: kernel if name == "kernel32" else advapi,
        raising=False,
    )
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "fixture ACL failure"), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    local_storage._windows_private(tmp_path, True)
    assert calls == ["D:P(A;OICI;FA;;;S-1-5-21-fixture)(A;OICI;FA;;;SY)"]
    assert freed.count(("handle", 88)) == 1 and freed.count(("memory", True)) == 2
    deny[0] = True
    with pytest.raises(OSError):
        local_storage._windows_private(tmp_path / "file.wav", False)
    assert calls[-1] == "D:P(A;;FA;;;S-1-5-21-fixture)(A;;FA;;;SY)"
    assert freed.count(("handle", 88)) == 2 and freed.count(("memory", True)) == 4


def windows_os():
    return SimpleNamespace(
        **{name: getattr(os, name) for name in dir(os) if name != "name"}, name="nt"
    )


@pytest.mark.parametrize("denied", [False, True])
def test_windows_capacity_lock_closes_descriptor_on_all_paths(monkeypatch, tmp_path, denied):
    monkeypatch.setenv("TACIT_VOICE_SPOOL", str(tmp_path / "voice"))
    monkeypatch.setattr(spool, "os", windows_os())
    seen = []

    def lock(fd, mode, size):
        seen.append(fd)
        assert mode == 9 and size == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
        if denied:
            raise OSError("busy")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_LOCK=9, locking=lock))
    if denied:
        with pytest.raises(audio.AudioError, match="Another Tacit"):
            with spool.capacity_lock():
                pytest.fail("Failed lock must not enter critical section")
    else:
        with spool.capacity_lock():
            assert os.fstat(seen[0])
    with pytest.raises(OSError):
        os.fstat(seen[0])


@pytest.mark.parametrize("errors", [[32, 33, 0], [5], [32] * 20])
def test_windows_atomic_replacement_retries_only_sharing_errors(monkeypatch, tmp_path, errors):
    import ctypes

    target = tmp_path / "manifest.json"
    target.write_bytes(b"original")
    monkeypatch.setattr(spool, "os", windows_os())
    errors = deque(errors)
    last, delays, attempts = [0], [], []

    def replace(source, destination, flags):
        attempts.append(flags)
        last[0] = errors.popleft()
        if last[0]:
            return False
        os.replace(source, destination)
        return True

    kernel = SimpleNamespace(MoveFileExW=NativeCall(replace))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last[0], raising=False)
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "fixture rename failure"), raising=False
    )
    monkeypatch.setattr(spool.time, "sleep", delays.append)
    succeeds = 0 in errors
    if succeeds:
        spool.atomic(target, b"replacement")
        assert target.read_bytes() == b"replacement"
        assert len(attempts) == 3 and delays == [0.025, 0.025]
    else:
        with pytest.raises(OSError):
            spool.atomic(target, b"replacement")
        assert target.read_bytes() == b"original"
        assert len(attempts) in {1, 20} and len(delays) == len(attempts) - 1
    assert all(flags == 0x9 for flags in attempts)
    assert list(tmp_path.glob("*.tmp")) == []
