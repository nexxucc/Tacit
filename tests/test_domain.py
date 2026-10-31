from dataclasses import replace
from itertools import product

import pytest
from tacit_api.demo import DemoStore, demo_principals
from tacit_contracts.models import (
    ContextRequest,
    Expression,
    Outcome,
    Predicate,
    Role,
    Rule,
    ScenarioRequest,
)
from tacit_domain.access import DomainError, Grant, authorize
from tacit_domain.knowledge import KnowledgeService
from tacit_domain.rules import distinguishing, evaluate, expression
from tacit_domain.scheduler import ACTIONS, scenarios, valid_facts


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


def scheduler_policy():
    """A hand-authored fixture policy, independent of the enumeration/oracle below."""

    def eq(field, value):
        return Expression(op="eq", field=field, value=value)

    active = Expression(
        op="all",
        children=(eq("lease_expired", False), eq("cancelled", False), eq("owns_attempt", True)),
    )
    prohibited = Expression(
        op="any",
        children=(eq("lease_expired", True), eq("cancelled", True), eq("owns_attempt", False)),
    )
    specs = (
        ("compute", "permitted", Expression(op="all", children=(active, eq("completed", False)))),
        ("compute", "forbidden", prohibited),
        ("upload", "permitted", Expression(op="all", children=(active, eq("completed", True)))),
        ("upload", "forbidden", prohibited),
        (
            "publish_result",
            "permitted",
            Expression(op="all", children=(active, eq("completed", True))),
        ),
        (
            "publish_result",
            "forbidden",
            Expression(op="any", children=(prohibited, eq("completed", False))),
        ),
        (
            "retry",
            "permitted",
            Expression(
                op="all",
                children=(
                    eq("lease_expired", True),
                    eq("cancelled", False),
                    eq("completed", False),
                    eq("attempt", 0),
                ),
            ),
        ),
        (
            "retry",
            "forbidden",
            Expression(op="any", children=(eq("cancelled", True), eq("attempt", 1))),
        ),
    )
    original = DemoStore().snapshot("scheduler", "snapshot-1").decisions[0]
    return tuple(
        original.model_copy(
            update={
                "revision_id": f"policy-{index}",
                "rule": Rule(
                    domain="scheduler-v1", action=action, condition=condition, outcome=outcome
                ),
            }
        )
        for index, (action, outcome, condition) in enumerate(specs)
    )


def test_scheduler_evaluator_agrees_with_independent_exhaustive_policy_oracle():
    decisions = scheduler_policy()
    # Deliberately do not use scenarios(), matches(), or expression() to derive
    # either the inputs or expected outcomes. This catches enumeration omissions
    # and evaluator errors independently of the implementation being checked.
    enumerated = []
    for worker, expired, cancelled, completed, owns, attempt in product(
        ("worker-1", "worker-2"),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        (0, 1),
    ):
        facts = dict(
            worker=worker,
            lease_expired=expired,
            cancelled=cancelled,
            completed=completed,
            owns_attempt=owns,
            attempt=attempt,
        )
        enumerated.append(facts)
        for action in ("compute", "upload", "publish_result", "retry"):
            if action == "retry":
                permitted = expired and not cancelled and not completed and attempt == 0
                forbidden = cancelled or attempt == 1
            else:
                permitted = (
                    not expired
                    and not cancelled
                    and owns
                    and (not completed if action == "compute" else completed)
                )
                forbidden = (
                    expired
                    or cancelled
                    or not owns
                    or (action == "publish_result" and not completed)
                )
            expected = (
                Outcome.CONFLICT
                if permitted and forbidden
                else Outcome.PERMITTED
                if permitted
                else Outcome.FORBIDDEN
                if forbidden
                else Outcome.UNKNOWN
            )
            actual, references = evaluate(decisions, facts, action)
            assert actual == expected, (action, facts)
            assert len(references) == int(permitted) + int(forbidden)
    assert len(enumerated) == 64
    assert scenarios() == tuple(enumerated)
    assert ACTIONS == ("compute", "upload", "publish_result", "retry")


@pytest.mark.parametrize("left,right", tuple(product((True, False, None), repeat=2)))
def test_three_valued_boolean_logic_does_not_turn_missing_facts_into_permission(left, right):
    facts = {key: value for key, value in (("left", left), ("right", right)) if value is not None}
    children = tuple(Expression(op="eq", field=key, value=True) for key in ("left", "right"))
    expected_all = (
        False
        if left is False or right is False
        else (True if left is True and right is True else None)
    )
    expected_any = (
        True
        if left is True or right is True
        else (False if left is False and right is False else None)
    )
    assert expression(Expression(op="all", children=children), facts) is expected_all
    assert expression(Expression(op="any", children=children), facts) is expected_any
    assert expression(Expression(op="not", children=(children[0],)), facts) is (
        None if left is None else not left
    )


@pytest.mark.parametrize(
    "op,expected",
    [
        ("eq", [False, True, False]),
        ("ne", [True, False, True]),
        ("lt", [True, False, False]),
        ("le", [True, True, False]),
        ("gt", [False, False, True]),
        ("ge", [False, True, True]),
    ],
)
def test_integer_boundary_comparisons_keep_booleans_and_missing_values_unknown(op, expected):
    node = Expression(op=op, field="attempt", value=1)
    assert [expression(node, {"attempt": n}) for n in (0, 1, 2)] == expected
    for value in (True, False, "1"):
        assert expression(node, {"attempt": value}) is None
    assert expression(node, {}) is None


def test_scheduler_unsupported_inputs_and_contradictions_are_not_permissions():
    decisions = scheduler_policy()
    facts = scenarios()[0]
    for action in (None, "delete", "PUBLISH_RESULT"):
        assert evaluate(decisions, facts, action) == (Outcome.UNKNOWN, ())
    for key, value in (
        ("lease_expired", 1),
        ("attempt", True),
        ("attempt", 2),
        ("worker", "worker-3"),
        ("unmodeled", False),
    ):
        invalid = {**facts, key: value}
        assert not valid_facts(invalid)
        assert evaluate(decisions, invalid, "publish_result") == (Outcome.UNKNOWN, ())
    incomplete = {key: value for key, value in facts.items() if key != "lease_expired"}
    incomplete["owns_attempt"] = True
    assert evaluate(decisions, incomplete, "compute") == (Outcome.UNKNOWN, ())
    prohibited = Rule(
        domain="scheduler-v1",
        action="publish_result",
        outcome="forbidden",
        when=(Predicate(field="lease_expired", equals=True),),
    )
    permitted = prohibited.model_copy(update={"outcome": "permitted"})
    original = decisions[0]
    contradictory = tuple(
        original.model_copy(update={"revision_id": str(i), "rule": rule})
        for i, rule in enumerate((prohibited, permitted))
    )
    assert evaluate(contradictory, {"lease_expired": True}, "publish_result") == (
        Outcome.CONFLICT,
        ("0", "1"),
    )
    assert evaluate(contradictory, {"lease_expired": False}, "publish_result") == (
        Outcome.UNKNOWN,
        (),
    )
    differences = distinguishing((prohibited, permitted))
    assert differences and len(differences) <= 12
    assert all(
        d["action"] == "publish_result"
        and d["facts"]["lease_expired"] is True
        and d["outcomes"] == ["forbidden", "permitted"]
        for d in differences
    )
