# Tacit

Voice-authored, source-bound, versioned project knowledge for humans and coding agents.

## The problem

Code shows what a system does, but not why. The reasoning behind a rule, like "a job result can only be published while the worker still holds a valid lease", usually lives in someone's head, an old chat thread, or a PR comment nobody can find. New teammates have to ask around, and coding agents just guess from the code, which is how they confidently break rules nobody wrote down.

Tacit gives that reasoning a proper home. A maintainer explains intent out loud (or in text), Tacit ties it to the exact files and commit it's about, and after a human reviews and confirms it, it becomes versioned knowledge that learners and coding agents can query later. Agents can read it but never write it.

## The main ideas

- **Intent comes from a human.** A maintainer speaks or types an explanation. Nothing is published unless a person explicitly confirms it.
- **Knowledge is bound to source.** Every draft points at specific files in a specific pushed 40-character commit SHA, so it's always clear what code the knowledge was written about. Uncommitted local edits are recorded as hashes but never count as verified evidence.
- **Knowledge is immutable and versioned.** Publishing creates a new snapshot of the repository's knowledge. Old snapshots never change, history can be followed revision by revision, and a stale publication fails instead of overwriting newer work.
- **Reading is cheap and safe.** Learners and agents read a pinned snapshot read-only. Exact retrieval uses no speech or AI provider at all, so it costs nothing and gives the same answer every time.
- **When unsure, it abstains.** If the retrieved context is incomplete, stale or conflicting, Tacit says so instead of making something up.

## How it works end to end

1. **Teach.** A maintainer runs `teach` (text) or `teach-voice` (microphone) against a repo, a pushed commit and one or more files. Audio is captured locally, uploaded in durable chunks, and transcribed by Prisma. With `--live-transcript` you see provisional text while you speak. If an upload gets interrupted, the recording stays on disk and can be resumed.
2. **Draft.** Tacit creates a private draft containing the transcript (original evidence is always kept, even if you correct it), the source bindings, and a proposed statement of the rule. Optionally Evon can extract grounded candidate decisions and Timbre can read clarification questions aloud, but both are optional and the keys stay on the backend.
3. **Review.** The maintainer reads the draft, fixes the wording, answers any clarifying questions, and resolves ambiguity. A correction is a human edit layered on top of the preserved original, not a replacement for it.
4. **Confirm.** Only an explicit `confirm` publishes. It's tied to the reviewed digest and the expected parent snapshot. If the repository requires several maintainers, other distinct people must have approved that exact draft first (the quorum can be 1 to 5 people).
5. **Publish.** In one atomic step Tacit writes an immutable revision, a new snapshot manifest, an audit record and an outbox record. The worker then projects it for retrieval.
6. **Consume.** Anyone with read access (or a scoped agent) pulls it back out:
   - `context` returns the confirmed decisions that apply to given files at a given commit, with citations. It warns when some retained knowledge came from other commits.
   - `ask` does exact retrieval of confirmed statements. Adding `--reason` explicitly asks for cited reasoning from Evon, which may cost credits.
   - `evaluate` runs a scenario against confirmed typed rules and answers permitted, forbidden, unknown or conflict. It only works for supported rule domains (currently scheduler rules), and it's not a proof about arbitrary code.
   - `history` follows the revision lineage of a decision.
   - Coding agents get the same reads through six read-only MCP tools, either as a local stdio server or as an optional remote server with repository-scoped OAuth.

## Vocabulary

| Term | Meaning |
|---|---|
| Workspace | A GitHub personal account or organization installation. |
| Repository | A GitHub repo enabled inside a workspace. Knowledge is stored per repository. |
| Draft | A private, unpublished proposal built from a teaching session. Only its author and permitted maintainers can see it. |
| Decision | One confirmed statement of intent, optionally with a typed rule that can be evaluated. |
| Revision | One immutable version of a decision. Later edits add revisions, they never rewrite old ones. |
| Snapshot | The full set of published knowledge for a repository at one point in time. The latest one is the head. |
| Lockfile | A small file that pins a repository to one exact snapshot, so tools keep using it even after the head moves. |

## Who can do what

Access comes from GitHub identity plus explicit Tacit roles. Both have to allow it.

- **Learners** read confirmed knowledge, ask questions and evaluate scenarios.
- **Maintainers** are humans who can teach, review, correct and publish. Custom roles and GitHub team mappings can grant this too.
- **Administrators** manage roles, mappings and publication policy, but being an administrator alone does not allow publishing knowledge.
- **Agents** are always read-only, even when they act on behalf of a maintainer. They can't teach, approve or publish.

Revocation is checked during long operations and again before results are delivered.

## What's in the box

- `tacit` / `tacit-intent` CLI for recording, teaching, review, confirmation, retrieval, snapshot export/lockfiles, role and quorum administration, and an offline queue for preparing text teaching without a connection and syncing it later as a private draft.
- A VS Code extension (local, not on the Marketplace) that exports a file, a selection or a symbol as source context for teaching.
- `tacit-mcp` for agents, local stdio or remote Streamable HTTP with OAuth.
- An API service and a worker, with PostgreSQL as the single source of truth (SQLite is only used in test fixtures). The worker handles the outbox, reconciliation and retention.
- Deployment tooling: a Docker Compose stack with a Caddy TLS proxy, plus backup and restore scripts for the database, encrypted artifacts and the encryption key.
- There is deliberately no browser recorder and no web dashboard. Everything goes through the CLI, the editor adapter and MCP.

## Trust boundaries in one paragraph

Provider keys (Prisma for speech to text, Evon for optional reasoning, Timbre for optional speech) never leave the backend. Audio and credentials are stored encrypted with one stable encryption key, so losing that key makes retained encrypted data unreadable. Published history is immutable, uncertain paid operations are never silently replayed, and the same operation ID can only be reused with its original unchanged input.

## Where this stands

This is a development checkout, not a validated public release. The core flows (teaching, publication, retrieval, quorum, team roles, remote MCP, recovery) have been tested, some of them live. Native Windows/macOS device checks, live Evon quality, production DNS/TLS and a controlled human handoff study are still open. The sections below list exactly what was tested, what wasn't, and how to run everything.

## What you can do

- **Teach in text or voice:** record locally, inspect live transcript previews, pause/resume capture and recover retained audio after interruptions.
- **Review before publishing:** correct transcripts, clarify grounded proposals and confirm an exact draft against its expected parent snapshot.
- **Share controlled knowledge:** use GitHub workspaces, scoped roles, team mappings and human approval quorums.
- **Connect coding agents:** retrieve source-bound context through local stdio MCP or optional remote MCP with repository-scoped OAuth.
- **Pin and work offline:** export immutable snapshots for offline reads, or queue text intent and explicitly sync it into private drafts later.
- **Select source from your editor:** export files, ranges or symbols through the VS Code adapter, including local dirty-file metadata.

## Tech stack

| Area | Technologies and role |
|---|---|
| Runtime and packaging | Python 3.12–3.14, uv workspaces and a frozen dependency lockfile |
| Backend | FastAPI and Uvicorn for HTTP/WebSockets; Pydantic contracts and settings |
| Persistence | PostgreSQL, SQLAlchemy, psycopg and Alembic; SQLite is used only for fixtures |
| Native client | Typer and Rich CLI, Textual review UI, sounddevice/PortAudio for recording and playback |
| Identity and credentials | GitHub App/device flow, OS keyring, scoped delegation and Fernet-encrypted backend credentials/artifacts |
| Agent integration | MCP SDK; stdio and optional Streamable HTTP with OAuth and S256 PKCE |
| Optional speech and reasoning | Gnani Prisma for speech-to-text, Evon for grounded reasoning, Gnani Timbre for clarification speech |
| Storage and operations | Local encrypted artifacts or S3-compatible storage via boto3; Docker Compose and optional Caddy TLS |
| Editor and quality tooling | JavaScript VS Code extension; pytest, Ruff and strict mypy |

See [system design](SYSTEM_DESIGN.md) for package responsibilities and trust boundaries, and [deployment](DEPLOYMENT.md) for storage and recovery details.

## Quickstart

You need Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and a compatible Python interpreter. A backend also needs PostgreSQL and a configured GitHub App. These shell examples target a POSIX terminal; [platform setup](PLATFORM_SETUP.md) includes native Windows PowerShell and macOS instructions.

### Connect a client to an existing backend

```sh
git clone https://github.com/nexxucc/Tacit.git
cd Tacit

uv sync --frozen --package tacit-cli --package tacit-mcp --no-default-groups

# Replace this with your trusted backend URL.
export TACIT_API_URL=https://YOUR-TACIT-HOST
uv run --no-sync tacit login
uv run --no-sync tacit whoami
uv run --no-sync tacit workspaces list
uv run --no-sync tacit repos list --workspace WORKSPACE_ID
```

Complete GitHub's device flow in your browser. Human credentials are stored in an approved OS keyring; agent reads use a scoped `TACIT_AGENT_TOKEN`. HTTPS is required except on loopback. Speech and model API keys are configured on the backend, not in the client.

For microphone support, use the platform installer with its audio option. The installer creates the `tacit-intent` launcher; inside a development checkout, `uv run --no-sync tacit` invokes the same CLI. See [client installation and audio setup](PLATFORM_SETUP.md).

### Run your own development backend

From the checkout, with PostgreSQL available:

```sh
uv sync --frozen

# Preserve an existing .env.
if [ ! -e .env ]; then cp .env.example .env; fi
```

Edit `.env` before continuing. Set `TACIT_DATABASE_URL`, `TACIT_GITHUB_APP_ID`, `TACIT_GITHUB_CLIENT_ID` and a stable `TACIT_CREDENTIAL_ENCRYPTION_KEY`; configure the webhook secret when accepting GitHub webhooks. The GitHub App needs device flow and the permissions described in `.env.example`. Speech/reasoning settings are optional for text teaching and exact retrieval. Keep the encryption key stable so retained encrypted credentials and objects remain readable.

```sh
# Migrate the selected database, then start the API.
uv run --env-file .env --no-sync alembic upgrade head
uv run --env-file .env --no-sync tacit-api
```

In another terminal, from the same checkout:

```sh
uv run --env-file .env --no-sync tacit-worker
```

Connect a local client with `export TACIT_API_URL=http://127.0.0.1:8000`. Check readiness with `curl --fail-with-body http://127.0.0.1:8000/health/ready`; this checks backend readiness, not provider quality. The API does not automatically load `.env`, which is why the server commands pass `--env-file` explicitly. See [backend setup](GETTING_STARTED.md) for authentication details and [deployment](DEPLOYMENT.md) for Compose, TLS and backup procedures.

### Teach, review and retrieve

```sh
uv run --no-sync tacit teach \
  --repo REPO_ID --commit PUSHED_40_CHARACTER_SHA \
  --file path/to/source.py --text-file /path/to/intent.txt

uv run --no-sync tacit review DRAFT_ID --repo REPO_ID
uv run --no-sync tacit confirm DRAFT_ID --repo REPO_ID

uv run --no-sync tacit context \
  --repo REPO_ID --snapshot SNAPSHOT_ID \
  --commit PUSHED_40_CHARACTER_SHA --files path/to/source.py
```

Use repository IDs returned by discovery, full pushed commit SHAs and canonical repository-relative paths. `teach` creates a private draft; `confirm` publishes only after the permission, digest, snapshot and configured quorum checks pass. The [teaching guide](TEACHING.md) explains proposals, typed-rule evaluation and history; the [voice guide](VOICE.md) covers microphone capture and transcription.

## Documentation

All detailed guides live in the repository root. They preserve the material previously collected in this README, with dated demonstrations collected separately so setup instructions stay easy to find.

### Setup and system design

- **[Backend setup and authentication](GETTING_STARTED.md)** — Configure the backend, run migrations and services, complete GitHub device login, and understand credential and version requirements.
- **[Client installation and native platforms](PLATFORM_SETUP.md)** — Install Arch, Windows and macOS launchers; enable audio; locate private client data; and handle existing recordings and native permissions.
- **[System design and trust boundaries](SYSTEM_DESIGN.md)** — Explore the seven workspace packages, PostgreSQL authority, source bindings, provider separation and human-only publication rules.

### Teaching and collaboration

- **[Teaching and retrieving knowledge](TEACHING.md)** — Follow text teaching, draft review, explicit publication, pinned context retrieval, optional reasoning, supported typed-rule evaluation and immutable history.
- **[Voice capture and live transcription](VOICE.md)** — Use provisional transcripts, sequenced capture, pause/resume and retained-audio recovery, including the transport and finalization boundaries.
- **[Access control and publication approvals](ACCESS_AND_APPROVALS.md)** — Configure team-to-role mappings, custom roles and repository approval policies; understand distinct-human counting, withdrawal, revocation and policy versions.
- **[Editor source selections](EDITOR_INTEGRATION.md)** — Build and install the VS Code adapter, export files/ranges/symbols, and validate commit bindings and changed-source hashes before teaching.

### Agents, offline work and operations

- **[Coding-agent integration and remote OAuth](MCP.md)** — Configure the six read-only tools through stdio or remote HTTP, including registered clients, consent, scoped tokens, expiry and revocation.
- **[Snapshots, exports and offline teaching](SNAPSHOTS_AND_OFFLINE.md)** — Pin snapshots, verify offline exports, queue text intent, recover interrupted synchronization and explicitly rebase unattempted entries.
- **[Deployment, backups and recovery](DEPLOYMENT.md)** — Set up Caddy TLS, create database or full local recovery bundles, restore to empty targets, and review the separate S3 recovery procedure.

### Verification and evidence

- **[Testing and evaluation](VERIFICATION.md)** — Run lint, formatting, typing and test checks; configure disposable PostgreSQL coverage; and understand scenario, handoff and external acceptance gates.
- **[Verification and acceptance history](ACCEPTANCE_HISTORY.md)** — Read the original dated regression results, operator voice/MCP/team/quorum/offline demonstrations, incident fixes and evidence limits.

## Project status

Tacit is a development checkout rather than a validated public release. Text/voice teaching, pinned reads, team/custom roles, remote MCP/OAuth, editor selections, publication quorum and offline text synchronization are implemented. Recorded operator checks include Arch voice/editor workflows, scoped MCP reads and revocation, two-human quorum publication and basic offline-to-online text synchronization.

Native Windows/macOS device acceptance, independent quality/handoff evaluation and production deployment/storage gates remain separate. Offline synchronization currently covers text intent; saved audio uses the existing voice recovery workflow. Consult [verification](VERIFICATION.md) and the dated [acceptance history](ACCEPTANCE_HISTORY.md) before treating a feature as validated for a particular environment.
