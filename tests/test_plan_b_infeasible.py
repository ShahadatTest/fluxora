"""Section 7 acceptance: Plan B and the infeasible constraint set."""

import copy

from cashflow_copilot.models import OutflowAmendment, OwnerConstraints, Plan
from cashflow_copilot.plans import (
    build_advance_plan,
    build_plan_b_full,
    build_plan_b_supplier_only,
    evaluate_plan,
    generate_candidate_plans,
)

INFEASIBLE = OwnerConstraints(
    max_advance_minor=400_000,
    allow_supplier_date_change=False,
    allow_ops_deferral=False,
)


def test_plan_b_supplier_move_infeasible_without_explicit_consent(delayed_ledger):
    # Consent is never pre-granted: on a fresh seed Plan B must be infeasible
    # with a reason naming the missing explicit acceptance event.
    result = evaluate_plan(delayed_ledger, build_plan_b_supplier_only())
    assert result.feasible is False
    assert any("explicit counterparty" in r or "acceptance event" in r
               for r in result.reasons)


def test_plan_b_supplier_move_alone_is_insufficient(delayed_ledger_with_consent):
    led = delayed_ledger_with_consent
    result = evaluate_plan(led, build_plan_b_supplier_only(led.consent_id))
    assert result.feasible is False
    assert result.min_balance_minor == 250_000  # below the 300,000 floor
    assert any("breach" in r for r in result.reasons)


def test_plan_b_full_is_sufficient(delayed_ledger_with_consent):
    led = delayed_ledger_with_consent
    result = evaluate_plan(led, build_plan_b_full(led.consent_id))
    assert result.feasible is True
    assert result.min_balance_minor == 300_000
    assert result.closing_minor == 600_000


def test_plan_b_full_keeps_1000_ops_on_oct_28(delayed_ledger_with_consent):
    result = evaluate_plan(delayed_ledger_with_consent,
                           build_plan_b_full(delayed_ledger_with_consent.consent_id))
    from .conftest import OCT_28, NOV_6
    fc = result.forecast.currencies["USD"]
    oct28 = next(d for d in fc.days if d.date == OCT_28)
    nov6 = next(d for d in fc.days if d.date == NOV_6)
    # Oct 28 outflow is only the retained 1,000 of ops (no other outflow then)
    assert oct28.outflow_minor == 100_000
    # Nov 6 carries supplier 4,500 + deferred ops 500
    assert nov6.outflow_minor == 500_000


def test_owner_approval_alone_cannot_move_supplier_date(delayed_ledger):
    plan = build_plan_b_supplier_only()
    plan.amendments[0].consent_evidence_id = None  # owner approval only
    result = evaluate_plan(delayed_ledger, plan)
    assert result.feasible is False
    assert any("counterparty acceptance" in r for r in result.reasons)


def test_protected_payroll_move_invalidates_plan(delayed_ledger):
    from datetime import date
    plan = Plan(
        id="plan-bad", name="move payroll", description="illegal",
        amendments=[OutflowAmendment("payroll-001", new_date=date(2026, 10, 30))],
    )
    result = evaluate_plan(delayed_ledger, plan)
    assert result.feasible is False
    assert any("protected" in r for r in result.reasons)


def test_ops_deferral_beyond_limit_rejected(delayed_ledger):
    from datetime import date
    plan = copy.deepcopy(build_plan_b_full())
    plan.amendments[1].splits = [(date(2026, 10, 28), 50_000),
                                 (date(2026, 11, 6), 100_000)]  # 1,000 > 500 limit
    result = evaluate_plan(delayed_ledger, plan)
    assert result.feasible is False


def test_infeasible_set_advance_alone_misses_floor(delayed_ledger):
    result = evaluate_plan(delayed_ledger, build_advance_plan(400_000), INFEASIBLE)
    assert result.feasible is False
    assert result.min_balance_minor == 200_000
    shortfall = result.forecast.currencies["USD"].worst_shortfall_minor
    assert shortfall == 100_000  # misses the 300,000 floor by 1,000


def test_infeasible_set_generates_no_plan_and_invents_nothing(delayed_ledger):
    feasible, notes = generate_candidate_plans(delayed_ledger, INFEASIBLE)
    assert feasible == []
    assert any("No feasible plan" in n for n in notes)
    joined = " ".join(notes).lower()
    assert "loan" in joined  # the notes explicitly disclaim invented loans
    # The ledger itself is untouched: no phantom receipts or transfers.
    assert delayed_ledger.receipts == {}
    assert set(delayed_ledger.outflows) == {"payroll-001", "supplier-001", "ops-001"}
