"""Source-backed owner entries, accounting adjustments and statement review.

Documents supply evidence only. No function executes a payment or refund.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
import re

from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .models import Evidence, Receivable, Outflow
from .owner_workflows import _begin, _finish, _evidence, _text, review_revision
from .payment_review import observed_movements, payment_candidates, require_ack, remember_ack, stamp_ack_fingerprint
from .planning import _hash
from .settlement import effective_split_payment, effective_receipt, bill_payment_entries

MAX_MONEY=10**15
CASH_CLASSES={'bank_fee':-1,'owner_capital':1,'loan_received':1,'owner_withdrawal':-1,
              'loan_repayment':-1,'other_inflow':1,'other_outflow':-1}


def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',value):
        raise CashflowError('Use an ID with 1–64 letters/digits/dashes/underscores.')


def _amount(value,zero=False):
    if type(value) is not int or not (0 if zero else 1)<=value<=MAX_MONEY:
        raise CashflowError('Use integer minor units from '+('0' if zero else '1')+' to 10^15.')


def _currency(ledger,currency):
    if not any(a.currency==currency for a in ledger.accounts.values()):
        raise CashflowError('This currency needs a cash account; no FX conversion is made.')


def _day(value):
    if type(value) is not date:raise CashflowError('Use a calendar date, not an invented or missing payment date.')


def _text_short(value,label):
    if not isinstance(value,str) or len(value)>200:raise CashflowError(label+' must be text up to 200 characters.')


def create_record(ledger,record,revision,*,ctx,acknowledge_duplicates=False):
    payload=dict(record=record,acknowledge_duplicates=acknowledge_duplicates)
    key,old=_begin(ledger,ctx,'manual_record_created',revision,payload)
    if old:return old
    if not isinstance(record,dict) or set(record)!={'kind','id','currency','amount_minor','date','due_date','category','protected','counterparty','invoice_reference','evidence_id','note'}:
        raise CashflowError('Use only the manual invoice/bill form fields.')
    r=record;_id(r['id']);_amount(r['amount_minor']);_currency(ledger,r['currency'])
    if r['kind'] not in {'invoice','bill'}:raise CashflowError('Choose invoice or bill.')
    if len(ledger.receivables)+len(ledger.outflows)>=600:raise CashflowError('Manual entry supports at most 600 invoices/bills.')
    if r['id'] in ledger.receivables or r['id'] in ledger.outflows:raise IdempotencyConflict('Record ID already exists. Use the existing source adjustment workflow; no overwrite occurred.')
    _text_short(r['counterparty'],'Party');_text_short(r['invoice_reference'],'External reference')
    _evidence(ledger,r['evidence_id']);_text(r['note'],'Source review note')
    if type(r['protected']) is not bool:raise CashflowError('Protected flag must be explicit.')
    try:
        day=date.fromisoformat(r['date']) if r['date'] else None
        due=date.fromisoformat(r['due_date']) if r['due_date'] else None
    except (ValueError,TypeError):raise CashflowError('Use ISO dates or leave an invoice expectation/due date unknown.')
    if r['kind']=='invoice':
        if r['category']!='invoice' or r['protected']:raise CashflowError('Invoice creation cannot supply bill protection/consent fields.')
        item=Receivable(r['id'],r['currency'],r['amount_minor'],day,r['evidence_id'],due_date=due,
            counterparty=r['counterparty'].strip(),invoice_reference=r['invoice_reference'].strip(),identity_evidence_id=r['evidence_id'])
    else:
        if day is None or due is not None or r['category'] not in {'payroll','supplier','operations'}:
            raise CashflowError('Bills need a contractual date/category; invoice due-date field must be blank.')
        item=Outflow(r['id'],r['currency'],r['amount_minor'],day,r['protected'] or r['category']=='payroll',r['evidence_id'],
            counterparty_consent_required_for_change=r['category']=='supplier',counterparty=r['counterparty'].strip(),
            invoice_reference=r['invoice_reference'].strip(),identity_evidence_id=r['evidence_id'])
    from .operations import descriptor,duplicate_reason
    collection=ledger.receivables if r['kind']=='invoice' else ledger.outflows
    warnings=[o.id for o in collection.values() if duplicate_reason(descriptor(r['kind'],item),descriptor(r['kind'],o))]
    if warnings and acknowledge_duplicates is not True:raise CashflowError('Possible duplicate source records: '+', '.join(warnings)+'. Check and explicitly acknowledge before keeping this new record.')
    collection[item.id]=item
    ledger.entry_records[item.id]=copy.deepcopy(dict(record,actor=ctx.actor,possible_duplicate_ids=warnings))
    return _finish(ledger,key,'manual_record_created',payload,dict(item_id=item.id,kind=r['kind'],possible_duplicate_ids=warnings),ctx)


def adjust_face(ledger,kind,item_id,new_total,day,evidence_id,note,revision,*,ctx):
    payload=dict(kind=kind,item_id=item_id,new_total_minor=new_total,date=str(day),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'source_amount_adjusted',revision,payload)
    if old:return old
    collection=ledger.receivables if kind=='invoice' else ledger.outflows if kind=='bill' else {}
    item=collection.get(item_id)
    if item is None:raise CashflowError('Choose an existing invoice or bill in this business.')
    _amount(new_total,True);_day(day)
    if day!=ledger.as_of:raise CashflowError('A source amount adjustment applies at the current snapshot date; no historical/future claim restatement is invented.')
    if new_total==item.effective_total_minor:raise CashflowError('No amount change proposed.')
    if kind=='bill' and item.protected:raise CashflowError('Protected payroll/obligation cannot be reduced, cancelled or changed through generic bill adjustments.')
    _evidence(ledger,evidence_id);_text(note,'Credit/correction source and reason')
    change=dict(id='face-'+key[:24],old_total_minor=item.effective_total_minor,new_total_minor=new_total,
        date=day.isoformat(),evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    if kind=='bill':
        from .settlement import bill_source_schedule
        schedule=bill_source_schedule(item)
        delta=new_total-item.effective_total_minor
        if sum(m for d,m in schedule)!=item.effective_total_minor:raise CashflowError('Recorded bill schedule does not match its effective source total; review the source before adjustment.')
        if delta>0:schedule.append((item.date,delta))
        elif delta<0:
            remove=-delta;kept=[]
            for d,m in sorted(schedule,reverse=True):
                used=min(remove,m);remove-=used
                if m>used:kept.append((d,m-used))
            schedule=kept
        grouped={}
        for d,m in schedule:grouped[d]=grouped.get(d,0)+m
        change['schedule']=[(d.isoformat(),m) for d,m in sorted(grouped.items()) if m]
        if item.status=='paid' and not item.observed_payments and not item.legacy_payment:
            originals=bill_payment_entries(item)
            if not originals:raise CashflowError('Legacy paid bill has no dated observation. Review payment source before a source amount adjustment.')
            item.legacy_payment=copy.deepcopy(originals[0])
    item.face_adjustments.append(change)
    if kind=='bill':
        if item.remaining_minor:
            item.status,item.paid_date='scheduled',None
        elif item.paid_minor==item.effective_total_minor and item.paid_minor>0:
            item.status='paid'
            dates=[date.fromisoformat(p['date']) for p in bill_payment_entries(item)]+[date.fromisoformat(c['date']) for c in item.payment_adjustments]
            item.paid_date=max(dates) if dates else None
        else:
            item.status,item.paid_date='adjusted',None
    result=dict(kind=kind,item_id=item_id,adjustment_id=change['id'],remaining_minor=item.remaining_minor,
        credit_or_overpaid_minor=item.credit_balance_minor if kind=='invoice' else item.overpaid_minor)
    return _finish(ledger,key,'source_amount_adjusted',payload,result,ctx)


def correct_split_payment(ledger,payment_id,new_amount,day,evidence_id,note,revision,*,ctx):
    payload=dict(payment_id=payment_id,new_amount_minor=new_amount,date=str(day),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'split_payment_corrected',revision,payload)
    if old:return old
    parent=ledger.split_receipts.get(payment_id)
    if parent is None or parent['business_id']!=ledger.business_id:raise CashflowError('Choose a split payment in this business.')
    current=effective_split_payment(ledger,parent);_amount(new_amount,True);_day(day)
    dates=[ledger.as_of,date.fromisoformat(current['date'])]+[ledger.receipts[r].date_received for r in parent['allocation_ids']]
    dates += [date.fromisoformat(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==payment_id]
    dates += [date.fromisoformat(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
    if day<max(dates):raise CashflowError('Correction cannot predate opening, payment, allocations or capacity-changing corrections.')
    from .workbench import unallocated_refund_total,split_dependency_dates
    dates += split_dependency_dates(ledger,parent)
    if day<max(dates):raise CashflowError('Whole-payment correction cannot predate a related unallocated refund/correction.')
    refunded=unallocated_refund_total(ledger,payment_id)
    allocated=sum(effective_receipt(ledger,r).amount_minor for r in parent['allocation_ids'])+refunded
    if new_amount<allocated:raise CashflowError('Correct/reverse affected child allocations first; whole total cannot be less than its effective allocated amount.')
    if new_amount==current['amount_minor']:raise CashflowError('No whole-payment amount change proposed.')
    _evidence(ledger,evidence_id);_text(note,'Whole-payment source correction')
    ident='split-correction-'+key[:24]
    ledger.split_adjustments[ident]=dict(id=ident,payment_id=payment_id,currency=parent['currency'],
        sequence=1+sum(c['payment_id']==payment_id for c in ledger.split_adjustments.values()),
        old_amount_minor=current['amount_minor'],new_amount_minor=new_amount,cash_delta_minor=new_amount-current['amount_minor'],
        date=day.isoformat(),evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'split_payment_corrected',payload,dict(payment_id=payment_id,correction_id=ident,
        cash_delta_minor=new_amount-current['amount_minor'],unallocated_minor=new_amount-allocated),ctx)


def record_cash_movement(ledger,movement_id,classification,currency,amount,day,evidence_id,note,revision,*,ctx,acknowledge_duplicates=False):
    payload=dict(id=movement_id,classification=classification,currency=currency,amount_minor=amount,date=str(day),
        evidence_id=evidence_id,note=note,acknowledge_duplicates=acknowledge_duplicates)
    key,old=_begin(ledger,ctx,'other_cash_observed',revision,payload)
    if old:return old
    _id(movement_id);_amount(amount);_currency(ledger,currency);_day(day)
    if classification not in CASH_CLASSES:raise CashflowError('Choose a non-sales cash classification from the form.')
    from .extension import observation_ids
    used=observation_ids(ledger)
    used.update(p['id'] for o in ledger.outflows.values() for p in bill_payment_entries(o))
    if movement_id in used:raise IdempotencyConflict('Cash observation ID already exists; use its original retry/correction workflow.')
    if not ledger.as_of<=day<=ledger.as_of+timedelta(days=366):raise CashflowError('New cash movement must be on/after opening and at most 366 days ahead in this synthetic sandbox.')
    _evidence(ledger,evidence_id);_text(note,'Observed cash source check')
    signed=amount*CASH_CLASSES[classification];candidates=payment_candidates(ledger,currency,signed,day);require_ack(candidates,acknowledge_duplicates)
    ledger.cash_movements[movement_id]=dict(id=movement_id,business_id=ledger.business_id,classification=classification,currency=currency,
        signed_minor=signed,date=day.isoformat(),evidence_id=evidence_id,note=note.strip(),actor=ctx.actor,adjustments=[])
    remember_ack(ledger,'other_cash',movement_id,candidates,ctx.actor,evidence_id);stamp_ack_fingerprint(ledger,'other_cash',movement_id)
    return _finish(ledger,key,'other_cash_observed',payload,dict(movement_id=movement_id,signed_minor=signed,
        possible_duplicate_ids=[c['key'] for c in candidates]),ctx)


def correct_cash_movement(ledger,movement_id,new_amount,day,evidence_id,note,revision,*,ctx):
    payload=dict(movement_id=movement_id,new_amount_minor=new_amount,date=str(day),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'other_cash_corrected',revision,payload)
    if old:return old
    item=ledger.cash_movements.get(movement_id)
    if item is None:raise CashflowError('Choose an existing other-cash observation.')
    _amount(new_amount,True);_day(day);_evidence(ledger,evidence_id);_text(note,'Cash correction reason')
    from .extension import movement_date
    dates=[ledger.as_of,movement_date(ledger,'other_cash:'+movement_id,item['date'])]+[date.fromisoformat(c['date']) for c in item['adjustments']]
    if day<max(dates):raise CashflowError('Cash correction cannot predate opening, original observation or a previous correction.')
    current=item['adjustments'][-1]['new_signed_minor'] if item['adjustments'] else item['signed_minor']
    signed=new_amount*CASH_CLASSES[item['classification']]
    if current==signed:raise CashflowError('No amount change proposed.')
    ident='cash-correction-'+key[:24]
    item['adjustments'].append(dict(id=ident,old_signed_minor=current,new_signed_minor=signed,cash_delta_minor=signed-current,
        date=day.isoformat(),evidence_id=evidence_id,note=note.strip(),actor=ctx.actor))
    return _finish(ledger,key,'other_cash_corrected',payload,dict(movement_id=movement_id,cash_delta_minor=signed-current),ctx)


def import_statement(ledger,rows,digest,note,revision,*,ctx,context=None):
    require_access(ctx,ledger)
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise CashflowError('Statement requires the uploaded file SHA-256 digest.')
    if not isinstance(rows,list) or len(rows)>100 or (not rows and context is None):raise CashflowError('Import 1–100 rows, or a mapped header-only zero-activity statement with account and period.')
    if context is not None:
        from .statement_mapping import validate_context
        validate_context(ledger,context,rows)
    identity=[ledger.business_id,digest]
    if context is not None:identity += [context['account_id'],context['start'],context['end']]
    batch_id='statement-'+_hash(identity)[:24]
    prior=ledger.statement_batches.get(batch_id)
    if prior:
        if prior['rows']!=rows or prior.get('context')!=context:raise IdempotencyConflict('Same file/account/period presented with different statement rows or mapping.')
        return dict(batch_id=batch_id,duplicate=True)
    payload=dict(rows=rows,file_digest=digest,note=note)
    if context is not None:payload['context']=context
    key,old=_begin(ledger,ctx,'statement_imported',revision,payload)
    if old:return old
    _text(note,'Statement source note')
    ids=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'id','currency','signed_minor','date','reference'}:raise CashflowError('Unexpected statement fields.')
        _id(row['id']);_currency(ledger,row['currency']);_text_short(row['reference'],'Statement reference')
        if row['id'] in ids:raise CashflowError('Statement row IDs must be unique within a file.')
        ids.add(row['id'])
        if type(row['signed_minor']) is not int or not 0<abs(row['signed_minor'])<=MAX_MONEY:raise CashflowError('Statement amounts must be nonzero signed integer minor units.')
        try:date.fromisoformat(row['date'])
        except (ValueError,TypeError):raise CashflowError('Each statement row needs an ISO date.')
    eid='ev-'+batch_id
    ledger.evidence[eid]=Evidence(eid,'synthetic_statement_import',f'Synthetic statement file sha256 {digest}; {len(rows)} rows. {note.strip()}. Evidence only; not bank validation or payment authority.')
    ledger.statement_batches[batch_id]=dict(id=batch_id,business_id=ledger.business_id,rows=copy.deepcopy(rows),file_digest=digest,
        evidence_id=eid,note=note.strip(),actor=ctx.actor)
    if context is not None:
        ledger.statement_batches[batch_id]['context']=copy.deepcopy(context)
        ledger.statement_accounts[batch_id]=dict(account_id=context['account_id'],evidence_id=eid,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'statement_imported',payload,dict(batch_id=batch_id),ctx)


def _row_key(batch_id,row_id):return _hash([batch_id,row_id])


def statement_report(ledger,batch_id,*,ctx):
    require_access(ctx,ledger,OP_READ)
    batch=ledger.statement_batches.get(batch_id)
    if batch is None or batch['business_id']!=ledger.business_id:raise CashflowError('Choose a statement imported for this business.')
    from .workbench import current_match_members
    movements={r['key']:r for r in observed_movements(ledger)};rows=[]
    from .treasury import statement_account, currency_accounts, default_account
    scope = statement_account(ledger, batch_id)
    ambiguous = any(len(currency_accounts(ledger, r['currency'])) > 1 for r in batch['rows']) and not scope
    for row in batch['rows']:
        review=ledger.statement_reviews.get(_row_key(batch_id,row['id']))
        members=current_match_members(review,movements)
        if ambiguous or (scope and any(movements[k].get('account_id', default_account(ledger, movements[k]['currency'])) != scope for k in members)):
            members = set()
        status=('matched' if members else 'needs_review') if review and review['status']=='matched' else review['status'] if review else 'unreviewed'
        candidates=[r for r in movements.values() if not ambiguous and (not scope or r.get('account_id', default_account(ledger, r['currency'])) == scope) and r['currency']==row['currency'] and r['signed_minor']==row['signed_minor']
            and abs((date.fromisoformat(r['date'])-date.fromisoformat(row['date'])).days)<=7]
        rows.append(dict(row,review=copy.deepcopy(review),status=status,candidates=candidates))
    used=set().union(*(current_match_members(r['review'],movements) for r in rows if r['status']=='matched'))
    windows={ccy:(min(r['date'] for r in batch['rows'] if r['currency']==ccy),max(r['date'] for r in batch['rows'] if r['currency']==ccy)) for ccy in {r['currency'] for r in batch['rows']}}
    if batch.get('context'):
        c=batch['context'];windows={ledger.accounts[c['account_id']].currency:(c['start'],c['end'])}
    unmatched=[r for r in movements.values() if (not scope or r.get('account_id', default_account(ledger,r['currency']))==scope) and r['currency'] in windows and windows[r['currency']][0]<=r['date']<=windows[r['currency']][1] and r['key'] not in used]
    return dict(batch=copy.deepcopy(batch),rows=rows,ledger_movements_without_match=unmatched,revision=review_revision(ledger),
        account_scope=scope, account_scope_required=ambiguous,
        note=('Confirm account scope in Cash accounts before matching this multi-account statement. ' if ambiguous else '') + 'Transaction matching only. The imported file may be incomplete and supplies no verified bank opening/closing balance. '
             'Candidates use same currency/signed amount and dates within 7 calendar days; identity/reference and posting-date differences require owner source checks. '
             'Current effective observations are matched, not accounting adjustment deltas. No cash, receipt, allocation, consent or action reconciliation is created.')


def review_statement_row(ledger,batch_id,row_id,movement_key,status,evidence_id,note,revision,*,ctx):
    payload=dict(batch_id=batch_id,row_id=row_id,movement_key=movement_key,status=status,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'statement_row_reviewed',revision,payload)
    if old:return old
    report=statement_report(ledger,batch_id,ctx=ctx);row=next((r for r in report['rows'] if r['id']==row_id),None)
    if row is None or status not in {'matched','unmatched','excluded'}:raise CashflowError('Review a current statement row and explicit disposition.')
    _evidence(ledger,evidence_id);_text(note,'Statement identity/reference review')
    selected=None
    if status=='matched':
        selected=next((r for r in row['candidates'] if r['key']==movement_key),None)
        if selected is None:raise CashflowError('Choose a current same-currency/amount candidate within the stated date window.')
        from .workbench import ensure_match_available
        ensure_match_available(ledger,[movement_key],{r['key']:r for r in observed_movements(ledger)},_row_key(batch_id,row_id))
    elif movement_key:raise CashflowError('An unmatched/excluded decision cannot claim a payment match.')
    ledger.statement_reviews[_row_key(batch_id,row_id)]=dict(batch_id=batch_id,row_id=row_id,status=status,
        movement_key=movement_key,fingerprint=selected['fingerprint'] if selected else None,evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'statement_row_reviewed',payload,dict(batch_id=batch_id,row_id=row_id,status=status),ctx)
