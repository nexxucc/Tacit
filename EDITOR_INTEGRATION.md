# Editor source selections

Build and use the VS Code adapter for explicit files, ranges and symbols with local source validation.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

Detailed material below is copied from the former README. Dated implementation and test notes describe their original verification stage; consult the [acceptance history](ACCEPTANCE_HISTORY.md) for later operator results.

### Editor source selections — 2026-11-03

The local VS Code adapter in `editors/vscode/` exports the active file, one contiguous selection, or a symbol supplied by the editor's language provider. It discovers the active file's Git checkout independently of workspace-folder order, prompts for a full source commit, and delegates validation/private-file creation to the installed Tacit CLI. No API credential, upload, microphone capture, model request or publication is involved in exporting context.

Build and install the local extension (Python standard library only; no npm dependencies):

```bash
cd /home/vansh/Tacit
mkdir -p output/editor
uv run --no-sync python scripts/package_editor.py output/editor/tacit-source-selection.vsix
code --install-extension output/editor/tacit-source-selection.vsix
```

The packager refuses to replace an existing archive. For a rebuild choose a fresh output filename. This is a local development extension, not a Marketplace release. In VS Code user settings, set `tacit.cliPath` to the installed executable, for example `/home/vansh/Tacit/.venv/bin/tacit` on this Arch checkout. The value is an executable path, not `uv run tacit` or another shell command. On Windows use the installed `tacit.exe` path. Workspace Trust is required; virtual documents and unsaved buffers are rejected. In remote VS Code sessions the executable and files must exist on the extension's workspace host.

Open a saved UTF-8 source file, then choose **Tacit: Export Active File Context**, **Tacit: Export Selected Lines Context**, or **Tacit: Export Symbol Context** from the command palette. The symbol picker requires a document-symbol provider for that language. Use a pushed, full 40-character SHA and save the exported JSON to a private local location outside version control. The adapter does not prove a commit has been pushed; the existing teaching API checks authoritative GitHub source access. Files are limited to 64 KiB and selections to 400 lines. Large files require a separate workflow; selecting fewer lines does not bypass the 64 KiB file limit.

The equivalent editor-independent command is:

```bash
tacit editor capture --root /path/to/checkout --commit FULL_40_CHARACTER_SHA \
  --file app/matcher.py --start-line 10 --end-line 35 --symbol match_route \
  --output /private/path/editor-context.json
```

Use the versioned export with grounded teaching, supplying the same commit and file scope:

```bash
tacit teaching extract --repo REPOSITORY_ID --commit FULL_40_CHARACTER_SHA \
  --file app/matcher.py --source-root /path/to/checkout \
  --editor-context /private/path/editor-context.json --text-file /private/path/intent.txt
```

`--editor-context` requires `--source-root` and cannot be combined with `--selections-file`. Import checks the exact commit, file scope, current disk hashes and dirty-file evidence before requesting the API. Re-export if the source changes. The existing human confirmation still controls extraction, followed by the existing clarification/review/draft/confirm workflow for publication. The JSON is local selection metadata, not a signed assertion or proof of symbol semantics. Dirty hashes identify local differences; local bytes and symbol names never replace verified commit content. Review line ranges against the pinned commit when the working file differs. The legacy `--selections-file` format remains available.

Verification: 16 focused Python tests passed; 12 Node adapter tests passed, including real CLI export; the VSIX installed and appeared in an isolated VS Code extension inventory. Tests cover source changes, commit/file mismatch, dirty hashes, escaped symlinks, root selection, bounds, exclusive output creation, shell-argument safety, symbol/range conversion, cancelled dialogs, untrusted workspaces and unsaved/external edits. A broader relevant Python regression run passed 122 tests with 48 conditional skips (PostgreSQL-only variants were not enabled for this focused run) and one existing Starlette warning. Ruff, formatting, strict mypy and diff checks passed; the AST graph was refreshed. The operator subsequently confirmed all three VS Code commands and the unsaved-edit, saved-dirty-file, overwrite, empty-selection and cancellation checks on Arch. The older-context import also returned the expected changed-file rejection. This marks the supplied Arch editor manual acceptance passed; native Windows/macOS checks remain pending. No paid provider request or knowledge publication was used. Run `uv run --no-sync pytest tests/test_editor_context.py` and `node --test editors/vscode/extension.test.js` to repeat these checks (the real CLI Node test uses this checkout's `.venv/bin/tacit` on Arch).


