"""Strict typed three-valued expressions; only definite matches authorize an outcome."""

from tacit_contracts.models import Decision, Expression, Outcome, Rule, Scalar

from tacit_domain.scheduler import ACTIONS, scenarios, valid_facts


def expression(node: Expression, facts: dict[str, Scalar]) -> bool | None:
    if node.op in {"all", "any", "not"}:
        values = [expression(child, facts) for child in node.children]
        if node.op == "not":
            return None if values[0] is None else not values[0]
        if node.op == "all":
            return False if False in values else (None if None in values else True)
        return True if True in values else (None if None in values else False)
    if node.field not in facts:
        return None
    value = facts[node.field]
    if type(value) is not type(node.value):
        return None
    if node.op == "eq":
        return value == node.value
    if node.op == "ne":
        return value != node.value
    if type(value) is not int or type(node.value) is not int:
        return None
    if node.op == "lt":
        return value < node.value
    if node.op == "le":
        return value <= node.value
    if node.op == "gt":
        return value > node.value
    return value >= node.value


def matches(rule: Rule, facts: dict[str, Scalar], action: str | None = None) -> bool:
    if rule.action is not None and rule.action != action:
        return False
    if rule.domain and not valid_facts(facts):
        return False
    if rule.condition:
        return expression(rule.condition, facts) is True
    return all(
        p.field in facts and type(facts[p.field]) is type(p.equals) and facts[p.field] == p.equals
        for p in rule.when
    )


def evaluate(
    decisions: tuple[Decision, ...], facts: dict[str, Scalar], action: str | None = None
) -> tuple[Outcome, tuple[str, ...]]:
    matched = [d for d in decisions if d.rule is not None and matches(d.rule, facts, action)]
    outcomes = {d.rule.outcome for d in matched if d.rule is not None}
    result = (
        Outcome.UNKNOWN
        if not outcomes
        else (Outcome.CONFLICT if len(outcomes) > 1 else Outcome(next(iter(outcomes))))
    )
    return result, tuple(d.revision_id for d in matched)


def distinguishing(
    rules: tuple[Rule | None, ...], limit: int = 12
) -> tuple[dict[str, object], ...]:
    """Enumerate the declared finite domain; unsupported generic rules require human clarification."""
    result: list[dict[str, object]] = []
    for action in ACTIONS:
        for facts in scenarios():
            outcomes = [r.outcome if r and matches(r, facts, action) else "unknown" for r in rules]
            if len(set(outcomes)) > 1:
                result.append({"action": action, "facts": facts, "outcomes": outcomes})
                if len(result) >= limit:
                    return tuple(result)
    return tuple(result)
