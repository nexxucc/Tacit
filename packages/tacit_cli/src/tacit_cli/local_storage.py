"""Native user paths and private files shared by CLI configuration and audio."""

import os
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO


def config_path() -> Path:
    if value := os.environ.get("TACIT_CONFIG"):
        return Path(value).expanduser()
    legacy = Path.home() / ".config/tacit/config.json"
    if os.path.lexists(legacy):
        return legacy
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Tacit/config.json"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Tacit/config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tacit/config.json"


def voice_path() -> Path:
    if value := os.environ.get("TACIT_VOICE_SPOOL"):
        return Path(value).expanduser()
    legacy = Path.home() / ".local/state/tacit/voice"
    if os.path.lexists(legacy):
        return legacy
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Tacit/voice"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Tacit/voice"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tacit/voice"


def reject_link(path: Path) -> None:
    """Reject links and Windows junction/reparse targets before accessing local data."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("Private local storage must not use a symlink or reparse point")


def reject_redirected_path(path: Path) -> None:
    reject_link(path)
    if os.name == "nt":
        for parent in path.absolute().parents:
            reject_link(parent)


def _windows_private(path: Path, directory: bool) -> None:
    """Install a protected DACL granting only the process user and SYSTEM access."""
    import ctypes
    from ctypes import wintypes

    # Windows exports are absent from Linux ctypes type stubs.
    windows: Any = ctypes
    advapi = windows.WinDLL("advapi32", use_last_error=True)
    kernel = windows.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        pointer,
    ]
    advapi.GetSecurityDescriptorDacl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    descriptor = pointer()
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise windows.WinError(windows.get_last_error())
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise windows.WinError(windows.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(pointer))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise windows.WinError(windows.get_last_error())
        inherit = "OICI" if directory else ""
        sddl = f"D:P(A;{inherit};FA;;;{sid_text.value})(A;{inherit};FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise windows.WinError(windows.get_last_error())
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = pointer()
        if (
            not advapi.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            )
            or not present.value
            or not dacl.value
        ):
            raise OSError("Cannot construct private Windows storage permissions")
        result = advapi.SetNamedSecurityInfoW(
            str(path.absolute()), 1, 0x80000004, None, None, dacl, None
        )
        if result:
            raise windows.WinError(result)
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, pointer))
        if descriptor:
            kernel.LocalFree(descriptor)


def make_private(path: Path, *, directory: bool = False) -> None:
    reject_link(path)
    if os.name == "nt":
        _windows_private(path, directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def private_directory(path: Path) -> None:
    reject_redirected_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private(path, directory=True)


def create_private_file(path: Path) -> BinaryIO:
    """Create exclusively and secure before writing; never replace a caller's file."""
    reject_redirected_path(path.parent)
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags, 0o600)
    try:
        make_private(path)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
