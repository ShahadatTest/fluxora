"""Per-currency daily forecast engine.

Deterministic. All money is integer minor units. Intraday ordering is
conservative and explicit: on any day, outflows are applied before inflows,
so the intraday low is what the floor check uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .errors import MissingConsentError, ProtectedObligationError
from .models import Plan
from .settlement import bill_schedule, cash_events, collection_held

INTRADAY_ASSUMPTION = (
    "Intraday ordering unknown: outflows are applied before inflows on the "
    "same day (conservative). End-of-day balances are unaffected."
)


@dataclass
class DayBalance:
    date: date
    opening_minor: int
    inflow_minor: int
    outflow_minor: int
    intraday_low_minor: int
    closing_minor: int
    floor_breached: bool


@dataclass
class CurrencyForecast:
    currency: str
    days: list[DayBalance]
    floor_minor: int
    min_balance_minor: int
    min_balance_date: date
    first_breach_date: date | None
    worst_shortfall_minor: int
    closing_minor: int

    def balance_on(self, day: date) -> int:
        for d in self.days:
            if d.date == day:
                return d.closing_minor
        raise KeyError(f"{day} outside forecast window")


@dataclass
class ForecastResult:
    as_of: date
    horizon_days: int
    currencies: dict[str, CurrencyForecast]
    assumptions: list[str] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)
    plan_label: str | None = None


def _window(as_of: date, horizon_days: int) -> list[date]:
    return [as_of + timedelta(days=i) for i in range(horizon_days)]


def _apply_amendments(ledger, plan: Plan | None) -> dict[str, list[tuple[date, int]]]:
    """Return {outflow_id: [(date, minor), ...]} scheduled payments.

    Raises ProtectedObligationError / MissingConsentError for illegal or
    unauthorized changes.
    """
    schedule: dict[str, list[tuple[date, int]]] = {}
    for oid, out in ledger.outflows.items():
        # Recorded terms (possibly amended by accepted proposals) are the base;
        # a what-if plan amends on top of them without mutating the ledger.
        from .settlement import bill_source_schedule
        schedule[oid] = bill_source_schedule(out)

    if plan is None:
        return schedule

    for amd in plan.amendments:
        out = ledger.outflows.get(amd.outflow_id)
        if out is None:
            raise KeyError(f"plan references unknown outflow {amd.outflow_id!r}")
        if out.protected:
            raise ProtectedObligationError(
                f"protected obligation {out.id} cannot be rescheduled; "
                "changing a protected date invalidates the plan"
            )
        if out.paid_minor or out.face_adjustments or out.agreement_schedules:
            raise ValueError('Paid or partially paid bills require manual terms review; no plan amendment is applied.')
        if out.counterparty_consent_required_for_change:
            evid = ledger.evidence.get(amd.consent_evidence_id or "")
            if evid is None or evid.id not in ledger.runtime_evidence_ids:
                raise MissingConsentError(
                    f"{out.id} date change requires an explicit counterparty "
                    "acceptance event recorded at runtime; owner approval "
                    "alone is insufficient and consent is never pre-granted. "
                    f"(cited: {amd.consent_evidence_id!r})"
                )
        if amd.splits is not None:
            total = sum(m for _, m in amd.splits)
            if total != out.amount_minor:
                raise ValueError(
                    f"split amounts for {out.id} sum to {total}, "
                    f"expected {out.amount_minor}"
                )
            moved = sum(m for d, m in amd.splits if d > out.date)
            if moved > out.maximum_deferrable_minor:
                raise ValueError(
                    f"{out.id}: deferring {moved} exceeds the allowed "
                    f"maximum of {out.maximum_deferrable_minor}"
                )
            schedule[out.id] = list(amd.splits)
        elif amd.new_date is not None:
            schedule[out.id] = [(amd.new_date, out.amount_minor)]
    return schedule


def forecast(ledger, as_of: date, horizon_days: int, plan: Plan | None = None, *, account_id: str | None = None, collection_limits: dict | None = None) -> ForecastResult:
    """Build a per-currency daily forecast.

    - Opening snapshot (as_of) is part of the forecast minimum.
    - Observed receipts dated inside the window are actual inflows.
    - Receivables contribute their remaining balance on the expected date,
      net of any projected (plan) collections — the original invoice is never
      recounted.
    - A what-if plan never mutates the ledger; its forecast is labelled
      conditional.
    """
    if type(horizon_days) is not int or not 1 <= horizon_days <= 120:
        raise ValueError('Forecast horizon must be 1–120 whole calendar days.')
    if account_id is not None and account_id not in ledger.accounts:
        from .errors import CashflowError
        raise CashflowError('Choose a forecast account in this business.')
    if collection_limits is not None:
        if not isinstance(collection_limits, dict) or plan is not None:
            raise ValueError('Collection downside limits require a dictionary and no action plan.')
        for rid, minor in collection_limits.items():
            rec = ledger.receivables.get(rid)
            if rec is None or type(minor) is not int or not 0 <= minor <= rec.remaining_minor:
                raise ValueError('Collection downside limits must be within the current remaining invoice amount.')
    def belongs(kind, ident):
        if account_id is None: return True
        from .funding import projected_account
        return projected_account(ledger, kind, ident)['account_id'] == account_id
    days = _window(as_of, horizon_days)
    last_day = days[-1]
    assumptions = [INTRADAY_ASSUMPTION]
    clarifications: list[dict] = []

    for rec in ledger.receivables.values():
        if rec.credit_balance_minor:
            clarifications.append(dict(code='customer_credit_requires_review',receivable_id=rec.id,
                question='Review customer credit/refund obligation for '+rec.id+'.',
                why_it_matters='Paid allocation exceeds the adjusted invoice total. No refund date or new cash was invented; this projection is incomplete until the obligation is resolved.',
                evidence_id=rec.face_adjustments[-1]['evidence_id'] if rec.face_adjustments else rec.evidence_id))
    for out in ledger.outflows.values():
        if out.overpaid_minor:
            clarifications.append(dict(code='supplier_overpayment_requires_review',outflow_id=out.id,
                question='Review supplier overpayment/refund claim for '+out.id+'.',
                why_it_matters='Recorded payment is retained. A possible refund claim is not an observed or expected cash inflow.',
                evidence_id=out.face_adjustments[-1]['evidence_id'] if out.face_adjustments else out.evidence_id))

    schedule = _apply_amendments(ledger, plan)
    observed_events = [e for e in cash_events(ledger, include_internal=account_id is not None)
                       if account_id is None or e['account_id'] == account_id]

    # Index projected receipts by receivable for the residual computation.
    projected_by_receivable: dict[str, list] = {}
    if plan is not None:
        for pr in plan.projected_receipts:
            if pr.receivable_id not in ledger.receivables:
                raise KeyError(f"plan references unknown receivable {pr.receivable_id!r}")
            if type(pr.amount_minor) is not int or pr.amount_minor <= 0:
                raise ValueError('Projected receipt amounts must be positive integer minor units.')
            projected_by_receivable.setdefault(pr.receivable_id, []).append(pr)
        for label in plan.conditional_labels:
            assumptions.append(label)

    # Projected totals reduce the residual scheduled on the expected date,
    # wherever in time the projected collection is assumed.
    projected_totals = {
        rid: sum(pr.amount_minor for pr in prs)
        for rid, prs in projected_by_receivable.items()
    }
    for rid, total in projected_totals.items():
        rec = ledger.receivables[rid]
        if collection_held(rec):raise ValueError('A held/disputed invoice cannot supply projected collection: '+rid)
        if total > rec.remaining_minor:
            raise ValueError(
                f"Projected collections for {rid} ({total} minor) exceed its "
                f"remaining balance ({rec.remaining_minor} minor). Review the plan after recorded receipts."
            )

    currencies: dict[str, CurrencyForecast] = {}
    from .treasury import currency_opening, currency_floor, restricted_cash
    from .models import Account
    for ccy in dict.fromkeys(a.currency for a in ledger.accounts.values()):
        if account_id is not None and ledger.accounts[account_id].currency != ccy: continue
        held = restricted_cash(ledger, currency=ccy, account_id=account_id)
        account = Account('pooled-' + ccy, ccy, currency_opening(ledger, ccy) - held, currency_floor(ledger, ccy)) if account_id is None else Account(account_id,ccy,ledger.accounts[account_id].opening_minor-held,ledger.accounts[account_id].floor_minor)
        if held:
            assumptions.append(f'{ccy}: {held} minor source-backed restricted cash is excluded from decision balances once, with no assumed release. Book cash is unchanged; payroll is reserved through its existing schedule.')
        if account_id is None and sum(a.currency == ccy for a in ledger.accounts.values()) > 1:
            assumptions.append(f'{ccy}: available own-account cash is pooled and floor buffers are summed. This does not guarantee funding in a particular payment account; review account balances and observed transfers separately.')
        if account_id is not None:
            assumptions.append(f'{account_id}: assigned future collections/payments are planning assumptions, not approvals, consent or executed transfers. Unassigned or stale routes are excluded from this account curve and must be reviewed separately. Same-day outgoing transfers/payments precede incoming cash conservatively.')
        ccy = account.currency
        out_by_day: dict[date, int] = {}
        for oid, out in ledger.outflows.items():
            if out.currency != ccy or not belongs('bill', oid):
                continue
            if out.status != 'scheduled':
                continue
            remaining = bill_schedule(out) if out.paid_minor or out.face_adjustments or out.agreement_schedules else schedule[oid]
            for d, minor in remaining:
                if d <= last_day:
                    day = max(d, as_of)
                    out_by_day[day] = out_by_day.get(day, 0) + minor
                    if d < as_of:
                        assumptions.append(f'{oid}: unpaid overdue amount is reserved on {as_of.isoformat()} for cash-risk analysis; its contractual date {d.isoformat()} is unchanged.')

        in_by_day: dict[date, int] = {}
        for event in observed_events:
            if event['currency'] == ccy and as_of <= event['date'] <= last_day:
                amount, day = event['amount_minor'], event['date']
                target = out_by_day if event['kind'] == 'observed_bill_payment' or amount < 0 else in_by_day
                target[day] = target.get(day, 0) + (-amount if target is out_by_day else amount)

        for rid, rec in ledger.receivables.items():
            if rec.currency != ccy or rec.remaining_minor <= 0 or not belongs('invoice', rid):
                continue
            if collection_held(rec):
                clarifications.append(dict(code='collection_on_hold',receivable_id=rid,
                    question=f'{rid}: review dispute/hold source before releasing collection.',
                    why_it_matters='Remaining receivable is retained but excluded from expected/projected inflow until explicit owner release.',
                    evidence_id=rec.collection_hold['evidence_id']))
                continue
            if rec.expected_date is None:
                clarifications.append(
                    {
                        "code": "unknown_expected_date",
                        "receivable_id": rid,
                        "question": (
                            f"When is {rid} expected to be paid? No expected "
                            "date is recorded."
                        ),
                        "why_it_matters": (
                            "Without a date the receivable is excluded from the "
                            "timed forecast; the cash gap cannot be dated until "
                            "this is known. No date is assumed."
                        ),
                    }
                )
                continue
            residual = rec.remaining_minor - projected_totals.get(rid, 0)
            residual = max(residual, 0)
            if collection_limits is not None and rid in collection_limits:
                residual = min(residual, collection_limits[rid])
                assumptions.append(f'{rid}: hypothetical collection limited to {residual} {ccy} minor units; no receipt or write-off recorded.')
            if as_of <= rec.expected_date <= last_day and residual > 0:
                in_by_day[rec.expected_date] = in_by_day.get(rec.expected_date, 0) + residual

        if plan is not None:
            for pr in plan.projected_receipts:
                rec = ledger.receivables[pr.receivable_id]
                if rec.currency != ccy or not belongs('invoice', rec.id):
                    continue
                if as_of <= pr.date <= last_day:
                    in_by_day[pr.date] = in_by_day.get(pr.date, 0) + pr.amount_minor

        day_rows: list[DayBalance] = []
        balance = account.opening_minor
        floor = account.floor_minor
        for day in days:
            out_today = out_by_day.get(day, 0)
            in_today = in_by_day.get(day, 0)
            intraday_low = balance - out_today
            closing = intraday_low + in_today
            breached = intraday_low < floor
            day_rows.append(
                DayBalance(
                    date=day,
                    opening_minor=balance,
                    inflow_minor=in_today,
                    outflow_minor=out_today,
                    intraday_low_minor=intraday_low,
                    closing_minor=closing,
                    floor_breached=breached,
                )
            )
            balance = closing

        lows = [(d.intraday_low_minor, d.date) for d in day_rows]
        min_balance, min_date = min(lows, key=lambda t: (t[0], t[1]))
        breach_days = [d.date for d in day_rows if d.floor_breached]
        currencies[ccy] = CurrencyForecast(
            currency=ccy,
            days=day_rows,
            floor_minor=floor,
            min_balance_minor=min_balance,
            min_balance_date=min_date,
            first_breach_date=breach_days[0] if breach_days else None,
            worst_shortfall_minor=max(floor - min_balance, 0),
            closing_minor=day_rows[-1].closing_minor,
        )

    return ForecastResult(
        as_of=as_of,
        horizon_days=horizon_days,
        currencies=currencies,
        assumptions=assumptions,
        clarifications=clarifications,
        plan_label=plan.name if plan else None,
    )
