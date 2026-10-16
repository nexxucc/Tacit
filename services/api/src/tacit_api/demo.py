"""Synthetic fixture only. Not a production repository or credential store."""

from tacit_contracts.models import Decision, Evidence, Predicate, Role, Rule
from tacit_domain.access import Grant, Principal
from tacit_domain.knowledge import Repository, Snapshot

DEMO_COMMIT = "demo-commit-1"


class DemoStore:
    def repositories(self) -> tuple[Repository, ...]:
        return (
            Repository("scheduler", "demo", "demo/scheduler"),
            Repository("scheduler-second", "demo-second", "demo-second/scheduler"),
        )

    def snapshot(self, repository: str, snapshot: str) -> Snapshot | None:
        if (repository, snapshot) == ("scheduler-second", "snapshot-second"):
            return Snapshot(
                snapshot,
                repository,
                DEMO_COMMIT,
                (
                    Decision(
                        id="second-private",
                        revision_id="second-revision",
                        statement="Second workspace intent is isolated.",
                        files=("worker.py",),
                        source_commit=DEMO_COMMIT,
                        evidence=(
                            Evidence(
                                decision_id="second-private",
                                revision_id="second-revision",
                                segment_id="second-evidence",
                                kind="text",
                            ),
                        ),
                    ),
                ),
            )
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

    def history(
        self, repository: str, snapshot: str, decision: str, cursor: int, limit: int
    ) -> tuple[Decision, ...]:
        snap = self.snapshot(repository, snapshot)
        return (
            tuple(d for d in snap.decisions if d.id == decision)[cursor : cursor + limit]
            if snap
            else ()
        )


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


def multi_user_principals() -> dict[str, Principal]:
    """Distinct synthetic identities for scripted read/denial demonstrations."""

    def user(uid: str, role: Role, second: bool = False) -> Principal:
        return Principal(
            uid,
            False,
            frozenset({"demo", "demo-second"} if second else {"demo"}),
            frozenset({"scheduler", "scheduler-second"} if second else {"scheduler"}),
            (Grant("demo", role),) + ((Grant("demo-second", Role.LEARNER),) if second else ()),
        )

    return {
        "demo-maintainer-a": user("maintainer-a", Role.MAINTAINER, True),
        "demo-maintainer-b": user("maintainer-b", Role.MAINTAINER),
        "demo-learner-a": user("learner-a", Role.LEARNER),
        "demo-learner-b": user("learner-b", Role.LEARNER),
    }
