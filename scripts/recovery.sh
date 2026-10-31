#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec uv run --project "$project_root" --env-file "$project_root/.env" --no-sync \
  python "$project_root/scripts/recovery.py" "$@"
