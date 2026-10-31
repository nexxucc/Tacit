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

For a Windows MCP host, configure the executable as the absolute path to `powershell.exe` or `pwsh.exe`, with arguments `-NoProfile`, `-File`, and the absolute path to `tacit-mcp.ps1`. The host's PowerShell execution policy must permit that script. For macOS, use the absolute `tacit-mcp` launcher path. Supply `TACIT_API_URL` and a scoped `TACIT_AGENT_TOKEN` through the host's private environment; do not put secrets in repository examples. Launchers inherit stdin/stdout/stderr for stdio MCP. Real client acceptance is deferred.

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
- Real Codex/Claude sessions, stable DNS/TLS, genuine deployed GitHub webhooks/revocation and the deferred four-user demonstration.
- Human-authored 30-scenario evaluation with at least 10 held out, plus a bounded developer handoff study.
- Production-shaped application recovery, deployed storage permissions, power-loss behavior and actual S3 acceptance if selected.

Later implementation scope: remote MCP/OAuth, snapshot export/lockfiles, team mappings/custom roles, editor selection integrations, multi-maintainer approval quorum and offline teaching synchronization.
