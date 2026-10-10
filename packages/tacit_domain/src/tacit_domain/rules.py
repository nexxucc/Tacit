from tacit_contracts.models import Decision, Outcome, Scalar


def evaluate(
    decisions: tuple[Decision, ...], facts: dict[str, Scalar]
) -> tuple[Outcome, tuple[str, ...]]:
    matched: list[Decision] = []
    for decision in decisions:
        rule = decision.rule
        if rule is not None and all(
            p.field in facts
            and type(facts[p.field]) is type(p.equals)
            and facts[p.field] == p.equals
            for p in rule.when
        ):
            matched.append(decision)
    outcomes = {d.rule.outcome for d in matched if d.rule is not None}
    result = (
        Outcome.UNKNOWN
        if not outcomes
        else (Outcome.CONFLICT if len(outcomes) > 1 else Outcome(next(iter(outcomes))))
    )
    return result, tuple(d.revision_id for d in matched)
