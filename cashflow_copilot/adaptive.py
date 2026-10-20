"""Bounded, deterministic planning from ledger data; no fixture identifiers.

Three greedy strategies are previews, not an exhaustive optimizer. Proposed
terms are simulated on a copy without inventing authorization evidence.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import timedelta

from .authz import OP_READ, require_access
from .errors import CashflowError
from .forecast import forecast
from .models import Plan, ProjectedReceipt
from .money import format_amount
from .settlement import bill_schedule, collection_held


AUTO_PLANS = {
    'auto-collections': 'Earlier collections',
    'auto-deferrals': 'Permitted bill deferrals',
    'auto-combined': 'Bill deferrals plus earlier collections',
}
MAX_DEFERRAL_DAYS = 14
MAX_RECORDS = 300
MAX_STEPS = 300


def advance_cap(ledger, currency):
    return ledger.constraints.advance_caps_minor.get(currency,
        ledger.constraints.max_advance_minor if currency == ledger.base_currency else None)


def validate_current_request(ledger, action):
    """Recheck target facts at owner approval/send, including edited drafts."""
    from datetime import date
    terms = action.terms
    amount = terms.get('amount_minor')
    if type(amount) is not int or amount <= 0:
        raise CashflowError('Review a positive request amount before approval/send.')
    try:
        requested = date.fromisoformat(terms.get('date', ''))
    except (ValueError, TypeError):
        raise CashflowError('Review an explicit request date before approval/send.')
    if requested < ledger.as_of:
        raise CashflowError('The request date predates this ledger snapshot. Review current terms.')
    if action.kind == 'request_early_payment':
        target = ledger.receivables.get(terms.get('receivable_id'))
        if target is None or amount > target.remaining_minor:
            raise CashflowError('The requested collection exceeds the current invoice remaining balance or the invoice is missing.')
        if collection_held(target):raise CashflowError('This invoice is held/disputed; review and explicitly release it before a collection request.')
        cap = advance_cap(ledger, target.currency)
        if cap is not None and amount > cap:
            raise CashflowError('The requested advance exceeds the current owner currency limit.')
    else:
        target = ledger.outflows.get(terms.get('outflow_id'))
        if target is None or target.status != 'scheduled' or target.protected:
            raise CashflowError('The target bill is missing, already paid or protected. Review a permitted request.')
        if target.paid_minor or target.agreement_schedules:
            raise CashflowError('A partially paid bill needs manual terms review before another deferral request.')
        if target.face_adjustments:
            raise CashflowError('This bill has source amount adjustments; review current terms manually before another deferral request.')
        if action.kind == 'request_supplier_extension':
            if not target.counterparty_consent_required_for_change or not ledger.constraints.allow_supplier_date_change:
                raise CashflowError('A supplier extension is not permitted for this bill under the current constraints.')
            if amount != target.amount_minor:
                raise CashflowError('The extension amount must match the current bill balance; it moves a date, not cash.')
        elif action.kind == 'defer_ops_expense':
            if target.counterparty_consent_required_for_change or not ledger.constraints.allow_ops_deferral:
                raise CashflowError('An owner-only deferral cannot replace required counterparty consent.')
            if amount > target.maximum_deferrable_minor or amount > target.amount_minor:
                raise CashflowError('The requested expense deferral exceeds the designated limit.')
        if requested <= target.date:
            raise CashflowError('A deferral request must be later than the current bill date.')
    if terms.get('currency', target.currency) != target.currency:
        raise CashflowError('The request currency differs from its source record. Review the draft.')
    if target.evidence_id not in ledger.evidence:
        raise CashflowError('The request source evidence is missing. Review the ledger.')
    from .operations import duplicate_pairs
    from .authz import owner_context_for
    if any(p['review'] and p['review']['verdict']=='confirmed_duplicate' and
           target.id in {p['left']['id'],p['right']['id']} and p['kind']==('invoice' if action.kind=='request_early_payment' else 'bill')
           for p in duplicate_pairs(ledger,ctx=owner_context_for(ledger.business_id))):
        raise CashflowError('This target has a confirmed duplicate. Correct/review its source before approving/sending a request.')


@dataclass
class Proposal:
    id: str
    name: str
    drafts: list[dict]
    forecast: object
    financially_sufficient: bool
    blockers: list[str]
    notes: list[str]
    evidence_ids: list[str]
    pending_permissions: list[str]


def input_problems(ledger) -> list[str]:
    """Missing data must not turn an incomplete projection into an all-clear."""
    problems = []
    from .payment_review import payment_warnings
    from .authz import owner_context_for
    for pair in payment_warnings(ledger,ctx=owner_context_for(ledger.business_id)):
        if pair['status'] in {'review_required','possible_duplicate'}:
            problems.append(pair['left']['id']+': possible duplicate observed payment with '+pair['right']['id']+'; review sources before relying on a sufficient plan.')
    for rec in ledger.receivables.values():
        if rec.credit_balance_minor:problems.append(rec.id+': customer credit/possible refund obligation needs review; not additional cash or revenue.')
    for out in ledger.outflows.values():
        if out.overpaid_minor:problems.append(out.id+': supplier overpayment/possible refund claim needs source review; no expected refund cash was invented.')
    from .operations import duplicate_pairs
    from .authz import owner_context_for
    for pair in duplicate_pairs(ledger,ctx=owner_context_for(ledger.business_id)):
        if pair['review'] and pair['review']['verdict']=='confirmed_duplicate':
            problems.append(f"{pair['left']['id']}: confirmed duplicate with {pair['right']['id']}; both remain included. Correct the source or review the verdict before relying on a sufficient plan.")
    currencies = [a.currency for a in ledger.accounts.values()]
    if not currencies:
        problems.append('No cash account/opening snapshot is recorded.')
    from .treasury import account_cash, restricted_cash
    for account in ledger.accounts.values():
        if account_cash(ledger, account.id) - restricted_cash(ledger, account_id=account.id) < 0:
            problems.append(account.id+': available account cash is negative; review source attribution and restrictions before relying on a sufficient plan.')
    for collection in (ledger.receivables, ledger.outflows):
        for item in collection.values():
            if item.currency not in currencies:
                problems.append(f'{item.id}: no {item.currency} cash account/opening snapshot.')
            if item.evidence_id not in ledger.evidence:
                problems.append(f'{item.id}: source evidence is missing.')
    for rec in ledger.receivables.values():
        if collection_held(rec):continue
        if rec.remaining_minor > 0 and rec.expected_date is None:
            problems.append(f'{rec.id}: expected collection date is unknown; no date was invented.')
        elif rec.remaining_minor > 0 and rec.expected_date < ledger.as_of:
            problems.append(f'{rec.id}: expected collection date has passed; confirm a current date. It is excluded from the timed forecast.')
    for out in ledger.outflows.values():
        schedule = bill_schedule(out)
        if out.status == 'scheduled' and any(d < ledger.as_of and m > 0 for d, m in schedule):
            problems.append(f'{out.id}: an unpaid scheduled amount predates the snapshot; confirm its status and cash treatment.')
    return problems


def overlapping_actions(ledger, specs) -> list[str]:
    targets = {(s['kind'], s.get('receivable_id') or s.get('outflow_id')) for s in specs}
    return sorted(a.id for a in ledger.actions.values()
        if a.state not in {'reconciled', 'counterparty_rejected', 'cancelled'}
        and (a.kind, a.terms.get('receivable_id') or a.terms.get('outflow_id')) in targets)


def _simulate(ledger, specs):
    work = copy.copy(ledger)
    work.outflows = dict(ledger.outflows)
    receipts = []
    for spec in specs:
        day = spec['date']
        if spec['kind'] == 'request_early_payment':
            receipts.append(ProjectedReceipt(spec['receivable_id'], spec['amount_minor'], day,
                'Requested early collection; customer agreement and observed payment still required.'))
        else:
            out = ledger.outflows[spec['outflow_id']]
            if spec['kind'] == 'request_supplier_extension':
                work.outflows[out.id] = replace(out, date=day)
            else:
                kept = out.amount_minor - spec['amount_minor']
                splits = ([(out.date, kept)] if kept else []) + [(day, spec['amount_minor'])]
                work.outflows[out.id] = replace(out, recorded_splits=splits)
    return forecast(work, ledger.as_of, ledger.horizon_days,
        plan=Plan('preview', 'Conditional proposed terms', '', projected_receipts=receipts,
            conditional_labels=['Hypothetical arithmetic only: no approval, consent or payment is recorded.']))


def _collection(ledger, specs, ccy, breach, need):
    requested_day = breach - timedelta(days=1)
    if requested_day < ledger.as_of:
        return False  # a same-day inflow cannot repair an earlier intraday low
    used = sum(s['amount_minor'] for s in specs
        if s['kind'] == 'request_early_payment' and s['currency'] == ccy)
    cap = advance_cap(ledger, ccy)
    capacity = max(cap - used, 0) if cap is not None else None
    for rec in sorted(ledger.receivables.values(), key=lambda r: (r.expected_date is None, r.expected_date, r.id)):
        if collection_held(rec) or rec.currency != ccy or rec.expected_date is None or rec.expected_date <= requested_day:
            continue
        previous = next((s for s in specs if s.get('receivable_id') == rec.id), None)
        remaining = rec.remaining_minor - (previous['amount_minor'] if previous else 0)
        amount = min(need, remaining, capacity if capacity is not None else remaining)
        if amount <= 0:
            continue
        if previous:
            previous['amount_minor'] += amount
            previous['date'] = min(previous['date'], requested_day)
        else:
            specs.append(dict(kind='request_early_payment', receivable_id=rec.id,
                amount_minor=amount, currency=ccy, date=requested_day))
        return True
    return False


def _deferral(ledger, specs, ccy, breach, need):
    end = ledger.as_of + timedelta(days=ledger.horizon_days - 1)
    # Only a dated recovery inside the window supplies a candidate new date.
    # A day AFTER expected collection preserves conservative intraday ordering.
    recovery = sorted({r.expected_date + timedelta(days=1) for r in ledger.receivables.values()
        if not collection_held(r) and r.currency == ccy and r.remaining_minor > 0 and r.expected_date is not None
        and breach <= r.expected_date < end})
    moved = {s.get('outflow_id') for s in specs}
    for out in sorted(ledger.outflows.values(), key=lambda o: (o.date, o.id)):
        if (out.currency != ccy or out.protected or out.status != 'scheduled'
                or out.id in moved or out.recorded_splits or out.amendments or out.paid_minor or out.face_adjustments or out.agreement_schedules
                or not ledger.as_of <= out.date <= breach):
            continue
        dates = [d for d in recovery if out.date < d <= out.date + timedelta(days=MAX_DEFERRAL_DAYS)]
        if not dates:
            continue
        if out.counterparty_consent_required_for_change:
            if not ledger.constraints.allow_supplier_date_change:
                continue
            kind, amount = 'request_supplier_extension', out.amount_minor
        else:
            if not ledger.constraints.allow_ops_deferral or out.maximum_deferrable_minor <= 0:
                continue
            kind, amount = 'defer_ops_expense', min(need, out.maximum_deferrable_minor)
        specs.append(dict(kind=kind, outflow_id=out.id, currency=ccy,
            amount_minor=amount, date=dates[0]))
        return True
    return False


def generate_proposals(ledger, *, ctx) -> list[Proposal]:
    require_access(ctx, ledger, OP_READ)
    if not 1 <= ledger.horizon_days <= 120 or len(ledger.receivables) + len(ledger.outflows) > MAX_RECORDS:
        raise CashflowError('Planner supports at most 300 invoice/bill records and a 1–120 day window. Narrow the synthetic dataset.')
    problems = input_problems(ledger)
    sources = sorted({item.evidence_id for group in
        (ledger.accounts, ledger.receivables, ledger.outflows, ledger.receipts)
        for item in group.values() if item.evidence_id in ledger.evidence})
    sources = sorted(set(sources) | set(ledger.collection_date_sources.values()))
    proposals = []
    for pid, name in AUTO_PLANS.items():
        specs, notes, skipped = [], [], set()
        notes.extend(f'{r.id}: held/disputed remaining collection excluded; source {r.collection_hold["evidence_id"]}.' for r in ledger.receivables.values() if collection_held(r))
        for step in range(MAX_STEPS):
            fc = _simulate(ledger, specs)
            breaches = [(c.first_breach_date, code, c) for code, c in fc.currencies.items()
                if c.first_breach_date is not None and code not in skipped]
            if not breaches or problems:
                break
            day, ccy, curve = min(breaches, key=lambda t: (t[0], t[1]))
            need = curve.floor_minor - next(d.intraday_low_minor for d in curve.days if d.date == day)
            changed = False
            operations = ([_collection] if pid == 'auto-collections' else
                [_deferral] if pid == 'auto-deferrals' else [_deferral, _collection])
            for operation in operations:
                if operation(ledger, specs, ccy, day, need):
                    changed = True
                    break
            if not changed:
                skipped.add(ccy)
                notes.append(f'{ccy}: no further eligible change can cover the breach on {day.isoformat()} within this strategy.')
        else:
            notes.append('Bounded planning search stopped at its work limit; this is not an exhaustive optimum.')
        fc = _simulate(ledger, specs)
        sufficient = bool(fc.currencies) and not problems and all(c.first_breach_date is None for c in fc.currencies.values())
        permissions = []
        for spec in specs:
            target = spec.get('receivable_id') or spec.get('outflow_id')
            permissions.append(f'{target}: owner review and explicit approval required.')
            if spec['kind'] == 'request_early_payment':
                permissions.append(f'{target}: customer agreement and observed payment required; projected cash is conditional.')
            elif spec['kind'] == 'request_supplier_extension':
                permissions.append(f'{target}: counterparty acceptance of this exact proposed date is required; existing evidence is not new consent.')
            spec['date'] = spec['date'].isoformat()
            spec['amount'] = format_amount(spec['amount_minor'], spec['currency'])
        if not specs:
            notes.append('Recorded forecast needs no new action.' if sufficient else 'No eligible draft terms were found.')
        proposals.append(Proposal(pid, name, specs, fc, sufficient, list(problems), notes, sources, permissions))
    return proposals
