from dataclasses import replace

import pytest
from tacit_api.demo import DemoStore, demo_principals
from tacit_contracts.models import ContextRequest, Outcome, Predicate, Role, Rule, ScenarioRequest
from tacit_domain.access import DomainError, Grant, authorize
from tacit_domain.knowledge import KnowledgeService
from tacit_domain.rules import evaluate


def test_read_only_learner_and_agent() -> None:
    for key in ("demo-learner", "demo-agent"):
        p = demo_principals()[key]
        authorize(p, "demo", "scheduler")
        with pytest.raises(DomainError, match="Human maintainer"):
            authorize(p, "demo", "scheduler", write=True)
    authorize(demo_principals()["demo-maintainer"], "demo", "scheduler", write=True)


def test_admin_is_not_implicitly_author() -> None:
    p = replace(demo_principals()["demo-maintainer"], grants=(Grant("demo", Role.ADMIN),))
    with pytest.raises(DomainError):
        authorize(p, "demo", "scheduler", write=True)


def test_scoped_union_and_provider_ceiling() -> None:
    p = replace(
        demo_principals()["demo-learner"],
        grants=(Grant("demo", Role.LEARNER), Grant("demo", Role.MAINTAINER, "other")),
    )
    with pytest.raises(DomainError):
        authorize(p, "demo", "scheduler", write=True)
    p = replace(p, grants=p.grants + (Grant("demo", Role.MAINTAINER, "scheduler"),))
    authorize(p, "demo", "scheduler", write=True)
    with pytest.raises(DomainError):
        authorize(replace(p, github_repositories=frozenset()), "demo", "scheduler")
    with pytest.raises(DomainError):
        authorize(p, "other-workspace", "scheduler")


def test_rules_preserve_unknown_and_conflict() -> None:
    snap = DemoStore().snapshot("scheduler", "snapshot-1")
    assert snap is not None
    rules = snap.decisions
    assert evaluate(rules, {"lease_expired": True})[0] == Outcome.FORBIDDEN
    for facts in ({}, {"lease_expired": False}, {"lease_expired": 1}, {"lease_expired": "true"}):
        assert evaluate(rules, facts)[0] == Outcome.UNKNOWN
    opposing = rules[0].model_copy(
        update={
            "revision_id": "revision-2",
            "rule": Rule(
                when=(Predicate(field="lease_expired", equals=True),), outcome="permitted"
            ),
        }
    )
    assert evaluate(rules + (opposing,), {"lease_expired": True})[0] == Outcome.CONFLICT


def test_commit_mismatch_and_source_filter() -> None:
    service = KnowledgeService(DemoStore())
    p = demo_principals()["demo-agent"]
    stale = service.context(
        p, "scheduler", ContextRequest(snapshot_id="snapshot-1", source_commit="new-commit")
    )
    assert stale.status == "unknown" and not stale.decisions
    result = service.evaluate(
        p,
        "scheduler",
        ScenarioRequest(
            snapshot_id="snapshot-1", source_commit="new-commit", facts={"lease_expired": True}
        ),
    )
    assert result.decision == Outcome.UNKNOWN
    empty = service.context(
        p,
        "scheduler",
        ContextRequest(
            snapshot_id="snapshot-1", source_commit="demo-commit-1", files=("unrelated.py",)
        ),
    )
    assert empty.status == "unknown"


def test_snapshot_is_repository_bound() -> None:
    with pytest.raises(DomainError):
        KnowledgeService(DemoStore()).snapshot(
            demo_principals()["demo-agent"], "other", "snapshot-1"
        )
