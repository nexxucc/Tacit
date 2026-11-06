# Snapshots, exports and offline teaching

Pin and export published knowledge, verify offline copies, and synchronize queued text into private drafts.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Snapshot export and lockfiles

Use a lockfile to select a stable published snapshot without repeatedly pasting repository, commit and snapshot IDs. A lock pins one repository and API URL, its workspace, source commit, snapshot, manifest and complete export SHA-256, with explicit format/API/schema/evaluator versions. Creating a lock from the current head resolves that head once; later head changes do not move an existing lock. To update, deliberately create and review a new lockfile under a new output name. Existing files are never overwritten.

```bash
# Set this to your trusted Tacit API; the lock never selects a server automatically.
export TACIT_API_URL=https://zodiac-bubbling-stereo.ngrok-free.dev

# Optional --snapshot selects an older immutable snapshot instead of current head.
uv run --no-sync tacit snapshot lock --repo gh-1406716088 --output tacit.lock.json

# Online reads still need your human login, or --agent with TACIT_AGENT_TOKEN.
uv run --no-sync tacit context --lock tacit.lock.json --files app/matcher.py
uv run --no-sync tacit ask "What should happen for ambiguous routes?" --lock tacit.lock.json
uv run --no-sync tacit evaluate --scenario examples/scenario.json --lock tacit.lock.json

# Download complete published content and verify it against the pinned digest.
uv run --no-sync tacit snapshot export --lock tacit.lock.json --output snapshot.json
uv run --no-sync tacit snapshot verify --lock tacit.lock.json --export snapshot.json

# Explicit offline operations: no network, provider calls or credentials needed.
uv run --no-sync tacit snapshot verify --lock tacit.lock.json --export snapshot.json --offline
uv run --no-sync tacit context --lock tacit.lock.json --export snapshot.json --files app/matcher.py
```

The scenario file must describe a domain supported by the repository's published typed rules; using a lock does not create those rules or turn text knowledge into an executable policy. `ask` remains exact confirmed-statement retrieval unless `--reason` is explicitly requested, which may consume Evon resources. `context`, `ask` and `evaluate` preserve their existing explicit-ID forms; supplied IDs conflicting with a lock fail before credentials or reasoning requests are sent. Online commands require `TACIT_API_URL` to match the pinned API URL, and revalidate access and content instead of silently falling back to an export or a newer head. The lock does not automatically verify the local checkout's Git HEAD; its commit pins the retrieved knowledge, so check your checkout or supply an explicit `--commit` to detect a mismatch.

Exports use the authenticated read-only `GET /v1/repos/{rid}/snapshots/{sid}/export` endpoint. They contain published decisions, rules, source bindings and evidence references, including retained members from older commits; private drafts, access tokens and raw audio are excluded. A manifest must be consistent, revisions sorted/unique and the entire canonical payload at most 4 MB with at most 10,000 decisions. Oversized or inconsistent snapshots fail rather than producing a partial export. Agent reads remain read-only and scoped. Existing wire protocol versions are unchanged because this is an additive endpoint and CLI feature.

Treat exports as private repository data. Files are created exclusively with the platform's private storage permissions and symlink-safe writer; choose deliberately before sharing or committing any export. Lockfiles contain identifiers and a server URL, not credentials, but can still disclose private repository metadata. JSON duplicates, incompatible versions, scope/content changes and output collisions are rejected. Offline context preserves file filtering, pagination, evidence, zero provider usage and warnings for older-commit knowledge. Its applicability only compares the pinned source commit with exported decisions; it does not establish current authorization, revocation status, local code applicability or server authenticity. Content hashes detect changes relative to a trusted lock; they are not signatures and an attacker able to replace both files can forge a self-consistent pair. No snapshot import, offline teaching sync or remote MCP transport is added.


Snapshot/export verification: the full PostgreSQL-backed regression run passed **600 tests with 28 explained skips** on each of Python 3.12.15, 3.13.17 and 3.14.8. After final contract cleanup and two additional boundary cases, the focused snapshot/compatibility/CLI-MCP suite passed **102 tests with no skips** on each interpreter (26 snapshot feature cases per run). Ruff lint, formatting and strict mypy passed (51 source files). Contracts and CLI wheel/sdist builds succeeded and include the new modules. Tests cover online lock/export/context/question flows, unchanged explicit-ID commands through regression coverage, head movement, private-draft exclusion, scoped agent access/revocation, offline zero-network reads, retained-commit warnings, pagination, content/scope/version tampering, duplicate JSON keys, API-origin mismatch, size limits and safe output creation. All provider responses were fixtures; no Gnani/Evon requests, knowledge publications to real repositories or production database changes were made. The isolated PostgreSQL test container was removed. Logs/JUnit reports and source checksums are retained in `/tmp/tacit-snapshot-regression/`. A running backend must be restarted or redeployed to expose the new export endpoint; hosted/live user acceptance has not been performed.


Hosted snapshot/lock acceptance subsequently passed on the ngrok-backed deployment. The operator created `tacit.lock.json` for Raahi snapshot `4eb0ab8f-669d-4a63-8af3-cadc11441b81`, retrieved confirmed complete context with no warnings and zero provider usage, and downloaded `snapshot.json`. Independent CLI verification matched the downloaded content against the lock both offline and through fresh authenticated online access. An offline file-scoped read preserved revision `c54f153a-bfda-473c-bb54-347bf0bf8af8` and zero usage; both local files have mode 0600. This verifies the tested live pin/read/export/verification path without paid providers. The local knowledge export and lock are test artifacts and were not added to version control. Offline integrity remains distinct from current access or signed authenticity.

### Offline text teaching synchronization — 2026-11-03

`tacit offline` provides an opt-in private queue for text teaching intent. Preparation, listing, inspection, rebasing and forgetting work without API access or credentials. Preparation requires an explicit human author ID, a trusted configured API URL, a local Git commit and file scope, and a pinned base snapshot (via `--lock` or `--expected-snapshot`; use `none` only for an empty head). This claimed author is checked against the actual authenticated human at synchronization; it grants no permission by itself.

The queue stores reviewed text, optional typed rules/decision replacement, source hashes and local dirty-selection metadata. It does not store credentials or committed source bytes, perform background uploads, call a model/speech service, or publish knowledge. Dirty local source never replaces the source fetched from the pinned Git commit by the API. Saved-audio upload and transcription remain in the existing explicit voice workflow; this queue does not queue raw audio, preserve voice-correction lineage, perform offline Evon extraction or merge distributed edits.

Example using a previously generated lock for Raahi; replace paths and intent with what the human maintainer has reviewed:

```bash
cd /home/vansh/Tacit
export TACIT_API_URL=https://zodiac-bubbling-stereo.ngrok-free.dev

# Works while the API/network is unavailable; no login or keyring access is needed.
uv run --no-sync tacit offline prepare \
  --repo gh-1406716088 --author gh-149251890 \
  --commit 2c11b8c25a7e3dc478e8f0187e5a65ba716b40bd \
  --file app/matcher.py --source-root /path/to/raahi-checkout \
  --lock tacit.lock.json --text-file /private/path/reviewed-intent.txt

uv run --no-sync tacit offline list
uv run --no-sync tacit offline show ENTRY_ID

# Once online, sign in as that human, review the displayed queue entry, then consent.
uv run --no-sync tacit whoami
uv run --no-sync tacit offline sync ENTRY_ID --source-root /path/to/raahi-checkout

# Synchronization returns a private draft; publication is a separate human workflow.
uv run --no-sync tacit review DRAFT_ID --repo gh-1406716088
uv run --no-sync tacit confirm DRAFT_ID --repo gh-1406716088
```

The existing repository maintainer permissions and configured publication quorum still apply. Offline sync itself invokes neither `confirm` nor approval endpoints. All six MCP tools remain read-only. Preparation does not verify GitHub access or prove the commit is pushed; authenticated API draft creation does that online. A lock is a scope/base pin, not authorization or a signed source of truth.

Before the first upload, sync checks the selected server, current source hashes, actual human identity and current knowledge head. Changed source is refused; inspect intent and prepare a new entry if necessary. A changed head is refused without silently overwriting newer knowledge. After reviewing intent against the new snapshot, explicitly change an **unattempted** queue entry's base:

```bash
uv run --no-sync tacit offline rebase ENTRY_ID --expected-snapshot NEW_SNAPSHOT_ID
# Rebase asks for confirmation and assigns a fresh draft operation ID.
uv run --no-sync tacit offline sync ENTRY_ID --source-root /path/to/raahi-checkout
```

Sync durably records `attempted` before sending a draft request. If the connection or local acknowledgement fails, retry the same entry: the operation ID, request and base remain fixed, allowing server idempotency to recover a draft that may already exist. Recovery deliberately does not require unchanged local source or an unchanged current head; it replays the original pinned request, never current working bytes. Only a definitive `snapshot_conflict` HTTP 409 resets an attempted entry to unattempted, because that response means no draft was committed for the operation. Other uncertain/rejected attempts remain immutable and must be investigated/recovered before any new teaching attempt. Do not repeatedly create new entries to work around an uncertain result.

Successful sync saves the returned draft locally as `synced`; repeating it displays that saved result without contacting the API. This local copy does not prove current access, current publication status or later revocation. Use online review for current status. Never treat queue JSON as authenticated or portable authority.

The default queue is the `offline-teaching` sibling of the native voice spool directory. `TACIT_OFFLINE_QUEUE` selects an explicit private local directory. Files are private JSON with native ACL/permission handling; they are not encrypted by Tacit. Retention is explicit, with at most twenty entries and 128 KiB per entry; no age-based deletion runs. Intent is limited to 8,000 characters and local/committed source files to the existing 64 KiB capture bound. Exclusive OS locking serializes queue writers and sync/rebase/delete operations, so other queue operations wait while a sync or its consent prompt holds that lock. Atomic replace and durable acknowledgement reuse the audio spool's platform storage implementation. Never store this queue in version control.

```bash
uv run --no-sync tacit offline forget ENTRY_ID
```

Forgetting asks for confirmation and deletes only local data. It cannot retract a draft or immutable publication already created on the server. If deleting an uncertain attempt, preserve its operation ID privately before investigating the server outcome. No server migration is required for this CLI-only feature. Native Windows/macOS queue behavior and operator-run online/offline acceptance remain separate from fixtures.


Offline verification: full Python 3.14.8 regression with disposable PostgreSQL passed **830 tests, 33 skipped, zero failures/errors** (863 collected, 299.35 seconds), with three existing Starlette deprecation warnings. Twenty-seven skips were SQLite variants whose PostgreSQL counterparts ran; six require live Evon configuration. After adding the final disk-failure/capacity/corruption guards and tests, the focused offline/source/audio/snapshot regression passed **128 tests with one SQLite locking skip**. Final offline-specific tests passed **21 tests** on each supported Python version (3.12.15, 3.13.17, 3.14.8); these are repeated interpreter checks rather than independent scenarios.

Coverage includes offline preparation without an HTTP client, explicit consent, account/agent/server mismatch, changed source/head, explicit rebase, immutable attempted-request digests, lost API responses, failure to persist attempt/acknowledgement, mismatched API output, exclusive queue capacity under competing processes, UUID collisions, corrupted-state refusal and explicit local deletion, escaped paths/symlinks, lock scope, source bounds and recovery against the actual draft API with fixture GitHub responses. Database inspection in those API tests confirms exactly one draft and no snapshot/publication receipt after retries.

Ruff, formatting, strict mypy (59 source modules), lockfile consistency, diff checks, CLI wheel/source build and AST graph refresh passed. Detailed logs/JUnit remain at `/tmp/tacit-offline-regression.VZsXOS/`; safe local receipt is `output/offline-check/results.json`. The disposable test database was removed. No working-database migration, real knowledge upload/publication, live provider call, commit or push occurred. The remaining acceptance is a human-operated offline-to-online text queue run and native Windows/macOS storage/terminal checks. Existing saved-audio recovery remains a separate workflow.


