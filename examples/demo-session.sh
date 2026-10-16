#!/usr/bin/env bash
set -euo pipefail
# Run only after starting an explicit loopback demo API on its own port.
: "${TACIT_API_URL:=http://127.0.0.1:8000}"
export TACIT_API_URL
for fixture in demo-maintainer-a demo-maintainer-b demo-learner-a demo-learner-b; do
  TACIT_TOKEN="$fixture" uv run --no-sync tacit whoami
  TACIT_TOKEN="$fixture" uv run --no-sync tacit context --repo scheduler --snapshot snapshot-1 --commit demo-commit-1
 done
TACIT_TOKEN=demo-maintainer-a uv run --no-sync tacit context --repo scheduler-second --snapshot snapshot-second --commit demo-commit-1
TACIT_AGENT_TOKEN=demo-agent uv run --no-sync tacit context --agent --repo scheduler --snapshot snapshot-1 --commit demo-commit-1
