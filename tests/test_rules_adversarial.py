"""Independent adversarial truth tables and contract boundaries for confirmed rules."""

from itertools import permutations, product

import pytest
from pydantic import ValidationError
from tacit_api.demo import DemoStore
from tacit_contracts.models import Expression, Outcome, Predicate, Rule
from tacit_domain.rules import distinguishing, evaluate, expression, matches


def leaf(field):
    return Expression(op="eq", field=field, value=True)


def negate(value):
    return None if value is None else not value


def conjunction(left, right):
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def disjunction(left, right):
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


@pytest.mark.parametrize("a,b,c", tuple(product((True, False, None), repeat=3)))
def test_nested_three_valued_policy_matches_independent_oracle(a, b, c):
    # NOT(A AND B) OR (C AND NOT A): a missing A must not be
    # accidentally converted to a definitive permission by Python truthiness.
    rule = Rule(
        outcome="permitted",
        condition=Expression(
            op="any",
            children=(
                Expression(
                    op="not", children=(Expression(op="all", children=(leaf("a"), leaf("b"))),)
                ),
                Expression(
                    op="all", children=(leaf("c"), Expression(op="not", children=(leaf("a"),)))
                ),
            ),
        ),
    )
    facts = {
        name: value
        for name, value in zip(("a", "b", "c"), (a, b, c), strict=True)
        if value is not None
    }
    expected = disjunction(negate(conjunction(a, b)), conjunction(c, negate(a)))
    assert expression(rule.condition, facts) is expected
    assert matches(rule, facts) is (expected is True)


@pytest.mark.parametrize("op", ["eq", "ne", "lt", "le", "gt", "ge"])
def test_comparisons_are_type_strict_and_do_not_overflow(op):
    huge = 10**100
    node = Expression(op=op, field="n", value=huge)
    operators = {
        "eq": lambda n: n == huge,
        "ne": lambda n: n != huge,
        "lt": lambda n: n < huge,
        "le": lambda n: n <= huge,
        "gt": lambda n: n > huge,
        "ge": lambda n: n >= huge,
    }
    for value in (-huge, huge - 1, huge, huge + 1):
        assert expression(node, {"n": value}) is operators[op](value)
    for wrong in (True, False, str(huge)):
        assert expression(node, {"n": wrong}) is None
    assert expression(node, {}) is None


def test_conflict_is_order_independent_and_references_only_definite_matches():
    template = DemoStore().snapshot("scheduler", "snapshot-1").decisions[0]
    decisions = tuple(
        template.model_copy(
            update={
                "revision_id": name,
                "rule": Rule(outcome=outcome, condition=leaf(field)),
            }
        )
        for name, outcome, field in (
            ("permit", "permitted", "active"),
            ("deny", "forbidden", "expired"),
            ("unknown", "permitted", "missing"),
            ("false", "forbidden", "cancelled"),
        )
    )
    for ordering in permutations(decisions):
        actual, references = evaluate(
            ordering, {"active": True, "expired": True, "cancelled": False}
        )
        assert actual is Outcome.CONFLICT
        assert set(references) == {"permit", "deny"}
        assert references == tuple(
            d.revision_id for d in ordering if d.revision_id in {"permit", "deny"}
        )
    assert evaluate(decisions, {}) == (Outcome.UNKNOWN, ())
    assert evaluate((), {"expired": False}) == (Outcome.UNKNOWN, ())


@pytest.mark.parametrize(
    "invalid",
    [
        {"op": "all", "children": []},
        {"op": "any", "field": "x", "children": [{"op": "eq", "field": "x", "value": True}]},
        {"op": "not", "children": [{"op": "eq", "field": "x", "value": True}] * 2},
        {"op": "eq", "field": "x"},
        {"op": "eq", "value": True},
        {"op": "eq", "field": "x", "value": 1.0},
        {"op": "lt", "field": "x", "value": True},
        {"op": "lt", "field": "x", "value": "1"},
        {"op": "exec", "field": "x", "value": 1},
    ],
)
def test_malformed_expressions_are_rejected_before_evaluation(invalid):
    with pytest.raises(ValidationError):
        Expression.model_validate(invalid)


def test_expression_resource_bounds_and_rule_ambiguity_are_rejected():
    node = leaf("x")
    for _ in range(7):
        node = {"op": "not", "children": [node]}
    with pytest.raises(ValidationError, match="depth"):
        Expression.model_validate(node)
    wide = {"op": "all", "children": [{"op": "all", "children": [leaf("x")] * 16}] * 4}
    with pytest.raises(ValidationError, match="64"):
        Expression.model_validate(wide)
    for body in (
        {"outcome": "permitted"},
        {"outcome": "permitted", "when": [{"field": "x", "equals": True}], "condition": leaf("x")},
        {"outcome": "permitted", "domain": "scheduler-v2", "condition": leaf("lease_expired")},
        {"outcome": "permitted", "domain": "scheduler-v1", "condition": leaf("unknown")},
        {
            "outcome": "permitted",
            "domain": "scheduler-v1",
            "when": [{"field": "attempt", "equals": True}],
        },
    ):
        with pytest.raises(ValidationError):
            Rule.model_validate(body)


def test_contradictory_predicates_cannot_authorize_and_action_scope_is_preserved():
    rule = Rule(
        outcome="permitted",
        action="compute",
        domain="scheduler-v1",
        when=(
            Predicate(field="lease_expired", equals=True),
            Predicate(field="lease_expired", equals=False),
        ),
    )
    for fact in (True, False):
        assert not matches(rule, {"lease_expired": fact}, "compute")
    active = rule.model_copy(update={"when": (Predicate(field="lease_expired", equals=False),)})
    assert matches(active, {"lease_expired": False}, "compute")
    for action in (None, "retry", "COMPUTE", "delete"):
        assert not matches(active, {"lease_expired": False}, action)
    for invalid in ({"lease_expired": 0}, {"lease_expired": False, "extra": True}):
        assert not matches(active, invalid, "compute")


def test_distinguishing_scenarios_are_replayable_and_obey_positive_limit():
    positive = Rule(
        domain="scheduler-v1", action="retry", outcome="permitted", condition=leaf("lease_expired")
    )
    negative = positive.model_copy(update={"outcome": "forbidden"})
    for limit in (1, 2, 12):
        examples = distinguishing((positive, negative), limit=limit)
        assert len(examples) == limit
        for example in examples:
            assert example["action"] == "retry"
            assert example["outcomes"] == ["permitted", "forbidden"]
            assert matches(positive, example["facts"], example["action"])
            assert matches(negative, example["facts"], example["action"])
    assert distinguishing((positive, positive)) == ()
