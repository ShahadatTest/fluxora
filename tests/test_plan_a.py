"""Section 7 acceptance: Plan A (early partial collection)."""

from datetime import date

import pytest

from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Receipt
from cashflow_copilot.plans import build_plan_a, evaluate_plan

from .conftest import NOV_5, OCT_22, OCT_23, usd


def test_plan_a_what_if_values(delayed_ledger):
    result = evaluate_plan(delayed_ledger, build_plan_a())
    assert result.feasible is True
    assert result.min_balance_minor == 300_000   # floor met exactly
    assert result.closing_minor == 600_000
    assert result.forecast.currencies["USD"].first_breach_date is None


def test_plan_a_keeps_payroll_on_original_date(delayed_ledger):
    result = evaluate_plan(delayed_ledger, build_plan_a())
    assert result.plan.amendments == []
    assert delayed_ledger.outflows["payroll-001"].date == date(2026, 10, 23)
    # And the evaluated forecast still pays payroll on 23 Oct:
    fc = result.forecast
    day = next(d for d in fc.currencies["USD"].days if d.date == OCT_23)
    assert day.outflow_minor == 600_000


def test_plan_a_is_labelled_conditional(delayed_ledger):
    result = evaluate_plan(delayed_ledger, build_plan_a())
    assert any("Conditional on collection" in a
               for a in result.forecast.assumptions)


def test_plan_evaluation_never_mutates_the_ledger(delayed_ledger):
    before_cash = delayed_ledger.actual_cash("USD")
    before_remaining = delayed_ledger.receivables["inv-client-001"].remaining_minor
    evaluate_plan(delayed_ledger, build_plan_a())
    assert delayed_ledger.actual_cash("USD") == before_cash == 1_000_000
    assert delayed_ledger.receivables["inv-client-001"].remaining_minor == before_remaining
    assert delayed_ledger.receipts == {}


def test_unique_receipt_reduces_remaining(delayed_ledger):
    out = delayed_ledger.record_receipt(
        Receipt(id="rcpt-001", receivable_id="inv-client-001",
                amount_minor=500_000, date_received=OCT_22,
                evidence_id="ev-client-invoice"),
        actor="owner",
    )
    assert out["changed"] is True
    assert delayed_ledger.receivables["inv-client-001"].remaining_minor == 300_000


def test_repeated_receipt_id_is_a_noop(delayed_ledger):
    rcpt = Receipt(id="rcpt-001", receivable_id="inv-client-001",
                   amount_minor=500_000, date_received=OCT_22,
                   evidence_id="ev-client-invoice")
    delayed_ledger.record_receipt(rcpt, actor="owner")
    out = delayed_ledger.record_receipt(rcpt, actor="owner")
    assert out["duplicate"] is True and out["changed"] is False
    assert delayed_ledger.receivables["inv-client-001"].remaining_minor == 300_000
    assert len(delayed_ledger.receipts) == 1


def test_conflicting_receipt_id_is_rejected(delayed_ledger):
    delayed_ledger.record_receipt(
        Receipt(id="rcpt-001", receivable_id="inv-client-001",
                amount_minor=500_000, date_received=OCT_22,
                evidence_id="ev-client-invoice"),
        actor="owner")
    from cashflow_copilot.errors import IdempotencyConflict
    with pytest.raises(IdempotencyConflict):
        delayed_ledger.record_receipt(
            Receipt(id="rcpt-001", receivable_id="inv-client-001",
                    amount_minor=400_000, date_received=OCT_22,
                    evidence_id="ev-client-invoice"),
            actor="owner")


def test_no_double_count_of_invoice_after_partial_receipt(delayed_ledger):
    """The 5,000 receipt plus the 3,000 remainder must close at 6,000 —
    never 11,000 by counting the full 8,000 invoice again."""
    delayed_ledger.record_receipt(
        Receipt(id="rcpt-001", receivable_id="inv-client-001",
                amount_minor=500_000, date_received=OCT_22,
                evidence_id="ev-client-invoice"),
        actor="owner")
    fc = forecast(delayed_ledger, delayed_ledger.as_of, delayed_ledger.horizon_days)
    c = usd(fc)
    assert c.balance_on(NOV_5) == 600_000
    assert c.closing_minor == 600_000  # not 1,100,000


def test_over_allocation_refused(delayed_ledger):
    from cashflow_copilot.errors import CashflowError
    with pytest.raises(CashflowError):
        delayed_ledger.record_receipt(
            Receipt(id="rcpt-big", receivable_id="inv-client-001",
                    amount_minor=800_001, date_received=OCT_22,
                    evidence_id="ev-client-invoice"),
            actor="owner")
