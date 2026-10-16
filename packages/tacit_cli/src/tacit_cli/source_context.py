"""Explicit local source selections; dirty hashes are context, never verified commit content."""

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from tacit_contracts.models import Selection


def capture(root: Path, commit: str, paths: list[str]) -> tuple[Selection, ...]:
    from tacit_contracts.models import DraftRequest

    canonical = DraftRequest.valid_files(tuple(paths))
    root = root.resolve()
    result = []
    for relative in canonical:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Source path must be a file inside the selected checkout")
        with path.open("rb") as file:
            local = file.read(65537)
        if len(local) > 65536:
            raise ValueError("Use explicit smaller selections for large local files")
        stored = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=root,
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        result.append(
            Selection(
                path=relative,
                captured_at=datetime.now(UTC).isoformat(),
                dirty_sha256=hashlib.sha256(local).hexdigest() if local != stored else None,
            )
        )
    return tuple(result)
