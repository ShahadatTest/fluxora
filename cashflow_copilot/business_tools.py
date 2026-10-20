"""Owner source checks, one-payment allocations and read-only purchase scenarios.

Imported documents never confirm a payment. Split cash is recognized once,
after owner attestation; its allocation receipts never supply additional cash.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .forecast import forecast
from .models import Allocation, Outflow, Receipt
from .owner_workflows import _begin, _finish, _evidence, _text
from .planning import _hash
from .settlement import split_unallocated, collection_held


def _fact(ledger, kind, ident):
    if kind == 'cash':
        account = ledger.accounts.get(ident)
        if account is None:
            raise CashflowError('Choose an opening cash account in this business.')
        return dict(kind=kind, id=ident, currency=account.currency, amount_minor=account.opening_minor,
                    as_of=ledger.as_of.isoformat(), evidence_id=account.evidence_id)
    if kind == 'collection':
        rec = ledger.receivables.get(ident)
        if rec is None or rec.remaining_minor <= 0:
            raise CashflowError('Choose an outstanding invoice in this business.')
        return dict(kind=kind, id=ident, currency=rec.currency, remaining_minor=rec.remaining_minor,
                    expected_date=rec.expected_date.isoformat() if rec.expected_date else None,
                    evidence_id=ledger.collection_date_source(ident),
                    collection_hold=copy.deepcopy(rec.collection_hold))
    raise CashflowError('Choose opening cash or expected collection for source review.')


def _save_fact_check(ledger, kind, ident, evidence_id, note, *, ctx):
    """Internal helper, called only after an authorized owner validation boundary."""
    require_access(ctx, ledger)
    ledger.fact_checks[kind + ':' + ident] = dict(kind=kind, id=ident,
        fingerprint=_hash(_fact(ledger, kind, ident)), reviewed_on=ledger.as_of.isoformat(),
        recorded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        evidence_id=evidence_id, note=note.strip(), actor=ctx.actor)


def confirm_fact_check(ledger, kind, ident, evidence_id, note, revision, *, ctx):
    payload = dict(kind=kind, id=ident, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'fact_source_checked', revision, payload)
    if old:
        return old
    fact = _fact(ledger, kind, ident)
    if kind == 'collection' and (not fact['expected_date'] or date.fromisoformat(fact['expected_date']) < ledger.as_of):
        raise CashflowError('Resolve the missing/past expected date first. A check cannot silently change it.')
    _evidence(ledger, evidence_id)
    _text(note, 'Source review note')
    _save_fact_check(ledger, kind, ident, evidence_id, note, ctx=ctx)
    return _finish(ledger, key, 'fact_source_checked', payload,
                   dict(kind=kind, id=ident, reviewed_on=ledger.as_of.isoformat()), ctx)


def data_freshness(ledger, reference_date=None, *, ctx):
    require_access(ctx, ledger, OP_READ)
    reference_date = ledger.as_of if reference_date is None else reference_date
    if type(reference_date) is not date or not ledger.as_of <= reference_date <= ledger.as_of + timedelta(days=366):
        raise CashflowError('Freshness reference date must be between the ledger snapshot and 366 days later.')
    rows = []
    for kind, collection in [('cash', ledger.accounts), ('collection', ledger.receivables)]:
        for ident, item in collection.items():
            if kind == 'collection' and item.remaining_minor <= 0:
                continue
            fact = _fact(ledger, kind, ident)
            check = ledger.fact_checks.get(kind + ':' + ident)
            threshold = 1 if kind == 'cash' else 7
            age = (reference_date - date.fromisoformat(check['reviewed_on'])).days if check else None
            if not check:
                status = 'unverified'
            elif check['evidence_id'] not in ledger.evidence:
                status = 'source_missing'
            elif check['fingerprint'] != _hash(fact):
                status = 'changed_since_check'
            elif age < 0:
                status = 'invalid_review_date'
            elif age > threshold:
                status = 'stale'
            else:
                status = 'fresh'
            if kind == 'collection' and (not item.expected_date or item.expected_date < reference_date):
                status = 'date_needs_confirmation'
            rows.append(dict(fact, status=status, review=copy.deepcopy(check), age_days=age,
                             threshold_days=threshold,
                             held=collection_held(item) if kind == 'collection' else False))
    return dict(reference_date=reference_date.isoformat(), ledger_as_of=ledger.as_of.isoformat(), rows=rows,
                note='Rule-based source-review age, not a confidence/probability score or bank verification. '
                     'Cash checks cover the opening snapshot, not a live bank balance. '
                     'Age is measured in calendar days against the displayed reference date, not a hidden wall clock. '
                     'Cash review expires after 1 day; collection review after 7 days. These are local review policies, not guarantees.')


def affordability(ledger, currency, amount_minor, purchase_date, label, *, ctx):
    require_access(ctx, ledger, OP_READ)
    if type(ledger.horizon_days) is not int or not 1 <= ledger.horizon_days <= 120:
        raise CashflowError('Affordability supports a 1â€“120-day forecast horizon.')
    if len(ledger.outflows) + len(ledger.receivables) > 600:
        raise CashflowError('Affordability supports at most 600 invoices/bills.')
    currencies = [a.currency for a in ledger.accounts.values()]
    if any(key != a.id for key,a in ledger.accounts.items()):
        raise CashflowError('Account register keys must match their stable account IDs.')
    if not currencies or currency not in currencies:
        raise CashflowError('Choose a currency with opening cash accounts; no FX is applied.')
    if type(amount_minor) is not int or not 0 < amount_minor <= 10**15:
        raise CashflowError('Purchase amount must be positive integer minor units, at most 10^15.')
    last = ledger.as_of + timedelta(days=ledger.horizon_days-1)
    if type(purchase_date) is not date or not ledger.as_of <= purchase_date <= last:
        raise CashflowError('Purchase date must lie inside the displayed forecast window.')
    _text(label, 'Purchase description')
    from .adaptive import input_problems
    issues = input_problems(ledger)
    if any(p['currency'] == currency and split_unallocated(ledger,p) > 0 for p in ledger.split_receipts.values()):
        issues.append('Unallocated customer receipts need source/refund-obligation review. Recorded cash is not revenue; no refund schedule was invented.')
    if any(i.currency not in currencies for group in [ledger.receivables, ledger.outflows] for i in group.values()):
        raise CashflowError('Every financial record needs a separate cash account before previewing a purchase.')
    freshness = data_freshness(ledger, ctx=ctx)
    for row in freshness['rows']:
        if row['currency'] == currency and not row['held'] and row['status'] != 'fresh':
            issues.append(row['id'] + ': source review is ' + row['status'].replace('_', ' ') + '.')
    baseline = forecast(ledger, ledger.as_of, ledger.horizon_days)
    work = copy.copy(ledger)
    work.outflows = dict(ledger.outflows)
    ident = 'hypothetical-purchase'
    while ident in work.outflows:
        ident += '-'
    work.outflows[ident] = Outflow(ident, currency, amount_minor, purchase_date, False, '')
    scenario = forecast(work, work.as_of, work.horizon_days)
    before, after = baseline.currencies[currency], scenario.currencies[currency]
    headroom = max(0, min(d.intraday_low_minor for d in before.days if d.date >= purchase_date)-before.floor_minor)
    status = 'floor_breach' if after.first_breach_date else 'source_review_required' if issues else 'within_floor_under_assumptions'
    protected = [dict(id=o.id, date=o.date.isoformat(), remaining_minor=o.remaining_minor,
        evidence_id=o.evidence_id, scenario_intraday_low_minor=next(
            (d.intraday_low_minor for d in after.days if d.date == max(o.date, ledger.as_of)), None))
        for o in ledger.outflows.values() if o.currency == currency and o.protected and o.remaining_minor > 0 and o.date <= last]
    return dict(currency=currency, amount_minor=amount_minor, purchase_date=purchase_date,
        label=label.strip(), baseline=baseline, scenario=scenario, status=status,
        baseline_already_breached=before.first_breach_date is not None,
        additional_shortfall_minor=after.worst_shortfall_minor-before.worst_shortfall_minor,
        numerical_headroom_minor=headroom, issues=list(dict.fromkeys(issues)), protected=protected,
        note='Hypothetical expense only. No bill, draft, approval or payment is created. '
             'Existing protected dates and consent requirements stay unchanged. '
             'Expected collections are assumptions, not observed cash; same-day outflows precede inflows. '
             'Headroom is numerical capacity from the purchase date within this forecast window, not a spending limit or guarantee. '
             'No FX, tax estimate, financing, return on purchase or risk beyond the window is assumed.')


def _allocation_rows(ledger, currency, assignments, available):
    if not isinstance(assignments, dict) or len(assignments) > 50:
        raise CashflowError('Allocate to at most 50 explicitly selected invoices at a time.')
    total = 0
    for invoice_id, amount in assignments.items():
        rec = ledger.receivables.get(invoice_id)
        if rec is None or rec.currency != currency:
            raise CashflowError('Every allocation needs an existing invoice in the receipt currency; no FX or guessed match.')
        if type(amount) is not int or not 0 < amount <= rec.remaining_minor:
            raise CashflowError('Each allocation must be a positive integer amount within that invoice remaining balance.')
        total += amount
    if total > available:
        raise CashflowError('Allocation total exceeds this payment unallocated balance.')
    return total


def _append_allocations(ledger, parent, assignments, allocation_date, key, actor):
    children = []
    for invoice_id, amount in sorted(assignments.items()):
        child_id = 'split-' + _hash([key, invoice_id])[:32]
        if child_id in ledger.receipts or child_id in ledger.receipt_inbox or child_id in ledger.split_receipts:
            raise IdempotencyConflict('Allocation ID conflicts with an existing source; no allocation was recorded.')
        children.append(Receipt(child_id, invoice_id, amount, allocation_date,
            evidence_id=parent['evidence_id'], actor=actor, business_id=ledger.business_id))
    # All validation precedes mutation, including every generated child ID.
    for receipt in children:
        ledger.receipts[receipt.id] = receipt
        ledger.receivables[receipt.receivable_id].allocations.append(Allocation(receipt.id, receipt.amount_minor, allocation_date, True))
        parent['allocation_ids'].append(receipt.id)
    return [r.id for r in children]


def confirm_split_observation(ledger, observation_id, assignments, note, acknowledge_duplicates, revision, *, ctx):
    payload = dict(observation_id=observation_id, assignments=assignments, note=note,
                   acknowledge_duplicates=acknowledge_duplicates)
    key, old = _begin(ledger, ctx, 'split_payment_observed', revision, payload)
    if old:
        return old
    from .owner_workflows import observation_candidates
    info = observation_candidates(ledger, observation_id, ctx=ctx)
    row = ledger.receipt_inbox[observation_id]
    if row['status'] != 'unallocated' or observation_id in ledger.receipts or observation_id in ledger.split_receipts:
        raise IdempotencyConflict('This observation was already handled. Review its existing payment; do not record cash again.')
    day = date.fromisoformat(row['date'])
    if day < ledger.as_of:
        raise CashflowError('A newly confirmed payment cannot predate the current opening snapshot. Review cash evidence instead.')
    if info['possible_duplicate_ids'] and acknowledge_duplicates is not True:
        raise CashflowError('Check and explicitly acknowledge possible duplicate payment/source IDs before recording cash.')
    _evidence(ledger, row['evidence_id'])
    _text(note, 'Observed payment and allocation source check')
    _allocation_rows(ledger, row['currency'], assignments, row['amount_minor'])
    from .payment_review import payment_candidates,require_ack,remember_ack,stamp_ack_fingerprint
    candidates=payment_candidates(ledger,row['currency'],row['amount_minor'],day)
    require_ack(candidates,acknowledge_duplicates)
    parent = dict(id=observation_id, business_id=ledger.business_id, amount_minor=row['amount_minor'],
        currency=row['currency'], date=row['date'], evidence_id=row['evidence_id'], actor=ctx.actor,
        note=note.strip(), allocation_ids=[], duplicate_ids_reviewed=info['possible_duplicate_ids'])
    ids = _append_allocations(ledger, parent, assignments, day, key, ctx.actor)
    ledger.split_receipts[observation_id] = parent
    row.update(status='split_recorded', owner_note=note.strip())
    remember_ack(ledger,'split_payment',observation_id,candidates,ctx.actor,row['evidence_id'])
    stamp_ack_fingerprint(ledger,'split_payment',observation_id)
    return _finish(ledger, key, 'split_payment_observed', payload,
        dict(payment_id=observation_id, allocation_ids=ids, unallocated_minor=split_unallocated(ledger, parent)), ctx)


def allocate_split_balance(ledger, payment_id, assignments, allocation_date, evidence_id, note, revision, *, ctx):
    payload = dict(payment_id=payment_id, assignments=assignments, date=str(allocation_date), evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'split_balance_allocated', revision, payload)
    if old:
        return old
    parent = ledger.split_receipts.get(payment_id)
    if parent is None or parent['business_id'] != ledger.business_id:
        raise CashflowError('Choose an existing split payment in this business.')
    from .extension import movement_date
    previous = [movement_date(ledger,'split_payment:'+payment_id,parent['date']), ledger.as_of]
    previous += [ledger.receipts[rid].date_received for rid in parent['allocation_ids']]
    previous += [date.fromisoformat(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
    previous += [date.fromisoformat(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==payment_id]
    from .workbench import split_dependency_dates
    previous += split_dependency_dates(ledger,parent)
    if type(allocation_date) is not date or allocation_date < max(previous):
        raise CashflowError('Allocation cannot predate the opening, payment, prior allocation or correction that frees its balance.')
    if not assignments:
        raise CashflowError('Select at least one allocation; the existing payment already contributes its cash.')
    _text(note, 'Allocation source check')
    _evidence(ledger, evidence_id)
    _allocation_rows(ledger, parent['currency'], assignments, split_unallocated(ledger, parent))
    ids = _append_allocations(ledger, parent, assignments, allocation_date, key, ctx.actor)
    for rid in ids:
        ledger.receipts[rid].evidence_id = evidence_id
    return _finish(ledger, key, 'split_balance_allocated', payload,
        dict(payment_id=payment_id, allocation_ids=ids, unallocated_minor=split_unallocated(ledger, parent)), ctx)
