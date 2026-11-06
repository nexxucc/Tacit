# Access control and publication approvals

Configure GitHub team mappings, custom content roles and distinct-human publication quorums.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## GitHub team mappings and custom roles

Workspace administrators can define named content roles and map GitHub organization teams to repository-scoped or workspace-wide content roles. These are additive grants: removing a team grant does not remove a separate direct grant. Current GitHub repository visibility, active membership, enabled installation/repository state, and delegated-token scope remain mandatory. Agents remain read-only even when their human's custom/team role resolves to maintainer.

Custom roles support two explicit permissions:

| Permission | Capability |
|---|---|
| `knowledge.read` | Read confirmed knowledge/history, ask questions and evaluate scenarios |
| `knowledge.write` | Human maintainer capabilities, including teaching, voice, correction, draft review and publication; requires `knowledge.read` |

Custom roles do not grant workspace administration. Team mappings can target the built-in `learner` or `maintainer` content role, or one custom role. Built-in administrator assignment and last-administrator protection are unchanged. Existing learners keep their baseline read access unless that separate grant is explicitly revoked; a team mapping cannot act as a deny rule. A user granted `knowledge.write` is explicitly granted maintainer-equivalent content authority, while an administrator-only role still cannot publish knowledge.

Apply migration `0008` before restarting the API/worker with this code. No existing built-in role assignments are rewritten. The following example uses the existing test organization/repository; run it only as a human workspace administrator after creating the intended team in GitHub:

```bash
cd /home/vansh/Tacit
uv run --env-file .env --no-sync alembic upgrade head
# Restart the API/worker with the normal deployment configuration before continuing.

export TACIT_API_URL=https://zodiac-bubbling-stereo.ngrok-free.dev
TACIT_WORKSPACE=gh-343680840
TACIT_REPOSITORY=gh-1422572999

# Human sign-in, if the current CLI credential has expired.
uv run --no-sync tacit login

# Create a named maintainer-equivalent content role.
uv run --no-sync tacit custom-roles put reviewers \
  --workspace "$TACIT_WORKSPACE" \
  --permission knowledge.read --permission knowledge.write \
  --expected-version 0 --description "Human repository knowledge maintainers"

# Map an existing GitHub team whose slug is knowledge-maintainers.
uv run --no-sync tacit team-roles add knowledge-maintainers \
  --workspace "$TACIT_WORKSPACE" --custom-role reviewers \
  --repo "$TACIT_REPOSITORY"

uv run --no-sync tacit custom-roles list --workspace "$TACIT_WORKSPACE"
uv run --no-sync tacit custom-roles assignments --workspace "$TACIT_WORKSPACE"
uv run --no-sync tacit team-roles list --workspace "$TACIT_WORKSPACE"
uv run --no-sync tacit team-roles effective --workspace "$TACIT_WORKSPACE"
```

Individual custom-role assignment uses `tacit custom-roles grant USER_ID ROLE_ID --workspace WORKSPACE_ID --repo REPOSITORY_ID`; `custom-roles revoke` uses the same arguments. Omitting `--repo` makes an assignment or mapping workspace-wide, still restricted to currently accessible enabled repositories. `team-roles add --role learner` or `--role maintainer` targets a built-in content role instead of `--custom-role`. Remove a mapping with `team-roles remove MAPPING_ID --workspace WORKSPACE_ID`, using the ID from `team-roles list`.

Role edits require the current `--expected-version`; creation requires zero. Stale edits/deletes return a conflict. A role in use cannot be deleted until its assignments and mappings are removed. `custom-roles delete ROLE_ID --workspace WORKSPACE_ID --expected-version VERSION` retires that ID permanently, preventing delayed requests from affecting a recreated role. Active roles are limited to 100, individual custom assignments to 1,000 and team mappings to 20 per workspace. Effective authentication bounds distinct team checks to 64 across its eligible scopes and fails closed if that budget is exceeded.

GitHub team creation and membership changes remain GitHub operations; Tacit never modifies them. The GitHub App needs organization **Members: read** permission. Mapping creation resolves the selected team slug to its numeric team ID and validates its organization ID. Runtime checks use the numeric organization/team membership endpoint and the authenticated user's freshly verified login/ID, preserving identity across renames and preventing a recreated slug from inheriting an old mapping. Only active membership grants a role; pending/absent memberships do not. Each distinct mapped team is checked once per principal evaluation, without a cross-request team-membership cache. A membership verification error fails content authorization closed; built-in administrator policy-management routes can still remove a broken mapping without requiring that team's API, while retaining ordinary GitHub eligibility and local administrator checks.

Publication and other local write checks reload custom permissions and mapping existence under the workspace lock. Mapping removal and permission downgrades therefore stop stale local grants. External team membership is refreshed on HTTP authentication, within the existing live-audio background refresh interval/deadline, and at the durable-upload reauthentication interval. Local audio checks do not make a GitHub team request per chunk. These transport windows are distinct from immediate local policy revocation. Configuration changes are audited; derived team grants are never copied into permanent individual role rows.

The administrative API adds `/v1/workspaces/{wid}/custom-roles`, `/custom-role-assignments` and `/team-role-mappings`, with scoped update/revoke/delete routes. `/effective-access` reports the authenticated user's current normalized grants and source provenance. MCP continues to expose exactly six read-only tools; no policy-management tool was added. Existing role and membership commands remain supported.

Implementation and automated verification are described below. Live team enrollment/removal was a separate operator acceptance gate and subsequently passed as recorded below. No live organization policy, membership, repository permissions or database schema was changed during automated development; migration and temporary Tacit policy changes were performed later during the authorized live check.

Automated verification on 2026-11-03 passed the full Python 3.14.8 regression suite: **753 passed, 32 skipped**, with the three existing Starlette deprecation warnings. After adding the final administrator-budget recovery cases, the focused team/custom-role and migration suite passed **73 tests, 2 skipped** on each of Python 3.12.15, 3.13.17 and 3.14.8. The two focused skips are SQLite concurrency/socket cases whose PostgreSQL counterparts ran; the full-suite skips also include the six opt-in live Evon cases. The full run collected 785 cases before the final two feature cases were added; this distinction explains the subsequent collection-count increase.

Coverage includes human custom-role teaching/publication, read-only custom roles without a built-in content grant, agent write refusal, tenant/repository isolation, stale versions and retired role IDs, pending/removed/recreated teams, rename-safe numeric identity, malformed/provider-failure handling, direct-grant preservation, one membership lookup per distinct team, last-administrator protection, concurrent policy updates and duplicate mapping requests, stale-principal rejection, revocation during an open durable voice upload, safe cleanup after suspension/repository disablement, and administrator recovery from broken or over-budget mappings. CLI commands and composite database constraints are exercised. These are fixtures and local PostgreSQL/socket checks, not a live GitHub team demonstration.

Ruff, formatting, strict mypy (55 source modules), frozen-lock consistency, diff checks, and API/CLI/contracts/domain wheel and source builds passed. The wheels include the new role modules, and the code graph was refreshed. Logs and JUnit reports are local in `/tmp/tacit-team-role-regression/`; a safe source-hash/result receipt is retained in `output/team-role-check/results.json`. The disposable PostgreSQL test container was removed. At the end of automated development, migration `0008` had not yet been applied to the real application database and no live GitHub team or role policy had been changed. Live acceptance preparation subsequently applied the migration, as recorded below; the two-account live permission/revocation check subsequently passed as recorded below. Changes remain uncommitted; only README was edited among Markdown source documents during this implementation.


### Multi-maintainer publication quorum — 2026-11-03

Repositories may require one to five distinct current human maintainers for publication. The default is **one**, preserving ordinary human confirmation. The final confirmer counts once; other counted people must have explicitly approved that exact draft. The author may participate, but repeated approval, multiple credentials for the same person, and approving one's own draft do not create additional people. Workspace administration alone grants neither approval nor publication permission. Maintainer-equivalent custom/team roles are eligible; agents cannot approve, withdraw approvals or publish.

Publication policy is repository-scoped and managed only by explicit human workspace administrators. Updates require the current policy version. Every update, including a change back to an earlier threshold, increments the version and invalidates previous approvals for future publications. An administrator can explicitly lower the threshold; this is audited policy administration, not a restriction that requires the existing publication quorum. Published snapshots remain immutable.

An approval binds a human ID and the approving human credential's hash to the immutable draft digest, its bound base snapshot and the policy version. No raw credential is stored in the approval or exposed in status responses. Current GitHub eligibility, repository access, team membership and normalized content authority are rechecked before publication; credential expiry/revocation, local permissions, mapping existence and current policy are checked again under the shared publication workspace lock. Approval withdrawal and policy/permission changes serialize with publication. External-provider verification precedes the lock; external changes that occur after that verification are subject to the same unavoidable observation window as ordinary GitHub-backed publication.

A non-confirmer approval remains bound to its approving credential. If that credential expires or is revoked, that person must sign in and approve again. Approval status lists **recorded** reviews, not a promise that quorum is currently satisfied. Read-only knowledge queries and the six MCP tools do not expose approval or publication actions. Up to twenty distinct recorded reviewers are allowed per draft; if stale reviews exhaust that bound, prepare and review a new draft. Fresh eligibility checks stop once enough additional eligible reviewers have been found; the final locked check may still require a retry if local authority changes meanwhile.

API endpoints:

- `GET/PUT /v1/repos/{repository_id}/publication-policy`: human administrator policy inspection/update, with `required_maintainers` and `expected_version` on PUT.
- `GET/POST/DELETE /v1/repos/{repository_id}/teaching-sessions/{draft_id}/approvals`: maintainer inspection, exact-review approval, or withdrawal of the caller's own approval. POST includes `policy_version`, `review_digest` and `expected_snapshot_id`.
- Existing `POST .../publications` still requires explicit human confirmation, matching draft/base and a unique confirmation operation ID. Insufficient current approvals return HTTP 409 `quorum_required` without changing knowledge. Receipt retries preserve the original result; approval checks do not republish a draft.

Migration `0009` adds `publication_policies` and `draft_approvals` with composite repository/draft foreign keys, user/credential foreign keys and policy bounds. Apply it with writers stopped and a verified private backup before starting this API against an existing database. This development task tests migrations in disposable databases; it does not migrate the running working deployment automatically. Do not downgrade away approval tables in a production deployment as a way to bypass quorum.

Human-operated CLI flow after migration and API restart (replace the placeholders with a disposable test repository and reviewed draft):

```bash
# Human administrator: inspect policy; an unconfigured repository reports version 0.
uv run --no-sync tacit approvals policy --repo REPOSITORY_ID
uv run --no-sync tacit approvals set-policy --repo REPOSITORY_ID \
  --required 2 --expected-version 0

# First human maintainer creates a source-bound draft using the existing teach workflow.
# A different human maintainer reviews and explicitly approves that draft under their own login.
uv run --no-sync tacit review DRAFT_ID --repo REPOSITORY_ID
uv run --no-sync tacit approvals approve DRAFT_ID --repo REPOSITORY_ID
uv run --no-sync tacit approvals list DRAFT_ID --repo REPOSITORY_ID

# A different current human maintainer confirms; their confirmation supplies the second person.
uv run --no-sync tacit confirm DRAFT_ID --repo REPOSITORY_ID

# Before publication, a reviewer may withdraw their own approval.
uv run --no-sync tacit approvals withdraw DRAFT_ID --repo REPOSITORY_ID
```

Review and approve under each person's own human login. Do not use an agent token to impersonate a maintainer or publish test narration to a real project's knowledge. Approval itself does not publish and does not invoke Evon, Prisma or Timbre. The confirmer CLI allows a longer request timeout for fresh reviewer verification. A failed/uncertain confirmation should reuse its printed operation ID, as before.

Verification: full Python 3.14.8 regression with disposable PostgreSQL 17 passed **816 tests, 33 skipped, zero failures/errors** (849 collected, 330.87 seconds), with three pre-existing Starlette deprecation warnings. Twenty-seven skips are SQLite variants whose PostgreSQL counterparts ran; six require explicit live Evon configuration. Final quorum/migration checks passed **47 tests with one SQLite concurrency skip** on each of Python 3.12.15, 3.13.17 and 3.14.8. These include distinct-human counting, repeated self-approval, exact content/base/policy bindings, withdrawal, credential expiry/revocation, current GitHub access, custom-role downgrades, provider failure, races between preflight and local commit, three-person quorum, CLI confirmation, at-most-once concurrent publication, receipt retries and migration roundtrip. The final focused checks cover the last fresh-reviewer short-circuit optimization after the full regression run began.

Ruff, formatting, strict mypy (58 source modules), lockfile consistency and diff checks passed. CLI/API/contracts wheels and source distributions built successfully. The AST graph was refreshed. Private/local safe receipts are under `output/quorum-check/`; detailed full regression logs/JUnit are under `/tmp/tacit-quorum-regression.RlEBpH/`. The dedicated test PostgreSQL container was removed after verification; the working database was neither migrated nor otherwise modified for this task. No live GitHub/Gnani/Evon calls, real knowledge publication, commit or push were performed. Live two-account publication/quorum acceptance remains pending; the earlier team-role permission demonstration is separate. Offline teaching synchronization remains unimplemented.


