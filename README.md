# Tacit

Voice-authored, source-bound, versioned project knowledge for humans and coding agents.

Maintainers explain intent through text or native audio, inspect transcripts and grounded proposals, resolve ambiguity, and explicitly confirm publication. Learners and delegated agents retrieve the same immutable snapshots read-only. Exact retrieval uses no speech or reasoning provider.

## Current status

First-line verification completed on 2026-10-31 using thirteen feature-specific adversarial agents. Python 3.12, 3.13 and 3.14 each passed **567 tests with 26 explained skips and zero failures**. Twenty skips are SQLite concurrency variants whose PostgreSQL counterparts ran; six require explicit live Evon configuration. Ruff lint, formatting and strict mypy pass. All seven workspace packages built successfully, producing fourteen wheel/sdist archives. Client-only wheel installation and real loopback CLI/MCP/WebSocket checks passed.

Tests reproduced and fixed buffered-audio loss, cancellation races, outside-collaborator invitation scoping, question reservation races, worker starvation, unsafe artifact failures, interrupted recovery, and Git-option injection in local source capture. Three nonfatal Starlette test-adapter deprecation warnings remain per runtime run.

Windows/macOS implementation and device-free fixtures exist; native platform/device acceptance is pending. Real providers, model quality and deployed operations have separate acceptance gates below. This is a development checkout, not a validated public release.

## Architecture and trust boundaries

- `packages/tacit_contracts`: shared Pydantic contracts and API/schema/evaluator versions.
- `packages/tacit_domain`: authorization and bounded deterministic knowledge/rule evaluation.
- `packages/tacit_client`: authenticated HTTP/WebSocket SDK and secure credential handling.
- `packages/tacit_cli`: native recording, durable recovery, teaching, review and retrieval.
- `packages/tacit_mcp`: six read-only local stdio tools.
- `services/api`: GitHub identity, publication, source grounding, provider adapters and encrypted storage.
- `services/worker`: transactional outbox projection, reconciliation and retention.
- `migrations`: PostgreSQL schema and immutability constraints.
- `deploy`, `scripts`, `site`: deployment/recovery/install tooling and static landing page.

PostgreSQL is the shared authority; SQLite is fixture-only. Prisma performs speech-to-text, Evon performs optional grounded reasoning, and Timbre synthesizes optional clarification speech. Provider keys remain on the backend. The product has no browser recorder or web dashboard.

Each personal GitHub account or organization installation maps to a workspace. GitHub eligibility and explicit scoped roles both constrain access. Workspace administration alone does not permit knowledge publication. Learners and all delegated agents remain read-only, including agents acting for maintainers. Outside collaborators require scoped invitations. Revocation is checked during long operations and before delivering results.

Drafts bind exact repository files and commits. Human confirmation uses the reviewed digest and expected parent snapshot. Publications atomically create immutable revisions, snapshot manifests, audit and outbox records. Concurrent stale publications fail rather than overwrite history. Reuse an operation ID only with its original unchanged input. Uncertain paid operations are never silently replayed.

## Backend setup

Use Python 3.12–3.14, uv, PostgreSQL and a configured GitHub App. From the checkout:

```sh
uv sync --frozen
# Create .env from .env.example only if it does not already exist.
# Configure PostgreSQL, GitHub and a stable credential encryption key privately.
uv run --env-file .env alembic upgrade head
uv run --env-file .env tacit-api
# In another terminal:
uv run --env-file .env tacit-worker
```

Preserve the existing encryption key: replacing it makes retained encrypted credentials/objects unreadable. Keep secrets out of Git. The API does not automatically load `.env`. `/health/live` checks process availability; `/health/ready` checks backend readiness, not model quality or availability. Never use the knowledge database as a disposable test database.

```sh
export TACIT_API_URL=http://127.0.0.1:8000
uv run --no-sync tacit login
uv run --no-sync tacit whoami
uv run --no-sync tacit workspaces list
uv run --no-sync tacit repos list --workspace WORKSPACE_ID
```

Complete GitHub's device flow. Human credentials use an approved OS keyring without plaintext fallback; delegated reads require `TACIT_AGENT_TOKEN`. HTTPS is required except on loopback. API/schema versions are `1`; evaluator version is `typed-expression-v2`. Explicit incompatible declarations receive `426 upgrade_required`; versionless legacy peers follow the implemented compatibility bridge.

## Teaching and consumption

```sh
uv run --no-sync tacit teach --repo REPO_ID --commit PUSHED_COMMIT_SHA \
  --file path/to/source.py --text-file /path/to/intent.txt
uv run --no-sync tacit review DRAFT_ID --repo REPO_ID
uv run --no-sync tacit confirm DRAFT_ID --repo REPO_ID
uv run --no-sync tacit repos head --repo REPO_ID
uv run --no-sync tacit context --repo REPO_ID --snapshot SNAPSHOT_ID \
  --commit PUSHED_COMMIT_SHA --files path/to/source.py
```

Use full pushed 40-character commit SHAs and canonical repository-relative paths with `/`. `teach` creates a private draft; only explicit `confirm` publishes. Transcript corrections preserve the original evidence. `teaching extract/list/review/answer/draft/cancel/speak` provides grounded candidates, clarification, Textual review and optional speech; use command `--help` for required arguments. Source selections include dirty hashes but never turn uncommitted bytes into verified commit evidence.

`ask --reason` explicitly requests cited reasoning. Incomplete, stale or conflicting context causes abstention. `evaluate` applies only to supported confirmed scheduler rules and returns permitted, forbidden, unknown or conflict. It is not a proof about arbitrary scheduler implementations. `history` follows immutable revision lineage.

```sh
uv run --no-sync tacit-mcp
# Or:
uv run --no-sync tacit mcp
```

Local MCP exposes `list_workspaces`, `list_repositories`, `get_context`, `ask_repository`, `evaluate_scenario` and `get_decision_history`. Configure a scoped agent credential privately in the host environment. MCP stdout contains protocol messages; diagnostics use stderr. Exact agent reads require zero STT/TTS calls. Remote MCP/OAuth is not implemented.

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

## Local MCP hosts

For a Windows MCP host, configure the executable as the absolute path to `powershell.exe` or `pwsh.exe`, with arguments `-NoProfile`, `-File`, and the absolute path to `tacit-mcp.ps1`. The host's PowerShell execution policy must permit that script. For macOS, use the absolute `tacit-mcp` launcher path. Supply `TACIT_API_URL` and a scoped `TACIT_AGENT_TOKEN` through the host's private environment; do not put secrets in repository examples. Launchers inherit stdin/stdout/stderr for stdio MCP. Codex CLI live acceptance is recorded below; Claude Code acceptance is waived at the user’s request; it has not been tested.

For direct checkout commands after client installation, use `uv run --no-sync --package tacit-cli tacit ...` or `uv run --no-sync --package tacit-mcp tacit-mcp`. A later full `uv sync` changes the shared checkout environment to include backend/dev dependencies; a later client-only install synchronizes it back to the selected client packages. Use a separate checkout for a backend environment you need to preserve.


## Incremental transcripts

## User flow

The default `teach-voice` workflow still captures and uploads recoverable audio first, then starts transcription. The optional `--live-transcript` flag starts a single Prisma realtime attempt while the microphone is active. It is available only for native microphone capture with the durable streaming workflow; file uploads and the legacy `--clip` path do not use it.

```sh
uv run --no-sync --package tacit-cli tacit teach-voice \
  --repo REPOSITORY_ID \
  --commit PUSHED_40_CHARACTER_COMMIT \
  --file path/to/source.py \
  --seconds 65 \
  --language en-IN \
  --live-transcript
```

The command asks for explicit consent before the provider stream and microphone start. Starting realtime can consume credits even when recording is cancelled or the final request fails. Provider credentials remain on the backend. A backend without the live capability is rejected before capture.

Completed provider segments are displayed incrementally on stderr as provisional text. They are not invented word-by-word partials. The live adapter requests a five-second maximum speech segment through Prisma's `x-max-speech-s` setting; this is a segmentation setting, not a measured latency guarantee. Speech activity/segment completion determines when a new segment is available; there may be a delay after speaking starts. Provider text is sanitized before terminal display. Final command JSON remains on stdout. Interim text is not a confirmed rule or a published snapshot.

Recording controls remain `p` + Enter to pause, `r` + Enter to resume, empty Enter to finish and Ctrl+C to cancel. A pause retains the same provider attempt. Tacit does not fill pauses with silence or reconnect the paid provider stream automatically; sufficiently long pauses or connection loss can end that attempt. Local transport heartbeat does not guarantee the provider will keep its session open.

## Transport and durable boundaries

`GET /v1/repos/{rid}/voice-capabilities` declares `live_transcript`. The additional WebSocket route is `/v1/repos/{rid}/voice-sessions/{sid}/live`. It requires a fresh empty session belonging to the authenticated human maintainer; agents and learners cannot start it. Existing version headers apply to its handshake.

| Direction | Frame | Meaning |
|---|---|---|
| Server → client | `{"type":"session","session":...}` | Reserved live session |
| Client → server | Binary: eight-byte big-endian sequence followed by PCM16 | One durable audio chunk, at most one second |
| Server → client | `{"type":"ack","chunks":N}` | Durable chunk count |
| Server → client | `{"type":"transcript","segment":...}` | Validated provider segment with index, ID and text |
| Client → server | `{"type":"ping"}` | Local transport heartbeat |
| Server → client | `{"type":"pong"}` | Heartbeat response |
| Client → server | `{"type":"finish","chunks":N,"audio_sha256":"...","capture_complete":true}` | End capture and bind the exact retained audio |
| Server → client | `{"type":"result","session":...}` | Final validated session result |
| Server → client | `{"type":"error","code":"..."}` | Safe failure code; no provider payload or credential |

Audio is retained locally and acknowledged only after encrypted backend storage. The backend reserves the operation before opening a provider stream and validates the final audio digest and source binding. Authorization is checked throughout the connection and before delivering transcript/final results. Resource queues, message sizes and deadlines are bounded. No remote MCP or new agent write capability is added.

The provider receives sequenced capture through one stream, with its required framing and final flush; successful live capture does not trigger a second post-recording transcription. Provider segments are validated for identity, size and ordering before display. Final transcript evidence is saved through the existing reviewable draft workflow. Explicit human `review`/`confirm` remains required to publish repository knowledge.

The shared provider parser also recognizes the documented one-based segment indexes as well as contiguous zero-based legacy indexes, without rewriting source evidence. Previously a valid one-based response could be marked incomplete. Settings and framing were checked against [Gnani's realtime documentation](https://docs.gnani.ai/api/STT/stt-websocket); that documentation does not establish live acceptance of this implementation.

## Failure and recovery

A lost live stream is not resumable as a paid stream. Preview text is not durable authoritative transcription. A failed, interrupted or ambiguous attempt retains local/server audio according to existing retention rules, but does not silently replay it. A saved final transcript can be recovered through the existing session recovery path without another provider call. Without a saved final transcript, deliberately starting a new operation is a separate potentially billable choice; export the retained WAV and inspect it first. Cancelling removes retained audio through the existing cancellation workflow; it cannot undo credits already used.


## Deployment and recovery

## Stable TLS endpoint

Use an operator-controlled Linux host with Docker Compose, persistent storage,
and a DNS hostname that resolves directly to that host. Permit inbound TCP 80 and
443 for ACME/HTTPS; UDP 443 is optional HTTP/3. If publishing an AAAA record, its
IPv6 route must also reach the host. A temporary tunnel URL is not stable hosting.

Create a private checkout `.env` from `.env.example`. Configure a strong URL-safe
`POSTGRES_PASSWORD`, stable `TACIT_CREDENTIAL_ENCRYPTION_KEY`, your GitHub App and
webhook secret, and provider settings as needed. Keep `TACIT_DEMO=false`. Do not
expose PostgreSQL or replace the API's loopback host-port binding. Configure:

```dotenv
TACIT_PUBLIC_HOST=tacit.example.org
TACIT_ACME_EMAIL=operator@example.org
```

The hostname must contain only the real DNS name, with no scheme, path or port.
The following are future operator commands, not a record of deployment:

```bash
docker compose --env-file .env -f deploy/compose.yaml \
  -f deploy/compose.tls.yaml up -d --build
```

The migration service precedes the API. The base API readiness healthcheck must
pass before Caddy starts. Caddy persists ACME state in `caddy-data`/`caddy-config`,
redirects HTTP to HTTPS, proxies `/v1/*` and health routes, and refuses other paths.
WebSocket upgrades for voice upload pass through the same proxy automatically;
the proxy does not rewrite the webhook payload or signature headers. Access
logging is not enabled, avoiding accidental credential/body logging.

The TLS overlay trusts forwarded headers on the API's private container listener.
Keep that listener behind Caddy and the host loopback binding; do not add a public
8000 binding with `FORWARDED_ALLOW_IPS=*`. The proxy does not replace Tacit's
credential, repository authorization, upload limits or GitHub HMAC checks.

Clients use `TACIT_API_URL=https://tacit.example.org`. In the GitHub App General
settings, set the webhook URL to
`https://tacit.example.org/v1/github/webhooks`, keep the same private secret and
SSL verification enabled, and subscribe to the needed Organization event. Actual
certificate issuance, HTTPS/WebSocket connectivity and genuine delivery remain
operator acceptance steps. No remote MCP/OAuth transport is added by this proxy.

## Database-only backups remain available

`scripts/backup.sh` and `scripts/restore.sh` remain the narrower database-only
tools. They do not include artifact objects or the encryption key. A database
dump alone is insufficient to recover encrypted voice/TTS artifacts or stored
provider credentials. They use `deploy/compose.yaml` and the checkout `.env`.

## Full local recovery bundle

**A bundle contains private database knowledge, encrypted artifact objects, and
a passphrase-wrapped copy of the matching stable encryption key. Protect the
whole bundle and keep its passphrase separately. Losing the passphrase/key makes
encrypted content unrecoverable.** Checksums alone are not authentication: the
manifest also has a passphrase-derived HMAC, and the key is wrapped with Fernet
using a separately salted Scrypt-derived key.

The bundle deliberately excludes the whole `.env`, GitHub/provider configuration,
provider API keys, Docker credentials, host TLS state, OS-keyring client tokens
and CLI recording spools. Preserve required backend configuration separately in
your secret manager. Caddy can issue new certificates for the same controlled
hostname; backing up its ACME state is a separate operational choice.

The tooling supports only the named local `artifacts` Compose volume. It fails
closed if `TACIT_ARTIFACT_BUCKET` is configured. The current recovery bounds are
100,000 archive entries, 100 GiB of object data, and 64 MiB per stored object.
An unfinished recovery stage prevents a new backup.

`scripts/recovery.sh` runs the standard-library/cryptography helper through this
checkout's installed environment. Invoke it explicitly with `bash`:

```bash
# Stop all writers yourself. The recovery tools refuse running api/worker/migrate
# services; they never stop/start them. Also stop any writers outside this stack.
docker compose --env-file .env -f deploy/compose.yaml stop api worker migrate

read -rsp 'Recovery passphrase (16+ characters): ' TACIT_RECOVERY_PASSPHRASE
echo
export TACIT_RECOVERY_PASSPHRASE
bash scripts/recovery.sh backup --directory /private/tacit-backups
unset TACIT_RECOVERY_PASSPHRASE
```

The private backup directory must be owned by the invoking user with mode 700.
The command refuses unsafe existing permissions instead of changing them. It
creates a unique hidden partial directory, captures a consistent PostgreSQL dump
while writers remain stopped, captures the matching encrypted volume, wraps the
key, authenticates/hashes the payloads, and publishes the completed directory.
It never prints the encryption key, recovery passphrase, provider settings, or
dump contents. The files are private (0600) and the bundle directory is 0700.
If using a nondefault Compose project, export the same `TACIT_COMPOSE_PROJECT`
used for deployment; otherwise recovery uses the normal `deploy` project.

Quiescence is an operational requirement, not a global database lock: operators
must prevent external database/object writers and avoid starting services while
a bundle is created/restored. Copy only completed bundle directories off-host.
Retain the original until a later separately authorized restore check succeeds.

## Restore to an explicitly empty target

Prepare the intended checkout/images and a private `.env` containing the matching
GitHub/provider configuration and stable key. Build images if necessary. Start
only PostgreSQL in the intended Compose project; do not start migrations, API,
worker or Caddy. The target database must have no non-system schema objects, and
the local artifact volume must be completely empty. Do not reuse production
volumes or run a volume-deletion command to manufacture an empty target.

If the stable key must be recovered first, write it to a new private file without
printing it:

```bash
read -rsp 'Recovery passphrase: ' TACIT_RECOVERY_PASSPHRASE
echo
export TACIT_RECOVERY_PASSPHRASE
bash scripts/recovery.sh recover-key --bundle /private/completed-bundle \
  --output /private/recovered-secrets/credential-key.env
```

The output directory must be owned by the current user with mode 700; an existing
output file is refused. Merge that one key setting privately into the target
`.env` using your secret-management process. Do not paste it into chat or commit
it. Recovery does not overwrite backend configuration automatically.

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d postgres
bash scripts/recovery.sh restore --bundle /private/completed-bundle \
  --target empty-development
unset TACIT_RECOVERY_PASSPHRASE
```

`empty-development` is an explicit safeguard acknowledgement, not permission to
overwrite an existing target. Restore authenticates the manifest, verifies every
payload checksum and archive path, requires the configured key to match exactly,
checks database emptiness, and stages objects into the empty volume before
restoring the database in a single transaction. It refuses archive symlinks,
hardlinks, devices, duplicate entries and traversal paths. The API/worker remain
stopped after success so the operator can review the result before startup.

PostgreSQL and the object filesystem are separate stores: this tool does not claim
an atomic commit across both. A failed or interrupted command leaves writers
stopped and may retain a hidden, operation-bound artifact stage. Never restart
writers or blindly rerun restore when the database outcome is uncertain.

If the database commit receipt was written but filesystem activation failed,
resume only that activation:

```bash
bash scripts/recovery.sh finish --bundle /private/completed-bundle \
  --target empty-development
```

`finish` requires the matching stage's confirmed database-commit receipt and will
not overwrite existing artifact destinations. The stage carries object size/hash
inventory. Completed activation leaves a private
`.tacit-recovery-completed-ID.json` receipt outside the stage; repeated completion
validates activated objects and can finish interrupted metadata cleanup. These
local receipts are excluded from artifact exports. Preserve them while recovering
that operation; they are not application audio objects. If PostgreSQL is still completely
empty, an uncommitted stage may instead be discarded explicitly:

```bash
bash scripts/recovery.sh discard --bundle /private/completed-bundle \
  --target empty-development
```

If the connection was lost after database commit but before the receipt was
written, neither automatic finish nor discard is safe. Independently inspect the
target database against the chosen backup (including schema/migration version,
known snapshot IDs and expected records) and the matching artifact stage while
all writers stay stopped. Only after confirming that exact database restore
committed, acknowledge its bundle `id` from `manifest.json` explicitly:

```bash
bash scripts/recovery.sh finish --bundle /private/completed-bundle \
  --target empty-development --acknowledge-database-committed EXACT_BUNDLE_ID
```

This option writes the missing commit receipt for that same operation before
activation. It is an operator attestation, not an automated database match check.
Never use it merely because a database is nonempty or to bypass a failed restore.
Without the acknowledgement, the tool fails closed rather than guessing whether
the transaction committed. First-line fixture tests and an isolated PostgreSQL,
encrypted-object and matching-key helper roundtrip now pass; see
the test record (see this README). Actual deployed Compose
permissions, full application recovery and power-loss behavior remain separate
acceptance gates.

## S3-compatible storage recovery

Do not use the local-volume recovery command for S3. Freeze all writers, take a
matching database dump, retain the bucket's versioned object inventory and the
exact object versions referenced by artifact rows, and retain the stable key in
your secret manager. Restore into a separate empty database/bucket, recover the
same object keys/versions and configure the matching key before starting writers.
Provider-specific snapshot/version-retention/export commands and IAM credentials
belong to the operator; no bucket operations or guessed credential acquisition
are performed by these scripts. A separately authorized end-to-end restore check
must precede any claim that this S3 procedure works for the selected provider.

## Verification and remaining acceptance

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest -ra
```

Set `TACIT_TEST_DATABASE_URL` only to a dedicated disposable PostgreSQL database to run concurrency/migration variants. Live Evon tests remain explicitly opt-in. The verified matrix used sanitized environments and temporary schemas; its disposable PostgreSQL container was removed afterward. Fixture providers do not establish live integration quality.

Remaining user/external gates:

- Native Windows/macOS installation, terminal behavior, Credential Manager/Keychain and microphone privacy prompts.
- Real microphones/speakers, device refusal/unplugging, sample-rate support and playback.
- Live Prisma short/long/incremental transcription, Timbre synthesis and measured credits; actual Evon quality and latency.
- Stable production DNS/TLS and the deferred four-user demonstration. Codex CLI and laptop/ngrok signed repository-addition/removal acceptance are recorded below; Claude Code testing is waived at the user’s request, without a compatibility claim.
- Human-authored 30-scenario evaluation with at least 10 held out, plus a bounded developer handoff study.
- Production-shaped application recovery, deployed storage permissions, power-loss behavior and actual S3 acceptance if selected.

Later implementation scope: remote MCP/OAuth, snapshot export/lockfiles, team mappings/custom roles, editor selection integrations, multi-maintainer approval quorum and offline teaching synchronization.

## Laptop HTTPS test endpoint

The laptop backend was reached through `https://zodiac-bubbling-stereo.ngrok-free.dev` on 2026-10-31 17:19 UTC. HTTPS liveness/readiness returned 200 with PostgreSQL ready; unauthenticated repository access and unsigned webhooks returned 401. Incompatible HTTP and WebSocket version declarations returned 426 through the tunnel; unauthenticated voice WebSocket handshakes were rejected. Protocol version headers survived proxying. Genuine GitHub-signed delivery and an authenticated WebSocket upgrade are verified below. Evon is unconfigured. This verifies ngrok-managed HTTPS, not direct Caddy certificate issuance. The laptop API, database and ngrok process must remain running for this endpoint to work.

GitHub delivery `a95843b0-d550-11f1-8da0-abc3bff532d0` (`installation_repositories.added`) reached the ngrok endpoint and returned HTTP 200 on 2026-10-31. Its verified receipt was persisted at `2026-10-31 17:29:45 UTC`; `tacit-voice-logging/webhook-probe` was recorded as enabled (`gh-1441094925`, workspace `gh-343680840`). This confirms a genuine GitHub-signed event passed through the HTTPS tunnel and was processed durably, without Evon.

Authenticated laptop endpoint checks also passed: GitHub device login identified the human owner and discovered both organization repositories. Exact retrieval of the existing scheduler-demo snapshot returned `answered`, confirmed applicability, one decision, no warnings and zero STT/TTS/reasoning/token usage. An authenticated voice-upload WebSocket returned HTTP 101 and the correct empty-session acknowledgment; that probe session was cancelled afterward. No microphone bytes, provider calls or knowledge publications were used.

Removal delivery `cb0b9d58-d551-11f1-923b-5b3869371059` was accepted with HTTP 200 and persisted at `2026-10-31 17:37:50 UTC`. The webhook disabled `webhook-probe` while `scheduler-demo` remained enabled. Subsequent authenticated scope checks require a fresh login because the previously saved Tacit credential expired; its 401 response is not evidence of repository-specific revocation. The existing API/database were restarted after a later shutdown; ngrok remained online.

After fresh public-endpoint login, repository-specific removal checks passed: the owner credential remained valid, repository discovery listed only `scheduler-demo`, and direct `webhook-probe` head access returned 404. The original `scheduler-demo` snapshot `1985d0af-121a-4a84-aff0-c8a6b20c6608` was unchanged and retrieved with confirmed applicability, no warnings and zero provider/token usage. This completes the laptop/ngrok HTTPS, authenticated read/WebSocket and signed repository-addition/removal checks. Direct Caddy certificate issuance, native devices, live providers and production/S3 acceptance remain separate.

On 2026-11-01, the second human account (`vanshjj`, `gh-139905374`) passed learner checks through the public HTTPS endpoint: discovery exposed only `scheduler-demo`; the shared published snapshot returned confirmed context with no warnings and zero provider/token usage. Teaching-draft creation, publication, voice-session creation and teaching-run creation all rejected empty-payload probes with 403. Reading the owner's private teaching draft also returned 403. The published snapshot remained unchanged, and no knowledge was published. This verifies the two-account owner/learner boundary; the four-user demonstration remains deferred.


On 2026-11-01, user-supplied output from a real Codex CLI session confirmed the local Tacit MCP server connected with all six read-only tools. A repository-scoped delegated agent credential discovered workspace `gh-343680840` and only `tacit-voice-logging/scheduler-demo`. `get_context` retrieved the exact published snapshot `1985d0af-121a-4a84-aff0-c8a6b20c6608` at commit `4f44dd5e2a7f37325be9f9ab7aa541140029d9c8`; Codex correctly reported the expired-worker publication prohibition and cited decision `a7f72fa6-ba60-4ff3-87a7-0463d56f6d17` and revision `70e3726c-8631-4e64-8880-bd6cb14885e0`. Applicability was confirmed, with no warnings or truncation and zero Tacit STT/TTS/reasoning calls or provider tokens. No teaching/publication tool was exposed. This proves Codex discovery and exact cited retrieval, not live Evon reasoning, execution of every MCP tool, or Claude Code compatibility; Claude Code testing is waived at the user's request; it is not recorded as a test pass.


## Human scenario and handoff evaluation

Status: preparation only; no quality or productivity result is established. Local CSV templates are in `output/human-evaluation/`: 20 teaching scenarios, 10 held-out scenarios, and a handoff results sheet. These local artifacts are not required for installation. Scenario statements and expected outcomes must be written and reviewed by a human module author before additional teaching; blank template rows are not authored scenarios. Existing published knowledge may already influence this evaluation, so identify that prior exposure rather than describing the dataset as a clean baseline.

The author records the scenario, facts, requested action, intended outcome, rationale and pinned source evidence. Include supported prohibitions, boundary cases, contradictory or missing facts, and cases where no definitive answer is justified. Keep the ten held-out scenarios and answer key out of teaching prompts and model-tuning/debugging; after using a case to guide fixes, retire it from the held-out set. Store answer keys privately and do not publish them as Tacit knowledge.

Before each run, pin the repository commit and published snapshot. Report exact context retrieval separately from deterministic typed-rule evaluation and optional Evon synthesis. The currently tested scheduler statement has no typed rule (`rule: null`); retrieving its text does not prove deterministic scenario evaluation. A text-only or Codex interpretation is also not evidence of Evon answer quality. Live Evon quality evaluation remains pending while its server is unavailable. Record output, expected outcome, citation validity, unsupported definitive answers, warnings, latency and provider usage for each evaluated case. Report the sample size and individual failures.

For handoff evaluation, define a bounded task and correctness rubric before timing. Prefer different participants with comparable experience or matched tasks with counterbalanced conditions; repeating the same task with the same person introduces learning bias. Compare source-only context with source plus Tacit context using the same code baseline, record time and correctness separately, and disclose participant count, task differences and prior exposure. No productivity gain is claimed before these measurements.


## Synthetic scheduler demonstration

`scheduler-demo` is an empty test repository, not an implemented scheduler. The operator agreed to use a proposed fictional publication policy to exercise Tacit. Local materials are in `output/scheduler-demo-evaluation/`: `proposed-specification.txt`, twenty development prompts, ten reserved prompts, separate answer keys, and review instructions. These materials are AI-authored. The human maintainer approved the specification on 2026-11-01; teaching, publication and evaluation remain pending. The ten reserved cases share an author with the specification and are not independently blind. The expired-lease rule has already been exposed in the existing snapshot. This demonstration does not fulfill the separate human-authored thirty-case benchmark or a real developer handoff study.

The proposed policy permits job-result publication only with current attempt ownership, completed computation, a currently valid lease and no cancellation. Known disqualifying facts forbid publication; missing or conflicting facts prevent permission, and unspecified actions remain unknown. The human maintainer must approve the text before using the normal teaching/review/confirmation workflow. Answer keys must remain out of teaching and model evaluation prompts. Pin the actual source commit and newly published snapshot before testing. Codex interpretation, exact retrieval, typed rules and live Evon quality are separate measurements.


On 2026-11-01, the human maintainer published the approved synthetic scheduler specification: decision `5ecd09f4-91cc-4840-bd48-bc5889b31888`, revision `23f0b137-c750-469d-9822-843eb1348edb`, snapshot `ff964f49-8174-4280-874a-4e27876a213b`, source commit `83c69e38ff7e26b5961a84d59ee26c43bd209de4`. Publication evidence was supplied by the operator. The specification is textual (`rule: null`). The local development evaluation prompt is pinned to this baseline; its twenty-case Codex interpretation run and ten reserved cases remain unexecuted. Earlier snapshot history is retained, so context warnings must be reported rather than silently discarded.


The user-supplied Codex development response matched all twenty saved synthetic expected outcomes (20/20). Its cited decision/revision IDs matched the published baseline. Codex reported confirmed, complete retrieval and zero Tacit provider calls/tokens, while preserving the warning `Retained knowledge from other commits needs applicability review`. The full raw MCP payload was not supplied, so this records the supplied client response rather than independent wire capture. These same-author development cases demonstrate text retrieval and Codex interpretation, not independent general accuracy, typed-rule execution, or Evon quality. Ten reserved synthetic cases remain pending. Local outcome comparison: `output/scheduler-demo-evaluation/development-results.json`.


The user-supplied Codex response for ten reserved synthetic cases matched all ten saved expected outcomes (10/10), citing the same published decision/revision. This run reused the earlier retrieved policy in the same conversation; it did not establish a fresh MCP retrieval or authorization check. Combined outcome agreement was 30/30 synthetic cases (20 development plus 10 reserved), not an independent blind benchmark. Some cases explicitly supplied derived facts such as lease validity, so they do not independently verify timestamp or ownership derivation. No Evon, typed-rule, actual scheduler, or real developer-handoff result is established. Local comparison: `output/scheduler-demo-evaluation/reserved-results.json`. Both synthetic case sets have now been exposed and must not be represented as unseen in later runs.


## Raahi real-code handoff preparation

The operator selected `nexxucc/raahi` as a simpler real-code exercise. Baseline: `2c11b8c25a7e3dc478e8f0187e5a65ba716b40bd`, repository ID `gh-1406716088`. A fresh isolated checkout passed all six tests in `tests/test_crowd.py` after installing its declared development dependencies; no live transit feed or paid provider was used. Local preparation is in `output/raahi-handoff/`: a source-derived intent statement awaiting maintainer review, and a proposed internal-matcher task to report sorted unique candidate route IDs for ambiguous matches without changing API schemas or matching semantics. No Raahi knowledge has been taught by this preparation, and no feature change or handoff evaluation has run. A Codex-only task does not establish human productivity gains.


The operator published Raahi matcher intent in snapshot `4eb0ab8f-669d-4a63-8af3-cadc11441b81`, decision `411b198f-f9c5-4fd0-870d-c91168ab9968`, revision `c54f153a-bfda-473c-bb54-347bf0bf8af8`, pinned to source `2c11b8c25a7e3dc478e8f0187e5a65ba716b40bd`. An isolated checkout and snapshot-pinned Codex task prompt are prepared under `output/raahi-handoff/`; execution and post-change review remain pending.


Raahi Codex-assisted handoff verification completed on 2026-11-01. Independent review of the local diff confirmed the only implementation change adds `candidate_route_ids: sorted(close_routes)` to ambiguous internal matcher results; the existing candidate selection, status logic, thresholds and other result fields are unchanged. Only `app/matcher.py` and new `tests/test_matcher.py` are changed in the isolated checkout. A fresh combined run passed 16 tests (six crowd plus ten matcher cases) in 0.41 seconds; focused Ruff and `git diff --check` passed. User-supplied Codex evidence reported confirmed snapshot retrieval, correct citations, no warnings, zero Tacit provider usage and 84 seconds elapsed; that timing and MCP trace were not independently captured. This demonstrates a bounded real-code task using Tacit context, not a controlled human handoff/productivity study, HTTP exposure of the new internal field, live feeds, or Evon synthesis. Changes remain uncommitted and unpushed.


## Live voice latency repair (2026-11-02)

Live operator tests failed: a retained 55.6-second recording yielded only delayed previews and no final draft; a 20-second attempt produced no final transcript. Backend storage showed audio delivery falling behind capture. Additional attempts exposed startup timeouts and an empty orphan session consuming quota; the operator cancelled that orphan while preserving captured recordings. These runs do not establish live voice acceptance.

Voice preflight now includes the authenticated user ID and current repository head so the CLI can reuse one response, with fallback for older servers. Duplicate initial capability/identity requests were removed, and progress messages distinguish access checks, session preparation and active capture. Initial source binding and fresh connection admission still happen before microphone capture; startup is not claimed to be instant. GitHub HTTP connections are pooled within each eligibility operation without caching access results or sharing cookies between operations.

Live audio/ACK/preview handling checks local credential expiry, membership, roles, repository enablement and session state on each event instead of making external GitHub requests for every chunk. A session-scoped background task revalidates GitHub eligibility every five seconds with a five-second deadline; failure stops the stream. GitHub-only revocation therefore has a bounded polling/detection window, distinct from per-event local checks. Final transcript persistence still requires fresh external authorization. Provider calls are never automatically replayed. Safe logs report handshake success, audio progress, slow authorization and failure classes without raw provider payloads, keys or transcript text.

Disposable PostgreSQL fixture verification of the live transcript and durable voice suites passed 64 tests, with 15 skips and one existing Starlette deprecation warning. Regression cases cover avoiding per-chunk external polling and stopping an idle stream after GitHub revocation, alongside existing logout/cancellation checks. Lint and targeted type checks passed. A new paid live attempt and measured time-to-capture/first-transcript/final-draft are still required; no live success is claimed.


After the latency repair, the operator supplied a successful 19.9-second live Prisma run on Raahi: recording `da7e33dc-207a-41e2-b00a-68b16bd2eac5`, operation `11c1e12b-0fb5-4a17-a243-e6c52b31c965`, session `ba60048d-b9b2-49da-b4fd-8e8edebfd42d`, private draft `e6174ed5-a8b5-4db6-bcc0-f6bffe2ce839`. Two provisional segments appeared before capture ended and a third afterward; the final draft reported `capture_complete: true`, `transcription_complete: true`, three segments and `ready_for_review`. No publication occurred. The original transcript misrecognized route/search terminology and needs human correction before publication. This establishes short live microphone-to-draft and incremental display acceptance from supplied client output; precise startup/finalization latency, credits for this attempt, provider-side completeness beyond its reported state, pause/resume, and recordings longer than thirty seconds remain unverified.


For the successful short live run, the operator reported noticeably improved responsiveness, approximately two seconds to the returned result, and a Prisma charge of 0.09 credits for 0.25 minutes. These are operator estimates/billing observations, not instrumented latency measurements; the CLI trace also contains a periodic waiting message. Provider-billed duration (15 seconds) differs from captured PCM duration (19.9 seconds) and is recorded separately without assuming a billing or audio-loss explanation.


A subsequent fifty-second live attempt delivered five provisional segments and thirty-two durable chunks before stopping on `Invalid transcript segment`; long live acceptance remains failed. Audio transport was progressing during capture. Gnani's current realtime documentation explicitly discusses empty transcript text, but the adapter rejected all blank strings. Valid empty segments are now retained with their original index/ID and omitted from previews and joined prose; all-empty sessions still fail and malformed/conflicting segments remain rejected. Safe schema flags now distinguish index/ID/text/finality failures without logging content. The supplied failure did not identify its rejected field, so empty text is a plausible cause rather than a confirmed explanation of that specific event. Fixture verification passed seven provider tests, with twenty-eight PostgreSQL-dependent live variants skipped in this run; lint and targeted mypy passed. A further live attempt is required. Source: https://docs.gnani.ai/api/STT/stt-websocket


The operator subsequently supplied matching client and API output for a successful 50.0-second live microphone run on 2026-11-02. Recording `3bdc7a43-a200-46b5-8bda-fc51a4ba6e88`, operation `c996c332-fa09-4b22-96c0-da91562fd4f2`, session `9e2033bb-67f5-4edf-8156-b3d4193b67ba`, private draft `540f9bf4-8715-4da6-9f9f-7a1b7bf6eea8`. Ten provisional segments appeared before capture ended and an eleventh afterward. API logs recorded all fifty durable chunks, fifty seconds forwarded to Prisma, capture finalization, and eleven complete segments; the draft reported both capture and transcription complete and remained unpublished. This establishes the tested long-live microphone/stream/preview/final-draft path, not arbitrary-duration reliability or word-perfect transcription. The transcript contains route/root spelling substitutions. The operator clarified that they actually spoke 'may remain ambiguous', so that phrase was transcribed faithfully and is not an ASR error. It differs from the scripted 'must remain ambiguous'; any change to the intended requirement needs human review before publication. No word-error rate was measured. This successful run contained no empty segments, so it does not independently prove empty-text validation caused the preceding failure. Exact latency and credits for this run, live pause/resume/cancellation/recovery, and broader provider quality remain separate checks.


For the successful fifty-second run, the operator reported 0.25 Gnani credits for 0.75 billed minutes (45 seconds), distinct from the captured PCM duration of 50.0 seconds. Together with the successful short run (0.09 credits), successful-run charges total 0.34 credits. This excludes prior failed attempts and does not establish a general tariff or explain billed-versus-captured duration differences. Exact finalization latency for the long run has not been supplied.


The operator authorized preparing a human correction of the successful long voice draft. Proposed text is local at `output/raahi-handoff/corrected-live-transcript.txt`; it restores route terminology and sentence boundaries, removes the standalone 'yes', and deliberately changes the spoken 'may remain ambiguous' to the intended 'must remain ambiguous'. The original 'may' was faithfully transcribed, so that edit is a human intent change, not an ASR error correction. The draft-reference correction path preserves the original transcript, audio hash, segments and source bindings without invoking a speech provider. Submission, review and publication acceptance of this correction remain pending.


The operator supplied successful correction-draft evidence: corrected draft `c6471034-93f2-4d53-bc7b-1b964aad15eb`, operation `3c42abcd-088e-4fc6-a034-933787b2c0b8`, references original draft `540f9bf4-8715-4da6-9f9f-7a1b7bf6eea8`. The reviewed statement differs while `voice.original_transcript` retains the spoken 'may', `corrected` is true, and the original audio SHA-256 `71bd655473b55bff949a12d3c53c033acd52a05f482f0f866b469be46c9fa661`, fifty-second duration, session/voice-operation IDs, eleven original segments and both source blob bindings match the original supplied draft. Both completion flags are true. Status is `ready_for_review` with no published snapshot. This verifies human correction and evidence preservation from operator output; no new recording or STT request was required by this correction path. Publishing test narration is unnecessary because approved Raahi matcher intent is already published.


The operator supplied a successful live pause/resume test: recording `2049f032-16c8-4234-93f8-ff95a18fc8d1`, operation `212372e2-2a0e-4e51-ae94-2a7cc980e477`, session `f1261714-550a-4245-8e31-e2e149026fd4`, private draft `198b2068-1d7f-48d7-a27b-289ffb1222ac`. The client acknowledged p/Enter pause and r/Enter resume; the final transcript contains the before/after phrases and excludes the specified paused sentinel phrase. It reports 16.8 captured seconds, three segments, both completion flags true and ready_for_review with no publication. This supports the tested short pause/resume flow from client output; raw paused audio exclusion, arbitrary pause length and provider reconnection behavior were not independently measured. Route/root homophone spelling remains a transcription-quality issue, not evidence of incorrect pronunciation. Credits for this attempt were not supplied.


Live Ctrl+C cancellation was verified for recording `524681b3-7a7c-4e75-b9db-e55842de5595`, operation `a25f0b0f-dd24-46bc-b4ad-fc832ff27183`, backend session `84c35c9d-ee0f-4133-a46f-fc408bf0f6f4`. The client received a preview before cancelling and the recording disappeared from its local list. Independent read-only database checks found status cancelled, zero retained VoiceChunk rows, zero teaching drafts and zero saved transcripts for that operation; the session retains its metadata counters for ten received chunks. The old cancellation message incorrectly claimed no transcription had happened despite the preview. It now reports cancellation and, on successful live cleanup, warns that Prisma may already have processed audio and consumed credits. Twenty platform-audio tests passed after this wording fix. Cancellation does not undo provider processing or establish zero charges.


Interrupted-upload recovery passed in an operator-driven test: stopping ngrok during non-live durable capture left recording `cbae5923-d182-4443-bef1-5b2a79fb59e6` recorded locally with thirty chunks/956800 PCM bytes (29.9 seconds), with the CLI reporting no transcription started. After restoring the tunnel, explicit `voice resume` returned private draft `8845152b-6b4c-46ab-96a0-72ab790151a9` without starting another capture. Voice operation `c75ad3ee-0216-479b-bd91-8277b0d287fc` and backend session `abb3930e-2699-42d5-ba6e-9018d0261303` matched the retained session; both completion flags were true and source bindings were preserved. This short recording used WAV transcription (`segments: []`), distinct from segmented live transcription. No publication occurred. This verifies the tested pre-transcription upload interruption/resume path from operator output, not recovery/rebilling behavior after an uncertain paid provider attempt. Credits and exact resume latency were not supplied.


Initial live Timbre smoke test failed with `synthesis_failed` (503), operation `8b23725c-70ea-45f2-88ff-82e540ba6ca9`. No successful playback is established. Current Gnani documentation confirms the configured binary REST endpoint, Timbre v2.5, English Kaveri voice and mono PCM16/48kHz WAV request settings. The original adapter hid failure details, so redacted stage, exception class, upstream HTTP status and audio byte count logging was added; keys, text and provider response bodies remain excluded. Artifact/Timbre fixture checks passed 15 tests with 14 database-dependent skips. No automatic retry or operation-reservation reset was performed. A diagnostic live retry, potentially billable, is required to distinguish provider rejection, connectivity and WAV-validation failures.


The diagnostic Timbre retry returned upstream HTTP 200 with 583724 audio bytes, then failed WAV validation; synthesis transport succeeded but playback remains unverified. The original response bytes/header were not retained, so neither header sizes nor output format can be established from that log. A fixture-reproduced compatibility change now finalizes canonical PCM WAV headers only when both RIFF and data lengths explicitly use the unknown-length 0xffffffff sentinel, preserving sample bytes; finite-length truncation and unsupported output formats remain rejected. Additional safe failure diagnostics include WAV channel count, width, rate, compression and declared frames. This is a tested compatibility improvement, not a confirmed explanation of that live failure. Combined voice-acceptance/artifact tests passed 29 with 20 database-dependent skips; targeted mypy and lint passed. A new provider attempt may incur another charge and must use a new explicitly chosen operation ID, retaining failed operation records.


Live Timbre synthesis and authenticated artifact download subsequently succeeded: operation `de177999-094c-4c95-a6f2-7d36ac9d619d`, artifact `d0580197-a038-4025-98d1-11b5708f4086`, 568364 bytes, SHA-256 `921e8f97590cabbf9ebbd27ddf88a02bf8e7ac2815b434f66f592095dc25b042`, reported TTS calls 1. The WAV was saved locally and the operator ran `tacit audio play` without a reported error. Audible speaker output, perceived quality, cost and latency await operator confirmation. This tests the backend synthesis/artifact/download/local playback path, not the Evon-dependent clarification-question workflow.


The operator confirmed the synthesized Timbre sentence was clearly audible through the speakers. Independent local inspection verified the downloaded artifact hash and a complete 5.92-second mono PCM16/48kHz WAV (284160 frames). The live synthesis/authenticated artifact download/local speaker playback smoke check therefore passed. This is a single short English/Kaveri example; synthesis credits and latency were not supplied, and full Evon-generated clarification-question playback remains unverified.


For the successful Timbre playback operation, the operator reported a charge of 0.2 credits for 110 billed characters. This records the observed successful request only; charges from preceding failed WAV-validation attempts were not supplied and are not included. It is not a general pricing guarantee.


## Post-voice full regression (2026-11-02)

The operator ran the full current suite on Python 3.14.8 with a disposable PostgreSQL 17 container and inherited Tacit/Gnani/Evon/AWS variables cleared. Result: **576 passed, 28 skipped, no failures/errors**, 604 collected, 411.85 seconds. Twenty-two skips are SQLite variants requiring PostgreSQL locking/concurrency; their PostgreSQL counterparts ran. Six skips require explicit live Evon configuration. Three warnings are existing Starlette/TestClient deprecations (one httpx backend warning and two timeout-argument warnings). Ruff lint passed, 84 files passed formatting, and strict mypy passed 49 source files. The operator's initial whitespace command was blocked by a missing less pager; the follow-up uses `git --no-pager diff --check`. Test evidence: `/tmp/tacit-regression.Ceqy8q/pytest.log` and `pytest.xml`. This latest full result is for Python 3.14 only; earlier Python 3.12/3.13 runs predate these voice fixes. Live-provider and device evidence is recorded separately above.
