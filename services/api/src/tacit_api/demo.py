"""Synthetic fixture only. Not a production repository or credential store."""

from tacit_contracts.models import Decision, Evidence, Predicate, Role, Rule
from tacit_domain.access import Grant, Principal
from tacit_domain.knowledge import Repository, Snapshot

DEMO_COMMIT = "demo-commit-1"


class DemoStore:
    def repositories(self) -> tuple[Repository, ...]:
        return (Repository("scheduler", "demo", "demo/scheduler"),)

    def snapshot(self, repository: str, snapshot: str) -> Snapshot | None:
        if (repository, snapshot) != ("scheduler", "snapshot-1"):
            return None
        decision = Decision(
            id="lease-publication",
            revision_id="revision-1",
            statement="An expired worker must not publish its result.",
            files=("scheduler/worker.py",),
            source_commit=DEMO_COMMIT,
            evidence=(
                Evidence(
                    decision_id="lease-publication",
                    revision_id="revision-1",
                    segment_id="segment-1",
                ),
            ),
            rule=Rule(when=(Predicate(field="lease_expired", equals=True),), outcome="forbidden"),
        )
        return Snapshot(snapshot, repository, DEMO_COMMIT, (decision,))


def demo_principals() -> dict[str, Principal]:
    def principal(role: Role, agent: bool = False) -> Principal:
        return Principal(
            "demo-user",
            agent,
            frozenset({"demo"}),
            frozenset({"scheduler"}),
            (Grant("demo", role),),
        )

    return {
        "demo-learner": principal(Role.LEARNER),
        "demo-maintainer": principal(Role.MAINTAINER),
        "demo-agent": principal(Role.MAINTAINER, True),
    }
