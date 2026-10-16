#!/usr/bin/env bash
set -euo pipefail
# Only restore into an explicitly chosen empty deployment, never over a populated database.
: "${TACIT_RESTORE_DUMP:?Set path to the database dump}"
: "${TACIT_RESTORE_TARGET:?Set this to empty-development to acknowledge the target}"
[[ "$TACIT_RESTORE_TARGET" == 'empty-development' ]] || { echo 'Target must be empty-development' >&2; exit 1; }
[[ -f "$TACIT_RESTORE_DUMP" ]] || { echo 'Dump unavailable' >&2; exit 1; }
existing=$(docker compose -f deploy/compose.yaml exec -T postgres psql -U tacit -d tacit -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
[[ "$existing" == '0' ]] || { echo 'Refusing to restore over an existing schema.' >&2; exit 1; }
docker compose -f deploy/compose.yaml exec -T postgres pg_restore -U tacit -d tacit --exit-on-error < "$TACIT_RESTORE_DUMP"
echo 'Database restored. Restore matching encrypted objects and stable encryption key before starting API/worker.'
