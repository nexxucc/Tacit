"""Local editor context export. No authentication, network, or publication."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import Field
from tacit_contracts.models import Contract, Selection

from tacit_cli.local_storage import create_private_file
from tacit_cli.source_context import capture

app = typer.Typer(help="Export explicit editor selections locally; no provider calls.")


class EditorContext(Contract):
    version: Literal[1] = 1
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    selections: tuple[Selection, ...] = Field(min_length=1, max_length=20)
    local_sha256: dict[str, str]


def check_root(root: Path) -> None:
    try:
        actual = (
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root,
                capture_output=True,
                check=True,
                timeout=10,
            )
            .stdout.decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Select the Git checkout root") from None
    if Path(actual).resolve() != root.resolve():
        raise ValueError("Select the Git checkout root, not a subdirectory")


def file_hash(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Source path must be a file inside the selected checkout")
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Editor context supports files up to 64 KiB")
    return hashlib.sha256(data).hexdigest()


def load_context(path: Path, root: Path, commit: str, files: list[str]) -> tuple[Selection, ...]:
    with path.open("rb") as stream:
        data = stream.read(131073)
    if len(data) > 131072:
        raise ValueError("Editor context is too large")
    context = EditorContext.model_validate_json(data)
    check_root(root)
    if context.source_commit != commit:
        raise ValueError("Editor context is bound to a different source commit")
    if set(files) != {s.path for s in context.selections}:
        raise ValueError("Editor context files differ from the requested files")
    if set(context.local_sha256) != set(files):
        raise ValueError("Editor context requires a hash for every file")
    current = {s.path: s for s in capture(root, commit, files)}
    for selection in context.selections:
        if file_hash(root, selection.path) != context.local_sha256[selection.path]:
            raise ValueError("Selected file changed since editor capture; export it again")
        if selection.dirty_sha256 != current[selection.path].dirty_sha256:
            raise ValueError("Editor dirty-file evidence does not match this checkout")
    return context.selections


@app.command("capture")
def export_context(
    root: Annotated[Path, typer.Option("--root")],
    commit: Annotated[str, typer.Option("--commit")],
    file: Annotated[str, typer.Option("--file")],
    output: Annotated[Path, typer.Option("--output")],
    start_line: int = 1,
    end_line: int | None = None,
    symbol: str | None = None,
    expected_sha256: str | None = None,
) -> None:
    try:
        check_root(root)
        digest = file_hash(root, file)
        baseline = capture(root, commit, [file])[0]
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("Editor buffer and saved file differ; save and export again")
        with (root / file).open("rb") as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError("Selected file changed during capture; export it again")
        lines = max(1, len(content.splitlines()))
        if end_line is None:
            end_line = lines
        selection = Selection.model_validate(
            baseline.model_dump()
            | {"start_line": start_line, "end_line": end_line, "symbol": symbol}
        )
        if end_line > lines:
            raise ValueError("Selection extends beyond the saved file")
        context = EditorContext(
            source_commit=commit, selections=(selection,), local_sha256={file: digest}
        )
        # Recheck capture before writing: changing files must never look clean.
        load_data = context.model_dump_json()
        if digest != file_hash(root, file):
            raise ValueError("Selected file changed during capture; export it again")
        with create_private_file(output) as stream:
            stream.write((load_data + "\n").encode())
        typer.echo(json.dumps({"output": str(output), "source_commit": commit, "path": file}))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
