"""Owner-reviewed refunds, date restatements and supplier instalment agreements.

These operations record synthetic facts; no payment or communication is executed.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from .authz import OP_READ, require_access
from .completion import _amount, _day, _id
from .errors import CashflowError, IdempotencyConflict
from .models import Evidence
from .owner_workflows import _begin, _finish, _evidence, _text
from .planning import _hash
from .settlement import (bill_payment_entries, bill_schedule, cash_events,
    effective_bill_payment, effective_receipt, effective_split_payment, split_parent)


def refund_entries(ledger):
    for kind, group in [('invoice', ledger.receivables), ('bill', ledger.outflows)]:
        for item in group.values():
            for entry in item.refunds:
                yield kind, item, entry


def effective_refund(entry):
    return entry['adjustments'][-1]['new_amount_minor'] if entry['adjustments'] else entry['amount_minor']


def observation_ids(ledger):
    return (set(ledger.receipts) | set(ledger.receipt_inbox) | set(ledger.split_receipts)
        | set(ledger.cash_movements) | set(ledger.unallocated_refunds) | {r['id'] for _, _, r in refund_entries(ledger)}
        | {p['id'] for o in ledger.outflows.values() for p in bill_payment_entries(o)})


def movement_date(ledger, key, original):
    changes = [c for c in ledger.date_corrections.values() if c['movement_key'] == key]
    return date.fromisoformat(max(changes, key=lambda c: c['sequence'])['new_date']) if changes else (
        original if type(original) is date else date.fromisoformat(original))


def _credit(item, kind):
    return item.credit_balance_minor if kind == 'invoice' else item.overpaid_minor


def _refresh_bill(ledger,item):
    if item.remaining_minor:
        item.status,item.paid_date='scheduled',None
    elif item.paid_minor==item.effective_total_minor and item.paid_minor>0:
        if item.credit_entries and not bill_payment_entries(item):
            item.status,item.paid_date='adjusted',None
            return
        dates=[movement_date(ledger,'bill_payment:'+p['id'],p['date']) for p in bill_payment_entries(item)]
        dates += [date.fromisoformat(c['date']) for c in item.payment_adjustments]
        item.status,item.paid_date='paid',max(dates) if dates else None
    else:item.status,item.paid_date='adjusted',None


def _refund_source(ledger, kind, item_id):
    group = ledger.receivables if kind == 'invoice' else ledger.outflows if kind == 'bill' else {}
    item = group.get(item_id)
    if item is None:
        raise CashflowError('Choose an invoice credit or supplier overpayment in this business.')
    return item


def _refund_dates(ledger, kind, item, exclude_refund=None):
    days = [ledger.as_of] + [date.fromisoformat(c['date']) for c in item.face_adjustments]
    if kind == 'invoice':
        days += [a.date_received for a in item.allocations]
        for receipt in ledger.receipts.values():
            if effective_receipt(ledger, receipt.id).receivable_id == item.id:
                parent = split_parent(ledger, receipt.id)
                days.append(movement_date(ledger, 'split_payment:' + parent['id'], parent['date']) if parent
                    else movement_date(ledger, 'receipt:' + receipt.id, receipt.date_received))
    else:
        days += [movement_date(ledger, 'bill_payment:' + p['id'], p['date']) for p in bill_payment_entries(item)]
        days += [date.fromisoformat(c['date']) for c in item.payment_adjustments]
    for entry in item.refunds:
        if entry['id'] == exclude_refund: continue
        days.append(movement_date(ledger, 'refund:' + entry['id'], entry['date']))
        days += [date.fromisoformat(c['date']) for c in entry['adjustments']]
    return days


def record_refund(ledger, kind, item_id, refund_id, amount, day, evidence_id, note,
                  revision, *, ctx, acknowledge_duplicates=False):
    payload = dict(kind=kind, item_id=item_id, refund_id=refund_id, amount_minor=amount,
        date=str(day), evidence_id=evidence_id, note=note, acknowledge_duplicates=acknowledge_duplicates)
    key, old = _begin(ledger, ctx, 'refund_observed', revision, payload)
    if old: return old
    item = _refund_source(ledger, kind, item_id)
    _id(refund_id); _amount(amount); _day(day); _evidence(ledger, evidence_id); _text(note, 'Observed refund source check')
    if refund_id in observation_ids(ledger):
        raise IdempotencyConflict('This observation ID already exists; use its original retry or correction workflow.')
    if amount > _credit(item, kind):
        raise CashflowError('Refund exceeds the remaining customer credit / supplier overpayment. No new credit or receipt may be invented.')
    if not max(_refund_dates(ledger, kind, item)) <= day <= ledger.as_of + timedelta(days=366):
        raise CashflowError('Refund must follow its source payments/adjustments, within the open snapshot and 366 days ahead.')
    from .payment_review import payment_candidates, require_ack, remember_ack, stamp_ack_fingerprint
    sign = -1 if kind == 'invoice' else 1
    candidates = payment_candidates(ledger, item.currency, sign * amount, day)
    require_ack(candidates, acknowledge_duplicates)
    if kind == 'bill' and item.status == 'paid' and not item.observed_payments and not item.legacy_payment:
        item.legacy_payment = copy.deepcopy(bill_payment_entries(item)[0])
    item.refunds.append(dict(id=refund_id, amount_minor=amount, date=day.isoformat(),
        evidence_id=evidence_id, note=note.strip(), actor=ctx.actor, adjustments=[]))
    if kind=='bill':_refresh_bill(ledger,item)
    remember_ack(ledger, 'refund', refund_id, candidates, ctx.actor, evidence_id)
    stamp_ack_fingerprint(ledger, 'refund', refund_id)
    return _finish(ledger, key, 'refund_observed', payload, dict(refund_id=refund_id,
        remaining_credit_minor=_credit(item, kind), signed_minor=sign * amount,
        possible_duplicate_ids=[c['key'] for c in candidates]), ctx)


def correct_refund(ledger, refund_id, amount, day, evidence_id, note, revision, *, ctx):
    payload = dict(refund_id=refund_id, amount_minor=amount, date=str(day), evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'refund_corrected', revision, payload)
    if old: return old
    found = next(((k, i, r) for k, i, r in refund_entries(ledger) if r['id'] == refund_id), None)
    if found is None: raise CashflowError('Choose a recorded refund observation.')
    kind, item, entry = found
    _amount(amount, True); _day(day); _evidence(ledger, evidence_id); _text(note, 'Refund correction reason')
    current = effective_refund(entry)
    if amount == current: raise CashflowError('No refund amount change proposed.')
    if amount > current + _credit(item, kind): raise CashflowError('Corrected refund exceeds the source credit capacity.')
    if day < max(_refund_dates(ledger, kind, item)):
        raise CashflowError('Refund correction cannot predate the source, opening or previous correction.')
    ident = 'refund-correction-' + key[:24]
    delta = (amount-current) * (-1 if kind == 'invoice' else 1)
    entry['adjustments'].append(dict(id=ident, new_amount_minor=amount, old_amount_minor=current,
        date=day.isoformat(), cash_delta_minor=delta, evidence_id=evidence_id, note=note.strip(), actor=ctx.actor))
    if kind=='bill':_refresh_bill(ledger,item)
    return _finish(ledger, key, 'refund_corrected', payload,
        dict(refund_id=refund_id, correction_id=ident, cash_delta_minor=delta, remaining_credit_minor=_credit(item, kind)), ctx)


def _base_event_key(event):
    return {'observed_receipt': ('split_payment:' if 'invoice_id' not in event else 'receipt:'),
            'observed_bill_payment': 'bill_payment:', 'other_cash_observed': 'other_cash:',
            'refund_observed': 'refund:', 'unallocated_refund_observed':'unallocated_refund:'}.get(event['kind'], '') + event['id']


def correct_payment_date(ledger, movement_key, new_date, evidence_id, note, revision, *, ctx):
    payload = dict(movement_key=movement_key, new_date=str(new_date), evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'payment_date_corrected', revision, payload)
    if old: return old
    _day(new_date); _evidence(ledger, evidence_id); _text(note, 'Payment date source check')
    from .payment_review import observed_movements
    row = next((r for r in observed_movements(ledger) if r['key'] == movement_key), None)
    if row is None: raise CashflowError('Choose a nonzero whole observed movement; split child allocations have no separate cash date.')
    current = date.fromisoformat(row['date'])
    if new_date == current: raise CashflowError('No payment-date change proposed.')
    if min(current, new_date) < ledger.as_of:
        raise CashflowError('A closed opening already absorbed this observation. Historical opening restatement is unsupported; review a cash snapshot instead.')
    if new_date > ledger.as_of + timedelta(days=366): raise CashflowError('Use a date within 366 days of the open snapshot.')
    kind, ident = movement_key.split(':', 1)
    # Move only the original observation. Amount corrections keep their actual
    # accounting dates, so the corrected observation must precede them.
    dependent = []
    if kind == 'receipt':
        dependent += [c['date'] for c in ledger.receipt_adjustments.values() if c['receipt_id'] == ident]
        item = ledger.receivables[row['target']]
    elif kind == 'split_payment':
        parent = ledger.split_receipts[ident]
        dependent += [ledger.receipts[r].date_received.isoformat() for r in parent['allocation_ids']]
        dependent += [c['date'] for c in ledger.split_adjustments.values() if c['payment_id'] == ident]
        item = None
        for rid in parent['allocation_ids']:
            rec = ledger.receivables[effective_receipt(ledger, rid).receivable_id]
            dependent += [r['date'] for r in rec.refunds]
        from .workbench import split_dependency_dates
        dependent += [str(d) for d in split_dependency_dates(ledger,parent) if d!=current and d!=ledger.as_of]
        dependent += [str(movement_date(ledger,'unallocated_refund:'+r['id'],r['date'])) for r in ledger.unallocated_refunds.values() if r['payment_id']==ident]
    elif kind == 'bill_payment':
        item = ledger.outflows[row['target']]
        dependent += [c['date'] for c in item.payment_adjustments if c['payment_id'] == ident]
    elif kind == 'other_cash':
        item = None
        dependent += [c['date'] for c in ledger.cash_movements[ident]['adjustments']]
    elif kind.startswith('internal_transfer'):
        raise CashflowError('Observed internal transfer legs are immutable. Use their separate compensating reversal, not a payment-date edit.')
    elif kind == 'unallocated_refund':
        from .workbench import split_dependency_dates
        item=None;entry=ledger.unallocated_refunds[ident];parent=ledger.split_receipts[entry['payment_id']]
        dependent += [c['date'] for c in entry['adjustments']]
        if new_date<max(split_dependency_dates(ledger,parent,exclude_refund=ident)):
            raise CashflowError('Unallocated refund date cannot predate its observed parent, allocations or corrections.')
    else:
        item = None
        entry = next(r for _, _, r in refund_entries(ledger) if r['id'] == ident)
        dependent += [c['date'] for c in entry['adjustments']]
        source_kind, source, _ = next((k, i, r) for k, i, r in refund_entries(ledger) if r['id'] == ident)
        # Refund cannot move ahead of the observations supporting its claim.
        other_days = _refund_dates(ledger, source_kind, source, exclude_refund=ident)
        if other_days and new_date < max(other_days): raise CashflowError('Refund date cannot predate its source credit/observations.')
    if item is not None: dependent += [str(movement_date(ledger,'refund:'+r['id'],r['date'])) for r in item.refunds]
    if dependent and new_date > min(date.fromisoformat(d) for d in dependent):
        raise CashflowError('Corrected payment date must precede related allocations, amount corrections and refunds. Their original accounting dates stay in history.')
    ident_change = 'date-correction-' + key[:24]
    sequence = 1 + sum(c['movement_key'] == movement_key for c in ledger.date_corrections.values())
    ledger.date_corrections[ident_change] = dict(id=ident_change, movement_key=movement_key,
        sequence=sequence, old_date=current.isoformat(), new_date=new_date.isoformat(),
        evidence_id=evidence_id, note=note.strip(), actor=ctx.actor)
    # No allocation, amount, action state or closed opening is rewritten.
    return _finish(ledger, key, 'payment_date_corrected', payload,
        dict(movement_key=movement_key, old_date=str(current), new_date=str(new_date)), ctx)


def _bill_basis(ledger,out):
    keys={'bill_payment:'+p['id'] for p in bill_payment_entries(out)}|{'refund:'+r['id'] for r in out.refunds}
    return _hash([out,[c for c in ledger.date_corrections.values() if c['movement_key'] in keys]])


def create_instalments(ledger, proposal_id, bill_id, schedule, evidence_id, note, revision, *, ctx):
    payload = dict(proposal_id=proposal_id, bill_id=bill_id, schedule=schedule, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'instalments_proposed', revision, payload)
    if old: return old
    _id(proposal_id); _evidence(ledger, evidence_id); _text(note, 'Proposed supplier terms')
    if proposal_id in ledger.instalment_proposals: raise IdempotencyConflict('Agreement ID already exists; its terms cannot be overwritten.')
    out = ledger.outflows.get(bill_id)
    if out is None or out.protected or not out.counterparty_consent_required_for_change or not out.remaining_minor:
        raise CashflowError('Choose an unpaid, unprotected supplier bill requiring counterparty consent; payroll cannot be rescheduled.')
    if not ledger.constraints.allow_supplier_date_change: raise CashflowError('Owner constraints disable supplier schedule changes.')
    if any(p['bill_id'] == bill_id and p['state'] not in {'applied', 'cancelled'} for p in ledger.instalment_proposals.values()):
        raise CashflowError('Review or cancel the pending proposal for this bill before another agreement.')
    if not isinstance(schedule, list) or not 1 <= len(schedule) <= 12:
        raise CashflowError('Use 1–12 dated instalments for the remaining payable.')
    days=[]; total=0
    for row in schedule:
        if not isinstance(row, dict) or set(row) != {'date', 'amount_minor'}: raise CashflowError('Use only instalment date and amount.')
        try: d=date.fromisoformat(row['date'])
        except (ValueError, TypeError): raise CashflowError('Each instalment needs a valid ISO date.')
        _amount(row['amount_minor']); total += row['amount_minor']; days.append(d)
        if not ledger.as_of <= d <= ledger.as_of + timedelta(days=366): raise CashflowError('Instalments must fall within the open snapshot and 366 days ahead.')
    if days != sorted(set(days)) or total != out.remaining_minor:
        raise CashflowError('Use strictly increasing dates whose amounts equal the exact remaining payable, in the bill currency.')
    if days[0]<max(_refund_dates(ledger,'bill',out)):
        raise CashflowError('Instalments cannot predate the source payments, refunds or corrections determining the remaining payable.')
    ledger.instalment_proposals[proposal_id] = dict(id=proposal_id, bill_id=bill_id, currency=out.currency,
        schedule=copy.deepcopy(schedule), remaining_minor=out.remaining_minor, basis=_bill_basis(ledger,out),
        state='draft', evidence_id=evidence_id, note=note.strip(), actor=ctx.actor, history=[])
    return _finish(ledger, key, 'instalments_proposed', payload, dict(proposal_id=proposal_id, state='draft'), ctx)


def advance_instalments(ledger, proposal_id, step, evidence_id, note, revision, *, ctx):
    payload = dict(proposal_id=proposal_id, step=step, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'instalments_' + str(step), revision, payload)
    if old: return old
    proposal=ledger.instalment_proposals.get(proposal_id)
    if proposal is None: raise CashflowError('Choose a supplier instalment proposal in this business.')
    _evidence(ledger, evidence_id); _text(note, 'Explicit agreement step source check')
    states={'approve':('draft','owner_approved'), 'accept':('owner_approved','counterparty_accepted'),
            'apply':('counterparty_accepted','applied')}
    if step == 'cancel':
        if proposal['state'] in {'applied','cancelled'}: raise CashflowError('Applied/cancelled agreement cannot be cancelled or revived. Propose a new agreement for later terms.')
        next_state='cancelled'
    else:
        if step not in states or proposal['state'] != states[step][0]: raise CashflowError('Follow draft → owner approval → observed counterparty acceptance → apply. No step can be skipped.')
        out=ledger.outflows[proposal['bill_id']]
        if out.protected or not out.counterparty_consent_required_for_change or not ledger.constraints.allow_supplier_date_change:
            raise CashflowError('Current protection/owner constraints prohibit this supplier agreement.')
        if _bill_basis(ledger,out) != proposal['basis']:
            raise CashflowError('Bill/payment/source terms changed after drafting. Cancel and review a fresh proposal; old consent does not authorize changed terms.')
        next_state=states[step][1]
        if step == 'accept':
            eid='ev-instalment-acceptance-'+key[:24]
            ledger.evidence[eid]=Evidence(eid, 'owner_recorded_synthetic_supplier_acceptance',
                f'Owner {ctx.actor} explicitly recorded external synthetic supplier acceptance of exact proposal {proposal_id}; source {evidence_id}. {note.strip()}. No message was sent; not independent verification.')
            # Keep this exact-terms acceptance outside the older generic date-
            # move consent registry. It cannot authorize a different action/bill.
            proposal['acceptance_evidence_id']=eid
            proposal['acceptance_terms_hash']=_hash([proposal_id,proposal['schedule'],proposal['basis']])
        if step == 'apply':
            acceptance=ledger.evidence.get(proposal.get('acceptance_evidence_id'))
            if (acceptance is None or acceptance.kind!='owner_recorded_synthetic_supplier_acceptance'
                    or proposal.get('acceptance_terms_hash')!=_hash([proposal_id,proposal['schedule'],proposal['basis']])):
                raise CashflowError('Exact-terms runtime counterparty acceptance is missing.')
            # Full source schedule includes the already-paid portion, consumed
            # oldest-first by bill_schedule. It creates no new payment.
            full=([(min(out.date, ledger.as_of),out.paid_minor)] if out.paid_minor else [])
            full += [(date.fromisoformat(r['date']),r['amount_minor']) for r in proposal['schedule']]
            out.agreement_schedules.append(dict(proposal_id=proposal_id,
                face_count=len(out.face_adjustments), schedule=[[str(d),m] for d,m in full],
                evidence_id=proposal['acceptance_evidence_id'], actor=ctx.actor))
    proposal['state']=next_state
    proposal['history'].append(dict(step=step,state=next_state,evidence_id=evidence_id,note=note.strip(),actor=ctx.actor))
    return _finish(ledger,key,'instalments_'+step,payload,dict(proposal_id=proposal_id,state=next_state),ctx)
