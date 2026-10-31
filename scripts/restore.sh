#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Only restore into an explicitly chosen empty deployment, never over a populated database.
: "${TACIT_RESTORE_DUMP:?Set path to the database dump}"
: "${TACIT_RESTORE_TARGET:?Set this to empty-development to acknowledge the target}"
[[ "$TACIT_RESTORE_TARGET" == 'empty-development' ]] || { echo 'Target must be empty-development' >&2; exit 1; }
[[ -f "$TACIT_RESTORE_DUMP" ]] || { echo 'Dump unavailable' >&2; exit 1; }
compose=(docker compose --env-file "$project_root/.env" -f "$project_root/deploy/compose.yaml")
"${compose[@]}" exec -T postgres pg_restore --list < "$TACIT_RESTORE_DUMP" > /dev/null
existing=$("${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U tacit -d tacit -Atc "
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
  (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema')) +
  (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') +
  (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
   WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema');")
[[ "$existing" == '0' ]] || { echo 'Refusing to restore over an existing schema.' >&2; exit 1; }
"${compose[@]}" exec -T postgres pg_restore -U tacit -d tacit --single-transaction --exit-on-error < "$TACIT_RESTORE_DUMP"
echo 'Database restored. Restore matching encrypted objects and stable encryption key before starting API/worker.'
