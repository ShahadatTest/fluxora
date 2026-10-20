"""Owner-attested correction entries, partial bill observations and variance.

All entries record synthetic observed facts; nothing initiates a transfer.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .forecast import forecast
from .models import Allocation, Evidence
from .owner_workflows import _begin, _finish, _evidence, _text
from .settlement import effective_receipt, cash_events, bill_schedule, bill_payment_entries, effective_bill_payment, split_parent, split_unallocated


def correct_receipt(ledger, receipt_id, invoice_id, amount_minor, day, evidence_id, note, revision, *, ctx):
    payload = dict(receipt_id=receipt_id, invoice_id=invoice_id, amount_minor=amount_minor,
        date=str(day), evidence_id=evidence_id, note=note)
    key, duplicate = _begin(ledger, ctx, 'receipt_corrected', revision, payload)
    if duplicate:
        return duplicate
    if receipt_id not in ledger.receipts or ledger.receipts[receipt_id].state != 'observed':
        raise CashflowError('Choose an existing observed receipt. Original receipt IDs and contents are retained.')
    old = effective_receipt(ledger, receipt_id)
    parent = split_parent(ledger, receipt_id)
    previous = [r for r in ledger.receipt_adjustments.values() if r['receipt_id'] == receipt_id]
    earliest = max(ledger.as_of, old.date_received, *(date.fromisoformat(r['date']) for r in previous))
    if type(day) is not date or day < earliest:
        raise CashflowError('Correction date cannot predate the opening snapshot, original receipt or a prior correction.')
    if type(amount_minor) is not int or amount_minor < 0:
        raise CashflowError('Corrected amount must be nonnegative integer minor units; zero reverses the recorded amount.')
    old_invoice, new_invoice = ledger.receivables[old.receivable_id], ledger.receivables.get(invoice_id)
    if new_invoice is None or new_invoice.currency != old_invoice.currency:
        raise CashflowError('Choose an invoice in the same currency; a correction cannot execute FX.')
    if type(amount_minor) is int and old_invoice.allocated_minor-old.amount_minor+(amount_minor if invoice_id==old_invoice.id else 0)<0:
        raise CashflowError('This receipt supports an observed refund. Correct the refund first; net collected allocation cannot become negative.')
    capacity = max(0, new_invoice.effective_total_minor - new_invoice.allocated_minor + (old.amount_minor if new_invoice.id == old_invoice.id else 0))
    from .extension import movement_date
    refunds=old_invoice.refunds+(new_invoice.refunds if new_invoice.id!=old_invoice.id else [])
    dependency_dates=[movement_date(ledger,'refund:'+r['id'],r['date']) for r in refunds]
    dependency_dates += [date.fromisoformat(c['date']) for r in refunds for c in r['adjustments']]
    if dependency_dates and day<max(dependency_dates):
        raise CashflowError('Allocation correction cannot predate a refund supported by this invoice.')
    if amount_minor > capacity:
        raise CashflowError('Corrected allocation exceeds the invoice available remaining balance.')
    if parent:
        if amount_minor > old.amount_minor + split_unallocated(ledger, parent):
            raise CashflowError('Corrected allocation exceeds the original split payment total. No new cash may be invented.')
        group_dates = [ledger.receipts[rid].date_received for rid in parent['allocation_ids']]
        group_dates += [date.fromisoformat(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
        group_dates += [date.fromisoformat(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==parent['id']]
        from .workbench import split_dependency_dates
        group_dates += split_dependency_dates(ledger,parent)
        if day < max(group_dates):
            raise CashflowError('Split allocation correction cannot predate another allocation/correction that determines its available balance.')
    if amount_minor == old.amount_minor and invoice_id == old_invoice.id:
        raise CashflowError('No amount/allocation change was proposed. The original receipt remains unchanged.')
    _evidence(ledger, evidence_id); _text(note, 'Correction reason')
    adjustment_id = 'correction-' + key[:24]
    change = dict(id=adjustment_id, receipt_id=receipt_id, old_invoice_id=old_invoice.id, new_invoice_id=invoice_id,
        old_amount_minor=old.amount_minor, new_amount_minor=amount_minor, currency=old_invoice.currency,
        date=day.isoformat(), cash_delta_minor=0 if parent else amount_minor-old.amount_minor, evidence_id=evidence_id,
        split_payment_id=parent['id'] if parent else None,
        note=note.strip(), actor=ctx.actor)
    # Compensate the effective allocation; preserve the original receipt and every allocation entry.
    old_invoice.allocations.append(Allocation(adjustment_id + '-remove', -old.amount_minor, day, True))
    if amount_minor:
        new_invoice.allocations.append(Allocation(adjustment_id + '-apply', amount_minor, day, True))
    ledger.receipt_adjustments[adjustment_id] = change
    if receipt_id in ledger.receipt_reviews:
        ledger.receipt_review_archive[adjustment_id] = dict(receipt_id=receipt_id,
            previous_review=copy.deepcopy(ledger.receipt_reviews.pop(receipt_id)), correction_id=adjustment_id)
    # Historical action approval/acceptance/reconciliation remains history. Never fabricate a new state.
    payload['adjustment'] = change
    return _finish(ledger, key, 'receipt_corrected', payload,
        dict(receipt_id=receipt_id, correction_id=adjustment_id, cash_delta_minor=change['cash_delta_minor']), ctx)


def record_bill_payment(ledger, bill_id, payment_id, amount_minor, day, evidence_id, note, revision, *, ctx, acknowledge_duplicates=False):
    require_access(ctx, ledger)
    import re
    if not isinstance(payment_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', payment_id):
        raise CashflowError('Payment observation ID needs 1â€“100 letters/digits/dashes/underscores.')
    for out in ledger.outflows.values():
        for entry in bill_payment_entries(out):
            if entry['id'] == payment_id:
                if (out.id, entry['amount_minor'], entry['date'], entry['evidence_id']) != (bill_id, amount_minor, str(day), evidence_id):
                    raise IdempotencyConflict('This payment observation ID already has different bill/amount/date/source data.')
                return dict(outflow_id=out.id, payment_id=payment_id, duplicate=True, changed=False)
    payload = dict(outflow_id=bill_id, payment_id=payment_id, amount_minor=amount_minor,
        date=str(day), evidence_id=evidence_id, note=note)
    key, duplicate = _begin(ledger, ctx, 'bill_payment_observed', revision, payload)
    if duplicate:
        return duplicate
    from .extension import observation_ids
    if payment_id in observation_ids(ledger):
        raise IdempotencyConflict('This observation ID belongs to another receipt/cash path; do not record the movement twice.')
    out = ledger.outflows.get(bill_id)
    if out is None or out.status != 'scheduled':
        raise CashflowError('Choose a scheduled bill with a remaining payable balance.')
    if type(amount_minor) is not int or not 0 < amount_minor <= out.remaining_minor:
        raise CashflowError('Observed bill payment must be positive and no more than the remaining payable.')
    if type(day) is not date or day < ledger.as_of:
        raise CashflowError('A new observed bill payment cannot predate the current cash snapshot.')
    if any(day<date.fromisoformat(c['date']) for c in out.payment_adjustments):
        raise CashflowError('A new bill payment cannot predate a correction that determines its remaining payable. Review the source dates.')
    _evidence(ledger, evidence_id); _text(note, 'Payment observation note')
    from .payment_review import payment_candidates, require_ack, remember_ack, stamp_ack_fingerprint
    candidates = payment_candidates(ledger,out.currency,-amount_minor,day)
    require_ack(candidates,acknowledge_duplicates)
    out.observed_payments.append(dict(id=payment_id, amount_minor=amount_minor, date=day.isoformat(),
        evidence_id=evidence_id, note=note.strip(), actor=ctx.actor))
    remember_ack(ledger,'bill_payment',payment_id,candidates,ctx.actor,evidence_id)
    stamp_ack_fingerprint(ledger,'bill_payment',payment_id)
    if out.remaining_minor == 0:
        out.status, out.paid_date = 'paid', max([date.fromisoformat(p['date']) for p in bill_payment_entries(out)]+
            [date.fromisoformat(c['date']) for c in out.payment_adjustments])
    return _finish(ledger, key, 'bill_payment_observed', payload,
        dict(outflow_id=bill_id, payment_id=payment_id, duplicate=False, changed=True), ctx)


def save_forecast(ledger, label, revision, *, ctx):
    key, duplicate = _begin(ledger, ctx, 'forecast_snapshot_saved', revision, dict(label=label))
    if duplicate:
        return duplicate
    _text(label, 'Forecast label')
    if len(label) > 120 or not 1 <= ledger.horizon_days <= 120:
        raise CashflowError('Use a label up to 120 characters and a 1â€“120 day horizon.')
    result = forecast(ledger, ledger.as_of, ledger.horizon_days)
    from .treasury import restricted_cash
    snapshot_id = 'forecast-' + key[:24]
    snap = dict(id=snapshot_id, label=label.strip(), business_id=ledger.business_id,
        as_of=ledger.as_of.isoformat(), horizon_days=ledger.horizon_days,
        saved_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        currencies={ccy: dict(opening_minor=curve.days[0].opening_minor+restricted_cash(ledger,currency=ccy), floor_minor=curve.floor_minor,
            restricted_at_save_minor=restricted_cash(ledger,currency=ccy), comparison_basis='book_cash',
            days=[dict(date=d.date.isoformat(), inflow_minor=d.inflow_minor, outflow_minor=d.outflow_minor,
                closing_minor=d.closing_minor+restricted_cash(ledger,currency=ccy),
                decision_closing_minor=d.closing_minor) for d in curve.days]) for ccy, curve in result.currencies.items()},
        invoices=[dict(id=r.id, currency=r.currency, expected_date=r.expected_date.isoformat() if r.expected_date else None,
            collection_hold=copy.deepcopy(r.collection_hold),
            remaining_minor=r.remaining_minor, evidence_id=r.evidence_id,
            date_evidence_id=ledger.collection_date_source(r.id)) for r in ledger.receivables.values()],
        assumptions=list(result.assumptions), clarifications=copy.deepcopy(result.clarifications))
    ledger.forecast_snapshots[snapshot_id] = snap
    return _finish(ledger, key, 'forecast_snapshot_saved', dict(label=label), dict(snapshot_id=snapshot_id), ctx)


def compare_forecast(ledger, snapshot_id, through, *, ctx):
    require_access(ctx, ledger, OP_READ)
    snap = ledger.forecast_snapshots.get(snapshot_id)
    if snap is None or snap['business_id'] != ledger.business_id:
        raise CashflowError('Choose a saved forecast for this business.')
    start = date.fromisoformat(snap['as_of'])
    end = start + timedelta(days=snap['horizon_days']-1)
    if type(through) is not date or not start <= through <= end:
        raise CashflowError('Evaluation date must lie inside the saved forecast window.')
    events = list(cash_events(ledger))
    adjustments = []
    for operation in ledger.owner_operations.values():
        if operation['kind'] == 'daily_cash_snapshot_confirmed':
            payload = operation['payload']
            for item in payload.get('adjustments', []):
                account = ledger.accounts.get(item['account_id'])
                if account:
                    adjustments.append(dict(currency=account.currency, date=payload['new_as_of'],
                        amount_minor=item['variance_minor'], evidence_id=item['evidence_id']))
    currencies = {}
    for ccy, saved in snap['currencies'].items():
        balance, rows = saved['opening_minor'], []
        for planned in saved['days']:
            day = date.fromisoformat(planned['date'])
            if day > through: break
            today = [e for e in events if e['currency'] == ccy and e['date'] == day]
            variance = sum(a['amount_minor'] for a in adjustments if a['currency'] == ccy and a['date'] == planned['date'])
            # The saved opening already includes variances recorded on its start date.
            if day == start: variance = 0
            receipts = sum(e['amount_minor'] for e in today if e['kind'] == 'observed_receipt')
            corrections = sum(e['amount_minor'] for e in today if e['kind'] == 'receipt_correction')
            paid = -sum(e['amount_minor'] for e in today if e['kind'] == 'observed_bill_payment')
            bill_corrections=sum(e['amount_minor'] for e in today if e['kind']=='bill_payment_correction')
            other_cash=sum(e['amount_minor'] for e in today if e['kind'] in {'other_cash_observed','other_cash_correction','refund_observed','refund_correction','unallocated_refund_observed','unallocated_refund_correction'})
            balance += receipts + corrections - paid + bill_corrections + other_cash + variance
            rows.append(dict(planned, recorded_receipts_minor=receipts, receipt_corrections_minor=corrections,
                recorded_paid_minor=paid,bill_payment_corrections_minor=bill_corrections, snapshot_variance_minor=variance, recorded_closing_minor=balance,
                other_cash_minor=other_cash,
                difference_minor=balance-planned['closing_minor'], evidence_ids=sorted({e['evidence_id'] for e in today} |
                    {a['evidence_id'] for a in adjustments if a['currency'] == ccy and a['date'] == planned['date']})))
        currencies[ccy] = rows
    from .extension import movement_date
    invoice_rows = []
    for item in snap['invoices']:
        total = sum(r.amount_minor for r in ledger.receipts.values() if r.state == 'observed'
            and r.receivable_id == item['id'] and start <= movement_date(ledger,'receipt:'+r.id,r.date_received) <= through)
        for change in ledger.receipt_adjustments.values():
            if start <= date.fromisoformat(change['date']) <= through:
                if change['old_invoice_id'] == item['id']: total -= change['old_amount_minor']
                if change['new_invoice_id'] == item['id']: total += change['new_amount_minor']
        rec=ledger.receivables.get(item['id'])
        if rec:
            from .extension import effective_refund
            total-=sum(effective_refund(r) for r in rec.refunds if start<=movement_date(ledger,'refund:'+r['id'],r['date'])<=through)
        invoice_rows.append(dict(item, recorded_net_allocation_minor=total))
    return dict(snapshot=copy.deepcopy(snap), through=through.isoformat(), currencies=currencies,
        invoices=invoice_rows, note='Recorded observations only. Missing observations are not proof that a payment did not occur. '
            'Date corrections restate open-period observation dates; original forecast and source dates are retained. Refunds are included in other cash. This is not a calibrated accuracy score or independent bank reconciliation.')


def correct_bill_payment(ledger,bill_id,payment_id,amount_minor,day,evidence_id,note,revision,*,ctx):
    payload=dict(bill_id=bill_id,payment_id=payment_id,amount_minor=amount_minor,date=str(day),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'bill_payment_corrected',revision,payload)
    if old:return old
    out=ledger.outflows.get(bill_id)
    if out is None:raise CashflowError('Choose an existing bill in this business.')
    try:current=effective_bill_payment(out,payment_id,ledger)
    except KeyError:raise CashflowError('Choose an original payment observation for this bill.')
    previous=[c for c in out.payment_adjustments if c['payment_id']==payment_id]
    earliest=max([ledger.as_of,date.fromisoformat(current['date'])]+[date.fromisoformat(c['date']) for c in previous])
    if type(day) is not date or day<earliest:raise CashflowError('Correction cannot predate the opening snapshot, original payment or previous correction.')
    if type(amount_minor) is int and out.paid_minor-current['amount_minor']+amount_minor<0:
        raise CashflowError('This payment supports an observed supplier refund. Correct the refund first; net paid cannot become negative.')
    available=max(0,out.effective_total_minor-(out.paid_minor-current['amount_minor']))
    if type(amount_minor) is not int or not 0<=amount_minor<=available:
        raise CashflowError('Corrected amount must be nonnegative integer minor units and cannot overpay this bill.')
    from .extension import movement_date
    refund_dates=[movement_date(ledger,'refund:'+r['id'],r['date']) for r in out.refunds]
    refund_dates += [date.fromisoformat(c['date']) for r in out.refunds for c in r['adjustments']]
    if refund_dates and day<max(refund_dates):
        raise CashflowError('Payment correction cannot predate a refund supported by this bill.')
    if amount_minor==current['amount_minor']:raise CashflowError('No amount change was proposed. Original payment stays unchanged.')
    _evidence(ledger,evidence_id);_text(note,'Payment correction reason')
    # Freeze the virtual legacy original before its fully paid status can change.
    if not out.observed_payments and not out.legacy_payment:
        out.legacy_payment=dict(bill_payment_entries(out)[0])
    correction=dict(id='pay-correction-'+key[:24],payment_id=payment_id,old_amount_minor=current['amount_minor'],
        new_amount_minor=amount_minor,currency=out.currency,date=day.isoformat(),cash_delta_minor=current['amount_minor']-amount_minor,
        evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    out.payment_adjustments.append(correction)
    if out.remaining_minor==0:
        out.status='adjusted' if out.face_adjustments and (out.paid_minor!=out.effective_total_minor or not out.paid_minor) else 'paid'
        out.paid_date=max([date.fromisoformat(p['date']) for p in bill_payment_entries(out)]+
            [date.fromisoformat(c['date']) for c in out.payment_adjustments]) if out.status=='paid' else None
    else:out.status='scheduled';out.paid_date=None
    payload['correction']=correction
    return _finish(ledger,key,'bill_payment_corrected',payload,
        dict(outflow_id=out.id,payment_id=payment_id,correction_id=correction['id'],cash_delta_minor=correction['cash_delta_minor']),ctx)


def set_collection_hold(ledger,invoice_id,active,source,note,revision,*,ctx):
    payload=dict(invoice_id=invoice_id,active=active,evidence_id=source,note=note)
    key,old=_begin(ledger,ctx,'collection_hold_changed',revision,payload)
    if old:return old
    rec=ledger.receivables.get(invoice_id)
    if rec is None or type(active) is not bool:raise CashflowError('Choose an invoice and explicit hold/release status.')
    if active and rec.remaining_minor<=0:raise CashflowError('This invoice has no remaining collection to hold.')
    was_active=bool(rec.collection_hold and rec.collection_hold['active'])
    if was_active==active:raise CashflowError('Choose a new hold/release status; the current status remains unchanged.')
    _evidence(ledger,source);_text(note,'Hold/resolution reason')
    payload['previous_hold']=copy.deepcopy(rec.collection_hold)
    eid='ev-collection-hold-'+key[:24]
    ledger.evidence[eid]=Evidence(eid,'owner_confirmed_collection_hold',
        f'Owner {ctx.actor} {"held" if active else "released"} expected collection for {rec.id}; source {source}; reason {note.strip()}. '
        'Original receivable/receipt history remains; no payment, counterparty consent or legal write-off.')
    rec.collection_hold=dict(active=active,evidence_id=eid,source_evidence_id=source,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'collection_hold_changed',payload,dict(invoice_id=invoice_id,active=active,evidence_id=eid),ctx)
