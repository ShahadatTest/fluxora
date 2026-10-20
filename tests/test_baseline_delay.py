"""Section 7 acceptance: baseline and delay forecasts.

All amounts here are integer minor units. Expected values come straight
from section 7 of the build brief (converted: 1 unit = 100 minor).
"""

from cashflow_copilot.forecast import forecast

from .conftest import (
    NOV_5, OCT_16, OCT_20, OCT_23, OCT_25, OCT_28, eur, usd,
)


def test_baseline_forecast_values(ledger):
    fc = forecast(ledger, ledger.as_of, ledger.horizon_days)
    c = usd(fc)
    assert c.balance_on(OCT_16) == 1_000_000
    assert c.balance_on(OCT_20) == 1_800_000   # +8,000 invoice
    assert c.balance_on(OCT_23) == 1_200_000   # -6,000 payroll
    assert c.balance_on(OCT_25) == 750_000     # -4,500 supplier
    assert c.balance_on(OCT_28) == 600_000     # -1,500 ops
    assert c.min_balance_minor == 600_000
    assert c.first_breach_date is None         # floor is 300,000: no breach
    assert c.worst_shortfall_minor == 0
    assert c.closing_minor == 600_000


def test_delay_forecast_values(delayed_ledger):
    fc = forecast(delayed_ledger, delayed_ledger.as_of, delayed_ledger.horizon_days)
    c = usd(fc)
    assert c.balance_on(OCT_23) == 400_000    # -6,000 payroll, no inflow yet
    assert c.balance_on(OCT_25) == -50_000    # -4,500 supplier
    assert c.balance_on(OCT_28) == -200_000   # -1,500 ops
    assert c.balance_on(NOV_5) == 600_000     # +8,000 delayed invoice
    assert c.min_balance_minor == -200_000
    assert c.first_breach_date == OCT_25      # floor 300,000 first breached
    assert c.worst_shortfall_minor == 500_000  # 300,000 - (-200,000)
    assert c.closing_minor == 600_000


def test_delay_event_moves_expected_date(delayed_ledger):
    rec = delayed_ledger.receivables["inv-client-001"]
    assert rec.expected_date == NOV_5


def test_eur_untouched_and_never_covers_usd_gap(delayed_ledger):
    fc = forecast(delayed_ledger, delayed_ledger.as_of, delayed_ledger.horizon_days)
    e = eur(fc)
    assert e.min_balance_minor == 100_000
    assert e.closing_minor == 100_000
    assert e.first_breach_date is None
    # EUR 1,000 sits idle while USD breaches: per-currency floors mean the
    # display conversion must never silently cover the USD gap.
    assert usd(fc).first_breach_date == OCT_25


def test_opening_balance_is_part_of_minimum(ledger):
    fc = forecast(ledger, ledger.as_of, ledger.horizon_days)
    c = usd(fc)
    assert c.days[0].date == OCT_16
    assert c.min_balance_minor <= c.days[0].opening_minor


def test_intraday_ordering_assumption_is_explicit(ledger):
    fc = forecast(ledger, ledger.as_of, ledger.horizon_days)
    assert any("outflows are applied before inflows" in a for a in fc.assumptions)
