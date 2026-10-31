"""Wire-contract versions, independent of package releases and database migrations."""

from collections.abc import Iterable

API_VERSION = "1"
SCHEMA_VERSION = "1"
EVALUATOR_VERSION = "typed-expression-v2"

API_VERSION_HEADER = "X-Tacit-API-Version"
SCHEMA_VERSION_HEADER = "X-Tacit-Schema-Version"
EVALUATOR_VERSION_HEADER = "X-Tacit-Evaluator-Version"

PROTOCOL_VERSIONS = (
    ("API", API_VERSION_HEADER, API_VERSION),
    ("schema", SCHEMA_VERSION_HEADER, SCHEMA_VERSION),
    ("evaluator", EVALUATOR_VERSION_HEADER, EVALUATOR_VERSION),
)


def protocol_headers() -> dict[str, str]:
    """Declare the exact wire contracts this client or server implements."""
    return {header: version for _, header, version in PROTOCOL_VERSIONS}


def compatibility_message(dimensions: Iterable[str] = ()) -> str:
    kinds = ", ".join(dimensions) or "API, schema or evaluator"
    return (
        f"Incompatible Tacit {kinds} version. "
        f"Supported versions: API {API_VERSION}, schema {SCHEMA_VERSION}, "
        f"evaluator {EVALUATOR_VERSION}. Upgrade the Tacit client and backend together; "
        "the request was not automatically retried."
    )
