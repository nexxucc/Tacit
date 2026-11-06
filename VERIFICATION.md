# Testing and evaluation

Run project checks and understand the independent evaluation and external acceptance requirements.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

## Verification and remaining acceptance

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest -ra
```

Set `TACIT_TEST_DATABASE_URL` only to a dedicated disposable PostgreSQL database to run concurrency/migration variants. Live Evon tests remain explicitly opt-in. The verified matrix used sanitized environments and temporary schemas; its disposable PostgreSQL container was removed afterward. Fixture providers do not establish live integration quality.

Remaining user/external gates:

- Native Windows/macOS installation, terminal behavior, Credential Manager/Keychain and microphone privacy prompts.
- Real microphones/speakers, device refusal/unplugging, sample-rate support and playback.
- Live Prisma short/long/incremental transcription, Timbre synthesis and measured credits; actual Evon quality and latency.
- Stable production DNS/TLS and the deferred four-user demonstration. Codex CLI and laptop/ngrok signed repository-addition/removal acceptance are recorded below; Claude Code testing is waived at the user’s request, without a compatibility claim.
- Human-authored 30-scenario evaluation with at least 10 held out, plus a bounded developer handoff study.
- Production-shaped application recovery, deployed storage permissions, power-loss behavior and actual S3 acceptance if selected.

Offline text teaching synchronization, two-account publication quorum and Arch VS Code selection have passed the operator-run checks recorded below. Native Windows/macOS acceptance and broader failure/deployment/quality gates remain separate. Team mappings/custom roles are implemented as described below; their two-account real-organization permission/revocation check has passed as recorded below. Snapshot export/lockfiles and opt-in remote MCP/OAuth are implemented as described below; live Codex remote OAuth discovery, pinned retrieval and server-side revocation have passed for the tested laptop/ngrok deployment.

## Human scenario and handoff evaluation

Status: preparation only; no quality or productivity result is established. Local CSV templates are in `output/human-evaluation/`: 20 teaching scenarios, 10 held-out scenarios, and a handoff results sheet. These local artifacts are not required for installation. Scenario statements and expected outcomes must be written and reviewed by a human module author before additional teaching; blank template rows are not authored scenarios. Existing published knowledge may already influence this evaluation, so identify that prior exposure rather than describing the dataset as a clean baseline.

The author records the scenario, facts, requested action, intended outcome, rationale and pinned source evidence. Include supported prohibitions, boundary cases, contradictory or missing facts, and cases where no definitive answer is justified. Keep the ten held-out scenarios and answer key out of teaching prompts and model-tuning/debugging; after using a case to guide fixes, retire it from the held-out set. Store answer keys privately and do not publish them as Tacit knowledge.

Before each run, pin the repository commit and published snapshot. Report exact context retrieval separately from deterministic typed-rule evaluation and optional Evon synthesis. The currently tested scheduler statement has no typed rule (`rule: null`); retrieving its text does not prove deterministic scenario evaluation. A text-only or Codex interpretation is also not evidence of Evon answer quality. Live Evon quality evaluation remains pending while its server is unavailable. Record output, expected outcome, citation validity, unsupported definitive answers, warnings, latency and provider usage for each evaluated case. Report the sample size and individual failures.

For handoff evaluation, define a bounded task and correctness rubric before timing. Prefer different participants with comparable experience or matched tasks with counterbalanced conditions; repeating the same task with the same person introduces learning bias. Compare source-only context with source plus Tacit context using the same code baseline, record time and correctness separately, and disclose participant count, task differences and prior exposure. No productivity gain is claimed before these measurements.


