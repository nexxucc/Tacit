#!/usr/bin/env bash
set -euo pipefail
# Checkout client install only: backend services stay on their own host.
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != '--audio' ) ]]; then
  echo 'Usage: install-macos.sh [--audio]' >&2
  exit 2
fi
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Run this installer in a native macOS terminal. Use install-arch.sh on Arch.' >&2
  exit 1
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
uv_path=$(type -P uv) || {
  echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
}
# Resolve relative PATH entries before writing a launcher used from other directories.
uv_path=$(cd -- "$(dirname -- "$uv_path")" && pwd)/$(basename -- "$uv_path")
launcher_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
for name in tacit-intent tacit-mcp; do
  if [[ -e "$launcher_dir/$name" || -L "$launcher_dir/$name" ]] || command -v "$name" >/dev/null; then
    echo "$name already exists. Inspect it before installing this checkout." >&2
    exit 1
  fi
done
cd "$project_root"
sync_args=(sync --frozen --package tacit-cli --package tacit-mcp --no-default-groups)
if [[ "${1:-}" == '--audio' ]]; then
  sync_args+=(--extra audio)
fi
"$uv_path" "${sync_args[@]}"
mkdir -p -- "$launcher_dir"
created=()
complete=false
cleanup() {
  if [[ "$complete" != true ]]; then
    for path in "${created[@]-}"; do
      if [[ -n "$path" ]]; then rm -f -- "$path"; fi
    done
  fi
}
trap cleanup EXIT
for name in tacit-intent tacit-mcp; do
  package=tacit-cli
  entrypoint=tacit
  if [[ "$name" == tacit-mcp ]]; then package=tacit-mcp; entrypoint=tacit-mcp; fi
  # Noclobber also protects against a launcher created after the preflight above.
  ( set -o noclobber
    { printf '#!/usr/bin/env bash\nset -euo pipefail\n'
      printf 'exec %q run --project %q --package %q --no-sync %q "$@"\n' \
        "$uv_path" "$project_root" "$package" "$entrypoint"
    } > "$launcher_dir/$name"
  )
  created+=("$launcher_dir/$name")
  chmod 755 -- "$launcher_dir/$name"
done
complete=true
printf 'Installed client launchers in %s. Add this directory to PATH if needed.\n' "$launcher_dir"
echo 'Keep this checkout and uv at their current paths; these launchers refer to them.'
echo 'For microphone/playback rerun installation with --audio after inspecting/removing these launchers.'
echo 'macOS microphone access must be allowed for your terminal in System Settings > Privacy & Security > Microphone.'
echo 'The sounddevice macOS wheel supplies PortAudio; no Homebrew or sudo operation is performed.'
