#!/usr/bin/env bash
set -euo pipefail
: "${TACIT_BACKUP_DIR:?Set a private backup directory}"
mkdir -p -- "$TACIT_BACKUP_DIR"
chmod 700 -- "$TACIT_BACKUP_DIR"
umask 077
stamp=$(date -u +%Y%m%dT%H%M%SZ)
# Back up immutable PostgreSQL knowledge; artifact objects and encryption key are separate.
docker compose -f deploy/compose.yaml exec -T postgres pg_dump -U tacit -d tacit -Fc > "$TACIT_BACKUP_DIR/tacit-$stamp.dump"
echo "Database backup: $TACIT_BACKUP_DIR/tacit-$stamp.dump"
echo 'Also back up the encrypted artifact store and the stable encryption key privately.'
