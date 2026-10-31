"""Explicit local source selections; dirty hashes are context, never verified commit content."""

import hashlib
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from tacit_contracts.models import Selection


def capture(root: Path, commit: str, paths: list[str]) -> tuple[Selection, ...]:
    from tacit_contracts.models import DraftRequest

    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Source commit must be a full 40-character Git commit SHA")
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
        try:
            size = subprocess.run(
                ["git", "cat-file", "-s", f"{commit}:{relative}"],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=True,
            ).stdout
            if int(size) > 65536:
                raise ValueError("Use explicit smaller selections for large committed files")
            stored = subprocess.run(
                ["git", "show", "--end-of-options", f"{commit}:{relative}"],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            raise ValueError("Cannot read the selected source from this Git commit") from None
        result.append(
            Selection(
                path=relative,
                captured_at=datetime.now(UTC).isoformat(),
                dirty_sha256=hashlib.sha256(local).hexdigest() if local != stored else None,
            )
        )
    return tuple(result)
