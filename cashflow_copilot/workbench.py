"""Guided synthetic setup, promises, net matching, excess refunds and party views.

All mutations are explicit owner operations. Documents supply no authority;
promises and matching supply no cash. Integer minor units and one cash stream.
"""
import base64
import copy
import csv
from datetime import date,timedelta
import hashlib
from io import StringIO
import re

from .authz import OP_READ,require_access
from .completion import _id,_amount,_day,_currency,_row_key
from .errors import CashflowError,IdempotencyConflict
from .extension import observation_ids,movement_date,effective_refund
from .models import Account,Evidence
from .money import MINOR_DIGITS
from .owner_workflows import _begin,_finish,_evidence,_text,review_revision
from .planning import _hash
from .settlement import (effective_receipt,receipt_fingerprint,split_parent,split_unallocated,
    effective_split_payment,bill_payment_entries,effective_bill_payment,collection_held)


def setup_business(ledger,spec,revision,*,ctx):
    require_access(ctx,ledger)
    key=_hash(['guided-setup',revision,spec])
    if key in ledger.setup_receipts:
        if ledger.setup_receipts[key]['business_id']!=ledger.business_id:raise CashflowError('This old setup retry belongs to a different active business; it cannot revive earlier state.')
        return dict(ledger.setup_receipts[key],duplicate=True)
    if revision!=review_revision(ledger):raise CashflowError('Active business changed after preview. Review setup again.')
    if not isinstance(spec,dict) or set(spec)!={'id','name','base_currency','timezone','as_of','horizon_days','accounts','synthetic'}:
        raise CashflowError('Use the guided synthetic setup fields only.')
    _id(spec['id']);_text(spec['name'],'Business name')
    used={ledger.business_id}|{r['business_id'] for r in ledger.setup_receipts.values()}|{r['previous_business_id'] for r in ledger.setup_receipts.values()}
    if spec['id'] in used:raise CashflowError('Use a new business ID. Setup replaces the active business; it is not an opening-balance edit or reuse of an earlier setup identity.')
    if len(spec['name'])>120 or spec['synthetic'] is not True:raise CashflowError('Use a business name up to 120 characters and explicitly synthetic data.')
    if spec['timezone'] not in {'UTC','Asia/Dhaka','Asia/Shanghai','Asia/Singapore','Europe/London','America/New_York'}:
        raise CashflowError('Choose a displayed setup timezone; business dates remain explicit calendar dates.')
    try:opening_day=date.fromisoformat(spec['as_of'])
    except (ValueError,TypeError):raise CashflowError('Choose a valid opening snapshot date.')
    if type(spec['horizon_days']) is not int or not 1<=spec['horizon_days']<=120:raise CashflowError('Forecast horizon must be 1–120 days.')
    if not isinstance(spec['accounts'],list) or not 1<=len(spec['accounts'])<=6:raise CashflowError('Add 1–6 separate currency accounts.')
    from .ledger import Ledger
    fresh=Ledger();fresh.business_id=spec['id'];fresh.business_name=spec['name'].strip()
    fresh.base_currency=spec['base_currency'];fresh.timezone=spec['timezone'];fresh.as_of=opening_day;fresh.horizon_days=spec['horizon_days']
    currencies=set()
    for row in spec['accounts']:
        if not isinstance(row,dict) or set(row)!={'id','currency','opening_minor','floor_minor','source_note'}:raise CashflowError('Each account needs ID, currency, opening, floor and checked source note.')
        _id(row['id']);_amount(row['floor_minor'],True);_text(row['source_note'],'Opening cash source')
        if type(row['opening_minor']) is not int or abs(row['opening_minor'])>10**15:raise CashflowError('Opening cash must be signed integer minor units within 10^15.')
        if row['currency'] not in MINOR_DIGITS or row['currency'] in currencies or row['id'] in fresh.accounts:
            raise CashflowError('Use unique account IDs and one account per supported currency; no FX funding is created.')
        currencies.add(row['currency']);eid='ev-opening-'+_hash([spec['id'],row['id']])[:24]
        fresh.evidence[eid]=Evidence(eid,'owner_attested_synthetic_opening',row['source_note'].strip())
        fresh.accounts[row['id']]=Account(row['id'],row['currency'],row['opening_minor'],row['floor_minor'],eid)
    if fresh.base_currency not in currencies:raise CashflowError('Base currency needs its own opening account.')
    from .authz import RequestContext
    from .business_tools import _save_fact_check
    for a in fresh.accounts.values():
        _save_fact_check(fresh,'cash',a.id,a.evidence_id,fresh.evidence[a.evidence_id].summary,ctx=RequestContext(ctx.actor,ctx.role,fresh.business_id))
    result=dict(business_id=fresh.business_id,account_count=len(fresh.accounts),previous_business_id=ledger.business_id)
    fresh.setup_receipts=copy.deepcopy(ledger.setup_receipts)
    fresh.setup_receipts[key]=result
    fresh.log_audit('guided_business_created',ctx.actor,'Owner explicitly replaced the active synthetic business after reviewing opening sources. No receipt, consent or cash transfer created.')
    ledger.__dict__.clear();ledger.__dict__.update(fresh.__dict__)
    return dict(result,duplicate=False)


def promise_report(ledger,*,ctx):
    require_access(ctx,ledger,OP_READ);rows=[]
    for promise in ledger.payment_promises.values():
        rec=ledger.receivables[promise['invoice_id']];valid=[];stale=[]
        for rid,link in promise['links'].items():
            receipt=effective_receipt(ledger,rid) if rid in ledger.receipts else None
            ok=(receipt is not None and receipt.state=='observed' and receipt.receivable_id==rec.id
                and receipt_fingerprint(ledger,rid)==link['fingerprint'] and receipt.amount_minor>=link['amount_minor']
                and receipt.date_received<=ledger.as_of)
            (valid if ok else stale).append(dict(link,receipt_id=rid))
        paid=sum(x['amount_minor'] for x in valid);left=max(0,promise['amount_minor']-paid)
        late=sum(x['amount_minor'] for x in valid if effective_receipt(ledger,x['receipt_id']).date_received>date.fromisoformat(promise['due_date']))
        status=('cancelled' if promise['cancelled'] else 'needs_review' if stale else ('met_late' if late else 'met') if not left else
            'missed' if date.fromisoformat(promise['due_date'])<ledger.as_of else 'partially_observed' if paid else 'open')
        rows.append(dict(copy.deepcopy(promise),currency=rec.currency,counterparty=rec.counterparty,status=status,
            fulfilled_minor=paid,late_observed_minor=late,remaining_minor=left,stale_links=stale,
            held=collection_held(rec),invoice_remaining_minor=rec.remaining_minor))
    return rows


def create_promise(ledger,promise_id,invoice_id,amount,due,evidence_id,note,revision,*,ctx):
    payload=dict(promise_id=promise_id,invoice_id=invoice_id,amount_minor=amount,due_date=str(due),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'customer_promise_recorded',revision,payload)
    if old:return old
    _id(promise_id);_amount(amount);_day(due);_evidence(ledger,evidence_id);_text(note,'Observed customer promise source')
    if promise_id in ledger.payment_promises:raise IdempotencyConflict('Promise ID already exists. Cancel and record new source-backed terms; no overwrite.')
    rec=ledger.receivables.get(invoice_id)
    if rec is None or not rec.remaining_minor or collection_held(rec):raise CashflowError('Choose an outstanding unheld invoice; promises do not release disputes.')
    if len(ledger.payment_promises)>=200:raise CashflowError('This sandbox supports at most 200 recorded promises.')
    committed=sum(p['remaining_minor'] for p in promise_report(ledger,ctx=ctx) if p['invoice_id']==invoice_id and not p['cancelled'])
    if amount>max(0,rec.remaining_minor-committed):raise CashflowError('Active promises exceed the current unpromised invoice balance. Review/cancel older terms first.')
    if not ledger.as_of<=due<=ledger.as_of+timedelta(days=366):raise CashflowError('Promise date must be in the open snapshot and at most 366 days ahead.')
    ledger.payment_promises[promise_id]=dict(id=promise_id,invoice_id=invoice_id,amount_minor=amount,due_date=str(due),
        evidence_id=evidence_id,note=note.strip(),actor=ctx.actor,cancelled=False,links={},history=[])
    return _finish(ledger,key,'customer_promise_recorded',payload,dict(promise_id=promise_id),ctx)


def review_promise(ledger,promise_id,step,receipt_id,amount,evidence_id,note,revision,*,ctx):
    payload=dict(promise_id=promise_id,step=step,receipt_id=receipt_id,amount_minor=amount,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'customer_promise_'+str(step),revision,payload)
    if old:return old
    promise=ledger.payment_promises.get(promise_id)
    if promise is None or (promise['cancelled'] and step!='unlink'):raise CashflowError('Choose an active promise; cancelled terms cannot be revived. A mistaken old link can still be explicitly unlinked.')
    _amount(amount,True)
    _evidence(ledger,evidence_id);_text(note,'Promise source review')
    if step=='link':
        _amount(amount)
        if receipt_id not in ledger.receipts or receipt_id in promise['links']:raise CashflowError('Choose a recorded receipt/allocation not already linked to this promise.')
        receipt=effective_receipt(ledger,receipt_id)
        if receipt.state!='observed' or receipt.receivable_id!=promise['invoice_id'] or receipt.date_received>ledger.as_of:
            raise CashflowError('Link only an observed receipt/allocation for this invoice through the current business date. No receipt is invented.')
        fingerprint=receipt_fingerprint(ledger,receipt_id)
        used=sum(p['links'][receipt_id]['amount_minor'] for p in ledger.payment_promises.values() if receipt_id in p['links'] and
            p['links'][receipt_id]['fingerprint']==fingerprint)
        current=next(p for p in promise_report(ledger,ctx=ctx) if p['id']==promise_id)
        if amount>min(receipt.amount_minor-used,current['remaining_minor']):raise CashflowError('Linked amount exceeds unclaimed observed receipt capacity or the remaining promise.')
        promise['links'][receipt_id]=dict(amount_minor=amount,fingerprint=fingerprint,evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    elif step=='unlink':
        if receipt_id not in promise['links'] or amount!=0:raise CashflowError('Choose an existing link; unlink amount must be zero.')
        del promise['links'][receipt_id]
    elif step=='cancel':
        if receipt_id or amount!=0:raise CashflowError('Cancelling a promise cannot add a receipt or amount.')
        promise['cancelled']=True  # observed links remain reserved history, not new money
    else:raise CashflowError('Choose link observed receipt, unlink or cancel promise.')
    promise['history'].append(dict(step=step,receipt_id=receipt_id,amount_minor=amount,evidence_id=evidence_id,note=note.strip(),actor=ctx.actor))
    return _finish(ledger,key,'customer_promise_'+step,payload,dict(promise_id=promise_id),ctx)


def current_match_members(review,movements):
    if not review or review.get('status')!='matched':return set()
    if review.get('mode')=='net':
        fingerprints=review['fingerprints']
        return set(fingerprints) if all(k in movements and movements[k]['fingerprint']==fp for k,fp in fingerprints.items()) else set()
    ident=review.get('movement_key');row=movements.get(ident)
    return {ident} if row and row['fingerprint']==review.get('fingerprint') else set()


def ensure_match_available(ledger,keys,movements,except_row):
    for ident,review in ledger.statement_reviews.items():
        if ident!=except_row and set(keys)&current_match_members(review,movements):
            raise CashflowError('An observed movement is already in another current single/net statement match. Explicitly unmatch it before reusing.')


def review_net_settlement(ledger,batch_id,row_id,movement_keys,evidence_id,note,revision,*,ctx):
    payload=dict(batch_id=batch_id,row_id=row_id,movement_keys=movement_keys,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'net_statement_match_reviewed',revision,payload)
    if old:return old
    _evidence(ledger,evidence_id);_text(note,'Gross receipts, fee and net deposit source check')
    batch=ledger.statement_batches.get(batch_id)
    row=next((r for r in batch['rows'] if r['id']==row_id),None) if batch and batch['business_id']==ledger.business_id else None
    if row is None:raise CashflowError('Choose a statement row for the active business.')
    _currency(ledger,row['currency'])
    from .treasury import statement_account, currency_accounts, default_account
    scope = statement_account(ledger, batch_id)
    if not scope and len(currency_accounts(ledger, row['currency'])) > 1:
        raise CashflowError('Confirm the statement account scope before net matching.')
    if not isinstance(movement_keys,list) or not 2<=len(movement_keys)<=30 or any(not isinstance(k,str) for k in movement_keys) or len(set(movement_keys))!=len(movement_keys):
        raise CashflowError('Select 2–30 distinct existing observed movements, including a separately recorded fee if applicable.')
    from .payment_review import observed_movements
    movements={r['key']:r for r in observed_movements(ledger)};selected=[]
    for ident in movement_keys:
        m=movements.get(ident)
        if m and scope and m.get('account_id', default_account(ledger, m['currency'])) != scope:
            raise CashflowError('Every net settlement member must belong to the statement account.')
        if m is None or m['currency']!=row['currency'] or abs((date.fromisoformat(m['date'])-date.fromisoformat(row['date'])).days)>7:
            raise CashflowError('Every selected current observation must have the same currency and be within 7 days of the statement row.')
        selected.append(m)
    if sum(m['signed_minor'] for m in selected)!=row['signed_minor']:
        raise CashflowError('Selected signed receipts/payments/fees do not equal the exact statement net amount. No balancing cash is invented.')
    ident=_row_key(batch_id,row_id);ensure_match_available(ledger,movement_keys,movements,ident)
    ledger.statement_reviews[ident]=dict(batch_id=batch_id,row_id=row_id,status='matched',mode='net',movement_key='',fingerprint=None,
        fingerprints={m['key']:m['fingerprint'] for m in selected},signed_minor=row['signed_minor'],currency=row['currency'],
        evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'net_statement_match_reviewed',payload,dict(batch_id=batch_id,row_id=row_id,matched_count=len(selected)),ctx)


def unallocated_refund_total(ledger,payment_id):
    return sum(effective_refund(r) for r in ledger.unallocated_refunds.values() if r['payment_id']==payment_id)


def split_dependency_dates(ledger,parent,exclude_refund=None):
    days=[ledger.as_of,movement_date(ledger,'split_payment:'+parent['id'],parent['date'])]
    days += [ledger.receipts[r].date_received for r in parent['allocation_ids']]
    days += [date.fromisoformat(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==parent['id']]
    days += [date.fromisoformat(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
    for r in ledger.unallocated_refunds.values():
        if r['payment_id']==parent['id'] and r['id']!=exclude_refund:
            days.append(movement_date(ledger,'unallocated_refund:'+r['id'],r['date']))
            days += [date.fromisoformat(c['date']) for c in r['adjustments']]
    return days


def record_unallocated_refund(ledger,payment_id,refund_id,amount,day,evidence_id,note,revision,*,ctx,acknowledge_duplicates=False):
    payload=dict(payment_id=payment_id,refund_id=refund_id,amount_minor=amount,date=str(day),evidence_id=evidence_id,note=note,acknowledge_duplicates=acknowledge_duplicates)
    key,old=_begin(ledger,ctx,'unallocated_refund_observed',revision,payload)
    if old:return old
    _id(refund_id);_amount(amount);_day(day);_evidence(ledger,evidence_id);_text(note,'Observed return of unallocated funds source')
    parent=ledger.split_receipts.get(payment_id)
    if parent is None or parent['business_id']!=ledger.business_id:raise CashflowError('Choose an already-observed split parent in this business; an unconfirmed inbox row supplies no cash.')
    if refund_id in observation_ids(ledger):raise IdempotencyConflict('Observation ID already belongs to a payment/refund/cash path.')
    if amount>split_unallocated(ledger,parent):raise CashflowError('Refund exceeds the existing unallocated payment remainder. Allocated invoice money must use its own review workflow.')
    if not max(split_dependency_dates(ledger,parent))<=day<=ledger.as_of+timedelta(days=366):raise CashflowError('Refund must follow its parent/allocations/corrections and fall within the open snapshot and 366 days ahead.')
    from .payment_review import payment_candidates,require_ack,remember_ack,stamp_ack_fingerprint
    candidates=payment_candidates(ledger,parent['currency'],-amount,day);require_ack(candidates,acknowledge_duplicates)
    ledger.unallocated_refunds[refund_id]=dict(id=refund_id,payment_id=payment_id,currency=parent['currency'],amount_minor=amount,date=str(day),
        evidence_id=evidence_id,note=note.strip(),actor=ctx.actor,adjustments=[])
    remember_ack(ledger,'unallocated_refund',refund_id,candidates,ctx.actor,evidence_id);stamp_ack_fingerprint(ledger,'unallocated_refund',refund_id)
    return _finish(ledger,key,'unallocated_refund_observed',payload,dict(refund_id=refund_id,cash_delta_minor=-amount,
        unallocated_minor=split_unallocated(ledger,parent),possible_duplicate_ids=[c['key'] for c in candidates]),ctx)


def correct_unallocated_refund(ledger,refund_id,amount,day,evidence_id,note,revision,*,ctx):
    payload=dict(refund_id=refund_id,amount_minor=amount,date=str(day),evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'unallocated_refund_corrected',revision,payload)
    if old:return old
    r=ledger.unallocated_refunds.get(refund_id)
    if r is None:raise CashflowError('Choose an observed unallocated-payment refund.')
    _amount(amount,True);_day(day);_evidence(ledger,evidence_id);_text(note,'Refund amount correction source')
    parent=ledger.split_receipts[r['payment_id']];current=effective_refund(r)
    if amount==current or amount>current+split_unallocated(ledger,parent):raise CashflowError('Use a changed refund amount within the parent unallocated capacity; zero reverses a mistaken observation.')
    if day<max(split_dependency_dates(ledger,parent)):raise CashflowError('Correction cannot predate its source, opening or dependent allocation/refund correction.')
    change=dict(id='unallocated-refund-correction-'+key[:24],old_amount_minor=current,new_amount_minor=amount,
        cash_delta_minor=current-amount,date=str(day),evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    r['adjustments'].append(change)
    return _finish(ledger,key,'unallocated_refund_corrected',payload,dict(refund_id=refund_id,cash_delta_minor=current-amount,unallocated_minor=split_unallocated(ledger,parent)),ctx)


def party_statement(ledger,role,party,currency='',*,ctx):
    require_access(ctx,ledger,OP_READ)
    if role not in {'customer','supplier'} or not isinstance(party,str) or not party.strip() or len(party)>200:raise CashflowError('Choose customer/supplier and an exact recorded party label.')
    if currency:_currency(ledger,currency)
    group=ledger.receivables if role=='customer' else ledger.outflows
    selected={i.id:i for i in group.values() if i.counterparty==party and (not currency or i.currency==currency)}
    if not selected:raise CashflowError('No records match this exact party/role/currency; similar names are not merged automatically.')
    rows=[];summary={}
    def add(kind,ident,item,day,amount,evidence,note):
        rows.append(dict(kind=kind,id=ident,target_id=item.id,currency=item.currency,date=str(day) if day else None,amount_minor=amount,evidence_id=evidence,note=note))
    for item in selected.values():
        ccy=item.currency;s=summary.setdefault(ccy,dict(source_total_minor=0,net_settled_minor=0,remaining_minor=0,credit_or_overpaid_minor=0))
        s['source_total_minor']+=item.effective_total_minor;s['net_settled_minor']+=item.allocated_minor if role=='customer' else item.paid_minor
        s['remaining_minor']+=item.remaining_minor;s['credit_or_overpaid_minor']+=item.credit_balance_minor if role=='customer' else item.overpaid_minor
        add('invoice' if role=='customer' else 'bill',item.id,item,item.expected_date if role=='customer' else item.date,item.effective_total_minor,item.evidence_id,
            'Current source claim/obligation, not cash. External reference: '+item.invoice_reference)
        for c in item.credit_entries:
            add('credit_reversal' if c['reversal'] else 'credit_transfer',c['transfer_id'],item,c['date'],c['delta_minor'],c['evidence_id'],'Noncash credit settlement; not an observed receipt/payment.')
        for change in item.face_adjustments:add('source_adjustment',change['id'],item,change['date'],change['new_total_minor']-change['old_total_minor'],change['evidence_id'],change['note'])
        for refund in item.refunds:
            add('customer_refund' if role=='customer' else 'supplier_refund',refund['id'],item,movement_date(ledger,'refund:'+refund['id'],refund['date']),
                effective_refund(refund),refund['evidence_id'],'Effective observed refund; original date '+refund['date']+'. '+refund['note'])
            for c in refund['adjustments']:add('refund_correction',c['id'],item,c['date'],c['new_amount_minor']-c['old_amount_minor'],c['evidence_id'],c['note'])
        if role=='supplier':
            for original in bill_payment_entries(item):
                p=effective_bill_payment(item,original['id'],ledger)
                add('bill_payment',p['id'],item,p['date'],p['amount_minor'],p['evidence_id'],'Current effective observed payment; original date '+original['date'])
            for c in item.payment_adjustments:add('bill_payment_correction',c['id'],item,c['date'],c['new_amount_minor']-c['old_amount_minor'],c['evidence_id'],c['note'])
            for a in item.agreement_schedules:add('accepted_supplier_schedule',a['proposal_id'],item,None,0,a['evidence_id'],'; '.join(str(d)+': '+str(m)+' minor '+ccy for d,m in a['schedule']))
    if role=='customer':
        for rid in ledger.receipts:
            receipt=effective_receipt(ledger,rid)
            if receipt.state!='observed' or receipt.receivable_id not in selected:continue
            parent=split_parent(ledger,rid);item=selected[receipt.receivable_id]
            add('split_allocation' if parent else 'receipt',rid,item,receipt.date_received,receipt.amount_minor,receipt.evidence_id,
                ('Allocation from parent '+parent['id']+'; supplies no additional cash.' if parent else 'Current effective observed receipt.')+' Original date '+str(ledger.receipts[rid].date_received))
        for c in ledger.receipt_adjustments.values():
            for side,sign in [('old_invoice_id',-1),('new_invoice_id',1)]:
                if c[side] in selected:
                    add('allocation_correction',c['id']+('-remove' if sign<0 else '-apply'),selected[c[side]],c['date'],
                        sign*c['old_amount_minor' if sign<0 else 'new_amount_minor'],c['evidence_id'],c['note'])
    rows.sort(key=lambda r:(r['date'] or '',r['kind'],r['id']))
    return dict(party=party,role=role,as_of=str(ledger.as_of),summary=summary,rows=rows,
        note='Current recorded settlement statement grouped by exact party label/role, not independent identity verification or a historical general ledger. '
             'Summary balances are separate per currency and keep remaining claims/obligations separate from credits. Rows include effective observations plus correction history; do not sum them as another cash stream. Split allocations are not additional cash. No automatic customer/supplier or FX netting.')


def party_csv(report):
    from .reports import csv_text,Money
    buf=StringIO(newline='');w=csv.writer(buf)
    w.writerow(['Party','Role','Business date','Section','Kind','ID','Target','Currency','Date','Amount','Evidence','Note'])
    for ccy,s in report['summary'].items():
        for field,amount in s.items():w.writerow([csv_text(report['party']),report['role'],report['as_of'],'current_summary',field,'','',ccy,'',csv_text(Money(amount,ccy)),'',csv_text(report['note'])])
    for r in report['rows']:w.writerow([csv_text(report['party']),report['role'],report['as_of'],'record_and_history',r['kind'],csv_text(r['id']),csv_text(r['target_id']),r['currency'],r['date'] or '',csv_text(Money(r['amount_minor'],r['currency'])),csv_text(r['evidence_id']),csv_text(r['note'])])
    return '\ufeff'+buf.getvalue()


FILE_TYPES={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.txt':'text/plain','.csv':'text/csv'}


def validate_attachment(filename,data):
    if not isinstance(filename,str) or not 1<=len(filename)<=120 or re.search(r'[\\/:\x00-\x1f\x7f]',filename) or filename in {'.','..'}:raise CashflowError('Use an ordinary filename without paths/control characters, up to 120 characters.')
    ext='.'+filename.rsplit('.',1)[-1].lower()
    if ext not in FILE_TYPES or not isinstance(data,bytes) or not 1<=len(data)<=1024*1024:raise CashflowError('Attach a nonempty PDF, PNG, JPEG, UTF-8 TXT or CSV, at most 1 MiB.')
    valid=(data.startswith(b'%PDF-') if ext=='.pdf' else data.startswith(b'\x89PNG\r\n\x1a\n') if ext=='.png' else data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9') if ext in {'.jpg','.jpeg'} else True)
    if not valid:raise CashflowError('File signature does not match the selected file type.')
    if ext in {'.txt','.csv'}:
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:raise CashflowError('Text/CSV attachments must be UTF-8.')
        if any(ord(c)<32 and c not in '\t\r\n' for c in text):raise CashflowError('Text attachment contains binary/control data.')
    return dict(filename=filename,media_type=FILE_TYPES[ext],bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),extension=ext)


def attachment_inventory(ledger,*,ctx):
    require_access(ctx,ledger,OP_READ)
    return [{k:v for k,v in a.items() if k!='payload_base64'} for a in ledger.evidence_attachments.values()]


def attach_evidence(ledger,evidence_id,filename,data,note,revision,*,ctx):
    require_access(ctx,ledger)
    metadata=validate_attachment(filename,data)
    payload=dict(evidence_id=evidence_id,**metadata,note=note)
    key,old=_begin(ledger,ctx,'original_evidence_attached',revision,payload)
    if old:return old
    _evidence(ledger,evidence_id);_text(note,'Attachment source review')
    ident='attachment-'+_hash([ledger.business_id,evidence_id,metadata['sha256'],filename])[:24]
    if ident in ledger.evidence_attachments:raise IdempotencyConflict('This attachment already has a record, including any retired history. No duplicate/revival was created.')
    if len(ledger.evidence_attachments)>=32 or sum(a['bytes'] for a in ledger.evidence_attachments.values() if a['active'])+len(data)>8*1024*1024:
        raise CashflowError('Evidence storage supports at most 32 attachment records and 8 MiB of active original bytes.')
    ledger.evidence_attachments[ident]=dict(id=ident,business_id=ledger.business_id,evidence_id=evidence_id,**metadata,
        note=note.strip(),actor=ctx.actor,active=True,payload_base64=base64.b64encode(data).decode('ascii'))
    return _finish(ledger,key,'original_evidence_attached',payload,dict(attachment_id=ident),ctx)


def attachment_bytes(ledger,attachment_id,*,ctx):
    require_access(ctx,ledger,OP_READ)
    item=ledger.evidence_attachments.get(attachment_id)
    if item is None or item['business_id']!=ledger.business_id or not item['active']:raise CashflowError('Choose an active evidence attachment in this business.')
    try:data=base64.b64decode(item['payload_base64'],validate=True)
    except (ValueError,TypeError):raise CashflowError('Stored evidence bytes are invalid; restore/review the source.')
    if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise CashflowError('Attachment checksum/length mismatch. No file was returned.')
    return data,{k:v for k,v in item.items() if k!='payload_base64'}


def retire_attachment(ledger,attachment_id,note,revision,*,ctx):
    payload=dict(attachment_id=attachment_id,note=note)
    key,old=_begin(ledger,ctx,'original_evidence_retired',revision,payload)
    if old:return old
    item=ledger.evidence_attachments.get(attachment_id);_text(note,'Attachment retirement reason')
    if item is None or item['business_id']!=ledger.business_id or not item['active']:raise CashflowError('Choose an active local attachment.')
    item['active']=False;item['payload_base64']=None;item['retired_by']=ctx.actor;item['retirement_note']=note.strip()
    return _finish(ledger,key,'original_evidence_retired',payload,dict(attachment_id=attachment_id),ctx)
