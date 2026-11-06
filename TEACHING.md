# Teaching and retrieving knowledge

Create and review source-bound drafts, explicitly publish them, and retrieve immutable repository context.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Teaching and consumption

```sh
uv run --no-sync tacit teach --repo REPO_ID --commit PUSHED_COMMIT_SHA \
  --file path/to/source.py --text-file /path/to/intent.txt
uv run --no-sync tacit review DRAFT_ID --repo REPO_ID
uv run --no-sync tacit confirm DRAFT_ID --repo REPO_ID
uv run --no-sync tacit repos head --repo REPO_ID
uv run --no-sync tacit context --repo REPO_ID --snapshot SNAPSHOT_ID \
  --commit PUSHED_COMMIT_SHA --files path/to/source.py
```

Use full pushed 40-character commit SHAs and canonical repository-relative paths with `/`. `teach` creates a private draft; only explicit `confirm` publishes. Transcript corrections preserve the original evidence. `teaching extract/list/review/answer/draft/cancel/speak` provides grounded candidates, clarification, Textual review and optional speech; use command `--help` for required arguments. Source selections include dirty hashes but never turn uncommitted bytes into verified commit evidence.

`ask --reason` explicitly requests cited reasoning. Incomplete, stale or conflicting context causes abstention. `evaluate` applies only to supported confirmed scheduler rules and returns permitted, forbidden, unknown or conflict. It is not a proof about arbitrary scheduler implementations. `history` follows immutable revision lineage.

```sh
uv run --no-sync tacit-mcp
# Or:
uv run --no-sync tacit mcp
```

Local MCP exposes `list_workspaces`, `list_repositories`, `get_context`, `ask_repository`, `evaluate_scenario` and `get_decision_history`. Configure a scoped agent credential privately in the host environment. MCP stdout contains protocol messages; diagnostics use stderr. Exact agent reads require zero STT/TTS calls. Optional Streamable HTTP with repository-scoped OAuth is implemented; see [Remote MCP with OAuth](MCP.md#remote-mcp-with-oauth).

