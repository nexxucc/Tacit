#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${TACIT_BACKUP_DIR:?Set a private backup directory}"
umask 077
[[ ! -L "$TACIT_BACKUP_DIR" ]] || { echo 'Backup directory must not be a symlink.' >&2; exit 1; }
mkdir -p -- "$TACIT_BACKUP_DIR"
[[ -O "$TACIT_BACKUP_DIR" && $(stat -c %a -- "$TACIT_BACKUP_DIR") == '700' ]] || {
  echo 'Backup directory must be owned by you with mode 700.' >&2
  exit 1
}
compose=(docker compose --env-file "$project_root/.env" -f "$project_root/deploy/compose.yaml")
stamp=$(date -u +%Y%m%dT%H%M%SZ)
# Back up immutable PostgreSQL knowledge; artifact objects and encryption key are separate.
partial=$(mktemp "$TACIT_BACKUP_DIR/.tacit-$stamp.XXXXXXXX.partial")
trap 'rm -f -- "$partial"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"${compose[@]}" exec -T postgres pg_dump -U tacit -d tacit -Fc > "$partial"
"${compose[@]}" exec -T postgres pg_restore --list < "$partial" > /dev/null
name=${partial##*/}
archive="$TACIT_BACKUP_DIR/${name#.}"
archive="${archive%.partial}.dump"
# Hard-link publication refuses collisions atomically; partial files never look like backups.
ln -- "$partial" "$archive"
echo "Database backup: $archive"
echo 'Also back up the encrypted artifact store and the stable encryption key privately.'
