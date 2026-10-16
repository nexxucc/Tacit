"""Finite scheduler domain used for clarification, never whole-program verification."""

from itertools import product

from tacit_contracts.models import Scalar

DOMAINS: dict[str, tuple[Scalar, ...]] = {
    "worker": ("worker-1", "worker-2"),
    "lease_expired": (False, True),
    "cancelled": (False, True),
    "completed": (False, True),
    "owns_attempt": (False, True),
    "attempt": (0, 1),
}
ACTIONS = ("compute", "upload", "publish_result", "retry")


def scenarios() -> tuple[dict[str, Scalar], ...]:
    return tuple(dict(zip(DOMAINS, values, strict=True)) for values in product(*DOMAINS.values()))


def valid_facts(facts: dict[str, Scalar]) -> bool:
    return all(
        key in DOMAINS and any(type(value) is type(v) and value == v for v in DOMAINS[key])
        for key, value in facts.items()
    )
