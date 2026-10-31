"""Private, identity/backend-scoped selection defaults; never stores credentials."""

import json
from pathlib import Path

from pydantic import TypeAdapter
from tacit_client.client import ClientError, TacitClient
from tacit_client.credentials import api_url

from tacit_cli.local_storage import config_path, private_directory, reject_redirected_path


def filename() -> Path:
    return config_path()


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
    try:
        reject_redirected_path(path)
    except OSError:
        raise ClientError("Configuration must not use a symlink or reparse point") from None
    return TypeAdapter(dict[str, object]).validate_json(path.read_text(encoding="utf-8"))


def save(workspace: str, repository: str | None = None) -> None:
    server, user = profile()
    data = read()
    key = server + ":" + user
    data[key] = {"workspace": workspace, "repository": repository}
    path = filename()
    if not path.parent.exists():
        private_directory(path.parent)
    # An explicit TACIT_CONFIG can live in an existing shared directory. Keep
    # new directories private without changing permissions on user-owned ones.
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
