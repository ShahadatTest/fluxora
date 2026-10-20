"""Observed cash events and residual bill schedules; no model authority."""
from dataclasses import replace
from datetime import date


def effective_receipt(ledger, receipt_id):
    original = ledger.receipts[receipt_id]
    result = original
    for change in ledger.receipt_adjustments.values():
        if change['receipt_id'] == receipt_id:
            result = replace(original, receivable_id=change['new_invoice_id'],
                amount_minor=change['new_amount_minor'], evidence_id=change['evidence_id'])
    if split_parent(ledger, receipt_id) is None:
        from .extension import movement_date
        result = replace(result, date_received=movement_date(ledger, 'receipt:'+receipt_id, original.date_received))
    return result


def receipt_fingerprint(ledger, receipt_id):
    # Include adjustment IDs, even if a later correction restores old terms.
    from .planning import _hash
    value={'original': ledger.receipts[receipt_id], 'adjustments': [
        r for r in ledger.receipt_adjustments.values() if r['receipt_id'] == receipt_id]}
    changes=[c for c in ledger.date_corrections.values() if c['movement_key']=='receipt:'+receipt_id]
    if changes:value['date_changes']=changes
    return _hash(value)


def bill_source_schedule(out):
    if out.agreement_schedules and out.agreement_schedules[-1]['face_count']==len(out.face_adjustments):
        return [(date.fromisoformat(d),m) for d,m in out.agreement_schedules[-1]['schedule']]
    return ([(date.fromisoformat(d),m) for d,m in out.face_adjustments[-1]['schedule']]
        if out.face_adjustments else list(out.recorded_splits or [(out.date,out.amount_minor)]))


def bill_schedule(out):
    """Allocate observed payments to oldest scheduled instalments first."""
    paid = out.paid_minor
    result = []
    source = bill_source_schedule(out)
    for day, amount in sorted(source):
        used = min(paid, amount)
        paid -= used
        if amount > used:
            result.append((day, amount - used))
    return result


def bill_payment_entries(out):
    entries=list(out.observed_payments)
    if out.legacy_payment:
        entries.insert(0,out.legacy_payment)
    elif not entries and not out.credit_entries and out.status=='paid' and out.paid_date is not None:
        entries.append(dict(id='legacy-paid-'+out.id,amount_minor=out.amount_minor,date=out.paid_date.isoformat(),
            evidence_id=out.evidence_id,note='Legacy whole-bill observation; original preserved.',actor='legacy-owner'))
    return entries


def effective_bill_payment(out,payment_id,ledger=None):
    original=next((p for p in bill_payment_entries(out) if p['id']==payment_id),None)
    if original is None:raise KeyError('Unknown bill payment observation.')
    result=dict(original)
    for change in out.payment_adjustments:
        if change['payment_id']==payment_id:
            result.update(amount_minor=change['new_amount_minor'],evidence_id=change['evidence_id'])
    if ledger is not None:
        from .extension import movement_date
        result['date']=str(movement_date(ledger,'bill_payment:'+payment_id,original['date']))
    return result


def collection_held(rec):
    return bool(rec.collection_hold and rec.collection_hold['active'])


def split_parent(ledger, receipt_id):
    return next((p for p in ledger.split_receipts.values() if receipt_id in p['allocation_ids']), None)


def split_unallocated(ledger, parent):
    from .workbench import unallocated_refund_total
    return effective_split_payment(ledger, parent)['amount_minor'] - sum(effective_receipt(ledger, rid).amount_minor for rid in parent['allocation_ids']) - unallocated_refund_total(ledger,parent['id'])


def effective_split_payment(ledger, parent):
    result = dict(parent)
    changes=[c for c in ledger.split_adjustments.values() if c['payment_id']==parent['id']]
    for index,change in sorted(enumerate(changes),key=lambda pair:pair[1].get('sequence',pair[0]+1)):
        result.update(amount_minor=change['new_amount_minor'], evidence_id=change['evidence_id'])
    from .extension import movement_date
    result['date']=str(movement_date(ledger,'split_payment:'+parent['id'],parent['date']))
    return result


def _original_cash_events(ledger):
    children = {rid for p in ledger.split_receipts.values() for rid in p['allocation_ids']}
    for parent in ledger.split_receipts.values():
        yield dict(id=parent['id'], kind='observed_receipt', currency=parent['currency'],
            date=date.fromisoformat(parent['date']), amount_minor=parent['amount_minor'],
            evidence_id=parent['evidence_id'], receipt_id=parent['id'])
    for change in ledger.split_adjustments.values():
        if change['cash_delta_minor']:
            yield dict(id=change['id'], kind='receipt_correction', currency=change['currency'],
                date=date.fromisoformat(change['date']), amount_minor=change['cash_delta_minor'],
                evidence_id=change['evidence_id'], receipt_id=change['payment_id'])
    for movement in ledger.cash_movements.values():
        yield dict(id=movement['id'], kind='other_cash_observed', currency=movement['currency'],
            date=date.fromisoformat(movement['date']), amount_minor=movement['signed_minor'],
            evidence_id=movement['evidence_id'], classification=movement['classification'])
        for change in movement['adjustments']:
            if change['cash_delta_minor']:
                yield dict(id=change['id'], kind='other_cash_correction', currency=movement['currency'],
                    date=date.fromisoformat(change['date']), amount_minor=change['cash_delta_minor'],
                    evidence_id=change['evidence_id'], classification=movement['classification'])
    for receipt in ledger.receipts.values():
        if receipt.state == 'observed' and receipt.id not in children:
            yield dict(id=receipt.id, kind='observed_receipt', currency=ledger.receivables[receipt.receivable_id].currency,
                date=receipt.date_received, amount_minor=receipt.amount_minor, evidence_id=receipt.evidence_id,
                receipt_id=receipt.id, invoice_id=receipt.receivable_id)
    for key, change in ledger.receipt_adjustments.items():
        if change['cash_delta_minor']:
            yield dict(id=key, kind='receipt_correction', currency=change['currency'],
                date=date.fromisoformat(change['date']), amount_minor=change['cash_delta_minor'],
                evidence_id=change['evidence_id'], receipt_id=change['receipt_id'])
    for out in ledger.outflows.values():
        for payment in bill_payment_entries(out):
            yield dict(id=payment['id'],kind='observed_bill_payment',currency=out.currency,
                date=date.fromisoformat(payment['date']),amount_minor=-payment['amount_minor'],
                evidence_id=payment['evidence_id'],outflow_id=out.id)
        for change in out.payment_adjustments:
            yield dict(id=change['id'],kind='bill_payment_correction',currency=out.currency,
                date=date.fromisoformat(change['date']),amount_minor=change['cash_delta_minor'],
                evidence_id=change['evidence_id'],outflow_id=out.id,payment_id=change['payment_id'])


def _unscoped_cash_events(ledger):
    """Single cash stream; corrected base dates retain original source dates."""
    from .extension import movement_date, refund_entries, _base_event_key
    def dated(event):
        if event['kind'] in {'observed_receipt','observed_bill_payment','other_cash_observed','refund_observed','unallocated_refund_observed'}:
            key=_base_event_key(event); original=event['date']; effective=movement_date(ledger,key,original)
            if effective!=original:
                event=dict(event, date=effective, original_date=original, date_corrected=True,
                    date_evidence_ids=[c['evidence_id'] for c in ledger.date_corrections.values() if c['movement_key']==key])
        return event
    for event in _original_cash_events(ledger):yield dated(event)
    for kind,item,entry in refund_entries(ledger):
        sign=-1 if kind=='invoice' else 1
        yield dated(dict(id=entry['id'],kind='refund_observed',currency=item.currency,
            date=date.fromisoformat(entry['date']),amount_minor=sign*entry['amount_minor'],evidence_id=entry['evidence_id'],
            refund_id=entry['id'],invoice_id=item.id if kind=='invoice' else None,
            outflow_id=item.id if kind=='bill' else None,classification='customer_refund' if kind=='invoice' else 'supplier_refund'))
        for change in entry['adjustments']:
            yield dict(id=change['id'],kind='refund_correction',currency=item.currency,date=date.fromisoformat(change['date']),
                amount_minor=change['cash_delta_minor'],evidence_id=change['evidence_id'],refund_id=entry['id'],
                invoice_id=item.id if kind=='invoice' else None,outflow_id=item.id if kind=='bill' else None,
                classification='customer_refund' if kind=='invoice' else 'supplier_refund')

    for r in ledger.unallocated_refunds.values():
        yield dated(dict(id=r['id'],kind='unallocated_refund_observed',currency=r['currency'],date=date.fromisoformat(r['date']),
            amount_minor=-r['amount_minor'],evidence_id=r['evidence_id'],receipt_id=r['payment_id'],classification='unallocated_customer_refund'))
        for c in r['adjustments']:
            yield dict(id=c['id'],kind='unallocated_refund_correction',currency=r['currency'],date=date.fromisoformat(c['date']),
                amount_minor=c['cash_delta_minor'],evidence_id=c['evidence_id'],receipt_id=r['payment_id'],classification='unallocated_customer_refund')


def cash_events(ledger, *, include_internal=False):
    """Account-scoped observations. Own transfers are excluded from business flows."""
    from .treasury import event_account, transfer_events
    for event in _unscoped_cash_events(ledger):
        yield dict(event, account_id=event_account(ledger, event))
    if include_internal:
        yield from transfer_events(ledger)
