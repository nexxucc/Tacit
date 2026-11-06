# Backend setup and authentication

Configure a backend, run migrations and services, and connect a human CLI session.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Backend setup

Use Python 3.12–3.14, uv, PostgreSQL and a configured GitHub App. From the checkout:

```sh
uv sync --frozen
# Create .env from .env.example only if it does not already exist.
# Configure PostgreSQL, GitHub and a stable credential encryption key privately.
uv run --env-file .env alembic upgrade head
uv run --env-file .env tacit-api
# In another terminal:
uv run --env-file .env tacit-worker
```

Preserve the existing encryption key: replacing it makes retained encrypted credentials/objects unreadable. Keep secrets out of Git. The API does not automatically load `.env`. `/health/live` checks process availability; `/health/ready` checks backend readiness, not model quality or availability. Never use the knowledge database as a disposable test database.

```sh
export TACIT_API_URL=http://127.0.0.1:8000
uv run --no-sync tacit login
uv run --no-sync tacit whoami
uv run --no-sync tacit workspaces list
uv run --no-sync tacit repos list --workspace WORKSPACE_ID
```

Complete GitHub's device flow. Human credentials use an approved OS keyring without plaintext fallback; delegated reads require `TACIT_AGENT_TOKEN`. HTTPS is required except on loopback. API/schema versions are `1`; evaluator version is `typed-expression-v2`. Explicit incompatible declarations receive `426 upgrade_required`; versionless legacy peers follow the implemented compatibility bridge.

