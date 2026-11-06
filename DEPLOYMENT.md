# Deployment, backups and recovery

Operate the Caddy TLS stack and follow the database, encrypted-artifact and key recovery procedures.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

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

