# Client installation and native platforms

Install the Arch, Windows and macOS clients, configure audio permissions, and locate private client data.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Arch client and recording

```sh
bash scripts/install-arch.sh --audio
# Arch audio additionally needs the system PortAudio package.
tacit-intent doctor
tacit-intent audio devices
tacit-intent audio devices --output
```

The checkout launcher is `tacit-intent`; installers refuse existing executables. Other tools already use the name `tacit`, so public distribution naming remains unresolved. No market-first claim is established.

`teach-voice` defaults to recoverable sequenced capture/upload. `voice list/resume/cancel/export/forget` manages retained recordings. Capture uses mono PCM16 at 16 kHz with bounded queues and duration. Press `p` + Enter to pause, `r` + Enter to resume, empty Enter to finish, or Ctrl+C to cancel. Paused microphone input is discarded; retained sessions still expire by wall clock. `audio play FILE --device ID` selects an output; `teaching speak --output-device ID` selects synthesized playback output.


## Native Windows and macOS clients

## Windows checkout install

Use native Windows PowerShell 5.1 or PowerShell 7, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/). The project requires Python 3.12–3.14; uv may provision a compatible interpreter according to its configuration. Keep this checkout and uv at their installation paths. Review scripts before executing and follow the machine's execution policy; the installer does not alter that policy, PATH or system settings.

From the checkout:

```powershell
.\scripts\install-windows.ps1 -Audio
```

Omit `-Audio` for text-only use. The script synchronizes the locked CLI/MCP workspace packages without backend/dev dependencies and creates `tacit-intent.ps1` and `tacit-mcp.ps1` in `$env:LOCALAPPDATA\Tacit\bin`. Existing commands/files are refused before synchronization. It creates no `tacit` alias that might replace an unrelated command. The launchers preserve the child exit code and inherited console streams. Use `-BinDirectory 'C:\your\directory'` for an explicit launcher directory.

```powershell
$env:TACIT_API_URL = 'https://YOUR-TACIT-HOST'
& "$env:LOCALAPPDATA\Tacit\bin\tacit-intent.ps1" doctor
& "$env:LOCALAPPDATA\Tacit\bin\tacit-intent.ps1" login
& "$env:LOCALAPPDATA\Tacit\bin\tacit-intent.ps1" audio devices
& "$env:LOCALAPPDATA\Tacit\bin\tacit-intent.ps1" audio devices --output
```

Use loopback HTTP only when the backend or its local SSH tunnel is on this machine. `https://YOUR-TACIT-HOST` is a placeholder, not an existing deployment. Windows microphone access and desktop-app microphone access must be enabled in Settings → Privacy & security → Microphone. A machine policy may require its administrator to enable access; Tacit does not change the policy. See [Microsoft's microphone permissions guidance](https://support.microsoft.com/en-us/windows/privacy/turn-on-app-permissions-for-your-microphone-in-windows).

## macOS checkout install

Use a native terminal with Git and uv. Apple Silicon and Intel are implementation targets where compatible Python and dependency wheels are available; neither architecture has been verified in this pass.

```sh
bash scripts/install-macos.sh --audio
export PATH="$HOME/.local/bin:$PATH"
export TACIT_API_URL='https://YOUR-TACIT-HOST'
tacit-intent doctor
tacit-intent login
tacit-intent audio devices
tacit-intent audio devices --output
```

Omit `--audio` for text-only use. The installer creates `tacit-intent` and `tacit-mcp` under `${XDG_BIN_HOME:-$HOME/.local/bin}`, refuses existing commands, and pins launchers to the checkout and uv executable. It does not use sudo, install Homebrew or modify shell startup files. Allow the terminal/IDE that launches Tacit under System Settings → Privacy & Security → Microphone. See [Apple's microphone settings guidance](https://support.apple.com/en-au/guide/mac-help/mchl211c911f/mac).

The upstream [sounddevice installation instructions](https://github.com/spatialaudio/python-sounddevice/blob/master/doc/installation.rst) describe bundled PortAudio on Windows/macOS. The audio extra selects the locked dependency; missing native libraries or unsupported architectures produce setup guidance. Linux still requires the platform's PortAudio package. No new dependency or lockfile version is required for these changes.

## Native data paths and existing recordings

| System | Configuration | Retained voice |
|---|---|---|
| Windows | `%APPDATA%\Tacit\config.json` | `%LOCALAPPDATA%\Tacit\voice` |
| macOS | `~/Library/Application Support/Tacit/config.json` | `~/Library/Application Support/Tacit/voice` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/tacit/config.json` | `${XDG_STATE_HOME:-~/.local/state}/tacit/voice` |

Existing legacy `~/.config/tacit/config.json` and `~/.local/state/tacit/voice` paths remain selected when present. `TACIT_CONFIG` and `TACIT_VOICE_SPOOL` overrides remain supported. Data is not moved or deleted automatically. `doctor` reports selected paths without creating them or opening an audio device. Windows storage requires filesystem support for private ACLs; an unsupported location fails rather than claiming privacy from `chmod` alone.

Input text/scenario/clarification files are read as UTF-8 across platforms. Audio exports use private exclusive creation and never overwrite an existing target. Snapshot and source paths remain canonical repository-relative paths using `/`, even when the checkout is on Windows.

