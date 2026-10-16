"""Private, identity/backend-scoped selection defaults; never stores credentials."""

import json
import os
from pathlib import Path

from pydantic import TypeAdapter
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import api_url


def filename() -> Path:
    return Path(os.environ.get("TACIT_CONFIG", str(Path.home() / ".config/tacit/config.json")))


def profile() -> tuple[str, str]:
    with TacitClient() as client:
        identity = client.request("GET", "/v1/auth/me")
    if not isinstance(identity, dict) or identity.get("agent") is not False:
        raise ClientError("Local defaults require a human credential")
    return api_url().rstrip("/"), str(identity["user_id"])


def read() -> dict[str, object]:
    path = filename()
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ClientError("Configuration must not be a symlink")
    return TypeAdapter(dict[str, object]).validate_json(path.read_text())


def save(workspace: str, repository: str | None = None) -> None:
    server, user = profile()
    data = read()
    key = server + ":" + user
    data[key] = {"workspace": workspace, "repository": repository}
    path = filename()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    from tacit_cli.spool import atomic

    atomic(path, json.dumps(data).encode())


def repository() -> str | None:
    try:
        server, user = profile()
        selection = read().get(server + ":" + user, {})
        value = selection.get("repository") if isinstance(selection, dict) else None
        return str(value) if value else None
    except (ClientError, OSError, ValueError):
        return None
