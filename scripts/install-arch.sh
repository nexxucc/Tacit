#!/usr/bin/env bash
set -euo pipefail
# Checkout-scoped install; no public package publishing or executable replacement.
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v uv >/dev/null || { echo 'Install uv first.' >&2; exit 1; }
launcher_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
launcher="$launcher_dir/tacit-intent"
if [[ -e "$launcher" ]] || command -v tacit-intent >/dev/null; then
  echo 'tacit-intent already exists. Inspect it before installing this checkout.' >&2
  exit 1
fi
cd "$project_root"
if [[ "${1:-}" == '--audio' ]]; then
  uv sync --frozen --extra audio
else
  uv sync --frozen
fi
mkdir -p -- "$launcher_dir"
( set -o noclobber
  { printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'exec uv run --project %q --no-sync tacit "$@"\n' "$project_root"
  } > "$launcher"
)
chmod 755 -- "$launcher"
echo "Installed $launcher. Add $launcher_dir to PATH if needed."
echo 'For microphone/playback, install Arch portaudio and use --audio.'
