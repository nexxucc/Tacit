"""Worker composition seam. No jobs may be acknowledged before durable wiring exists."""

import sys


def main() -> None:
    print(
        "Worker unavailable: transactional outbox relay and idempotent handlers are not implemented",
        file=sys.stderr,
    )
    raise SystemExit(1)
