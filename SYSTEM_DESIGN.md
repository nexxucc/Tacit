# System design and trust boundaries

Understand the workspace packages, authoritative storage, source bindings and human-only publication model.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Architecture and trust boundaries

- `packages/tacit_contracts`: shared Pydantic contracts and API/schema/evaluator versions.
- `packages/tacit_domain`: authorization and bounded deterministic knowledge/rule evaluation.
- `packages/tacit_client`: authenticated HTTP/WebSocket SDK and secure credential handling.
- `packages/tacit_cli`: native recording, durable recovery, teaching, review and retrieval.
- `packages/tacit_mcp`: six read-only local stdio tools.
- `services/api`: GitHub identity, publication, source grounding, provider adapters and encrypted storage.
- `services/worker`: transactional outbox projection, reconciliation and retention.
- `migrations`: PostgreSQL schema and immutability constraints.
- `deploy`, `scripts`, `site`: deployment/recovery/install tooling and static landing page.

PostgreSQL is the shared authority; SQLite is fixture-only. Prisma performs speech-to-text, Evon performs optional grounded reasoning, and Timbre synthesizes optional clarification speech. Provider keys remain on the backend. The product has no browser recorder or web dashboard.

Each personal GitHub account or organization installation maps to a workspace. GitHub eligibility and explicit scoped roles both constrain access. Workspace administration alone does not permit knowledge publication. Learners and all delegated agents remain read-only, including agents acting for maintainers. Outside collaborators require scoped invitations. Revocation is checked during long operations and before delivering results.

Drafts bind exact repository files and commits. Human confirmation uses the reviewed digest and expected parent snapshot. Publications atomically create immutable revisions, snapshot manifests, audit and outbox records. Concurrent stale publications fail rather than overwrite history. Reuse an operation ID only with its original unchanged input. Uncertain paid operations are never silently replayed.

