"""Plan ranking: hard constraints first, then the visible objective —
fewer altered obligations, lower direct cost, smaller date changes."""

from cashflow_copilot.plans import (
    build_plan_a,
    build_plan_b_full,
    evaluate_plan,
    generate_candidate_plans,
    rank_plans,
)


def test_plan_a_ranks_before_plan_b(delayed_ledger_with_consent):
    led = delayed_ledger_with_consent
    a = evaluate_plan(led, build_plan_a())
    b = evaluate_plan(led, build_plan_b_full(led.consent_id))
    assert a.feasible and b.feasible
    ranked = rank_plans([b, a])
    assert [r.plan.id for r in ranked] == ["plan-a", "plan-b"]
    # Why: Plan A alters no obligations; Plan B alters two.
    assert a.altered_obligations == 0
    assert b.altered_obligations == 2


def test_generation_returns_ranked_feasible_plans(delayed_ledger_with_consent):
    feasible, notes = generate_candidate_plans(delayed_ledger_with_consent)
    ids = [r.plan.id for r in feasible]
    assert ids == ["plan-a", "plan-b"]
    assert all(r.feasible for r in feasible)


def test_generation_without_consent_excludes_plan_b(delayed_ledger):
    # Consent is never pre-granted: without an explicit acceptance event,
    # Plan B variants are infeasible and reported as such.
    feasible, notes = generate_candidate_plans(delayed_ledger)
    ids = [r.plan.id for r in feasible]
    assert ids == ["plan-a"]
    assert any("Plan B" in n and "infeasible" in n for n in notes)


def test_rank_key_ordering_is_visible_and_deterministic(delayed_ledger_with_consent):
    led = delayed_ledger_with_consent
    a = evaluate_plan(led, build_plan_a())
    b = evaluate_plan(led, build_plan_b_full(led.consent_id))
    assert a.rank_key() < b.rank_key()
    assert a.rank_key() == (0, 0, 0)
