"""Current interpretations of observed movements; never a second cash stream."""
from datetime import date

from .authz import OP_READ, require_access
from .errors import CashflowError
from .planning import _hash
from .settlement import effective_receipt, effective_split_payment, effective_bill_payment, bill_payment_entries


def observed_movements(ledger):
    rows=[]
    def add(kind,ident,ccy,minor,day,evidence,target,history):
        if not minor:return
        key=kind+':'+ident
        row=dict(key=key,kind=kind,id=ident,currency=ccy,signed_minor=minor,date=str(day),evidence_id=evidence,target=target)
        from .extension import movement_date
        row['date']=str(movement_date(ledger,key,day))
        from .treasury import default_account, currency_accounts
        binding = ledger.movement_accounts.get(key)
        if binding or len(currency_accounts(ledger, ccy)) > 1:
            row['account_id'] = binding['account_id'] if binding else default_account(ledger, ccy)
        date_history=[c for c in ledger.date_corrections.values() if c['movement_key']==key]
        parts=[ledger.business_id,row,history]
        if date_history:parts.append(date_history)
        row['fingerprint']=_hash(parts);rows.append(row)
    children={rid for p in ledger.split_receipts.values() for rid in p['allocation_ids']}
    for original in ledger.receipts.values():
        if original.id in children or original.state!='observed':continue
        current=effective_receipt(ledger,original.id)
        add('receipt',current.id,ledger.receivables[current.receivable_id].currency,current.amount_minor,
            current.date_received,current.evidence_id,current.receivable_id,
            [c for c in ledger.receipt_adjustments.values() if c['receipt_id']==current.id])
    for original in ledger.split_receipts.values():
        current=effective_split_payment(ledger,original)
        add('split_payment',current['id'],current['currency'],current['amount_minor'],current['date'],
            current['evidence_id'],current['id'],[c for c in ledger.split_adjustments.values() if c['payment_id']==current['id']])
    for out in ledger.outflows.values():
        for original in bill_payment_entries(out):
            current=effective_bill_payment(out,original['id'],ledger)
            add('bill_payment',current['id'],out.currency,-current['amount_minor'],current['date'],current['evidence_id'],out.id,
                [c for c in out.payment_adjustments if c['payment_id']==current['id']])
    for item in ledger.cash_movements.values():
        amount=item['adjustments'][-1]['new_signed_minor'] if item['adjustments'] else item['signed_minor']
        source=item['adjustments'][-1]['evidence_id'] if item['adjustments'] else item['evidence_id']
        add('other_cash',item['id'],item['currency'],amount,item['date'],source,item['classification'],item['adjustments'])
    from .extension import refund_entries,effective_refund
    for kind,item,entry in refund_entries(ledger):
        add('refund',entry['id'],item.currency,(-1 if kind=='invoice' else 1)*effective_refund(entry),
            entry['date'],entry['adjustments'][-1]['evidence_id'] if entry['adjustments'] else entry['evidence_id'],item.id,entry['adjustments'])
    for r in ledger.unallocated_refunds.values():
        add('unallocated_refund',r['id'],r['currency'],-effective_refund(r),r['date'],
            r['adjustments'][-1]['evidence_id'] if r['adjustments'] else r['evidence_id'],r['payment_id'],r['adjustments'])
    from .treasury import transfer_events
    for e in transfer_events(ledger):
        row = dict(key=e['kind']+':'+e['id'], kind=e['kind'], id=e['id'], currency=e['currency'],
            signed_minor=e['amount_minor'], date=str(e['date']), evidence_id=e['evidence_id'],
            target=e['transfer_id'], account_id=e['account_id'])
        row['fingerprint'] = _hash([ledger.business_id, row]); rows.append(row)
    if len(rows)>2000:raise CashflowError('Movement review supports at most 2,000 current nonzero observations; no rows were silently dropped.')
    return rows


def payment_candidates(ledger,currency,signed_minor,day):
    return [r for r in observed_movements(ledger) if r['currency']==currency and r['signed_minor']==signed_minor and r['date']==str(day)]


def require_ack(candidates,acknowledged):
    if candidates and acknowledged is not True:
        raise CashflowError('Possible duplicate observed payment: '+', '.join(r['key'] for r in candidates)+
            '. Check the source and explicitly acknowledge distinct movements before recording. See Payment Warnings. No merge/deletion occurred.')


def remember_ack(ledger,kind,ident,candidates,actor,evidence_id):
    if candidates:
        ledger.payment_acknowledgements[kind+':'+ident]=dict(actor=actor,evidence_id=evidence_id,
            candidates={r['key']:r['fingerprint'] for r in candidates},
            note='Explicit owner acknowledgment that these possible duplicate observations represent distinct movements; not independent verification.')


def payment_warnings(ledger,*,ctx):
    require_access(ctx,ledger,OP_READ)
    rows=sorted(observed_movements(ledger),key=lambda r:r['key']);pairs=[]
    groups={}
    for row in rows:groups.setdefault((row['currency'],row['signed_minor'],row['date']),[]).append(row)
    for group in groups.values():
        for i,a in enumerate(group):
            for b in group[i+1:]:
                ident=_hash([ledger.business_id,a['fingerprint'],b['fingerprint']])
                review=ledger.payment_pair_reviews.get(ident)
                acknowledged=False
                for left,right in [(a,b),(b,a)]:
                    old=ledger.payment_acknowledgements.get(left['key'],{})
                    if old.get('candidates',{}).get(right['key'])==right['fingerprint']:
                        # A correction of the newly recorded movement invalidates its original acknowledgment too.
                        if not old.get('self_fingerprint') or old['self_fingerprint']==left['fingerprint']:
                            acknowledged=True
                status=review['verdict'] if review else 'distinct_acknowledged' if acknowledged else 'review_required'
                pairs.append(dict(id=ident,left=a,right=b,status=status,review=review))
                if len(pairs)>1000:raise CashflowError('Payment warning review supports at most 1,000 pairs. Review a smaller dataset; no warning was silently dropped.')
    return pairs


def stamp_ack_fingerprint(ledger,kind,ident):
    item=ledger.payment_acknowledgements.get(kind+':'+ident)
    if item:
        row=next((r for r in observed_movements(ledger) if r['key']==kind+':'+ident),None)
        if row:item['self_fingerprint']=row['fingerprint']


def review_payment_pair(ledger,pair_id,verdict,evidence_id,note,revision,*,ctx):
    from .owner_workflows import _begin,_finish,_evidence,_text
    payload=dict(pair_id=pair_id,verdict=verdict,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'payment_pair_reviewed',revision,payload)
    if old:return old
    pair=next((p for p in payment_warnings(ledger,ctx=ctx) if p['id']==pair_id),None)
    if pair is None or verdict not in {'distinct','possible_duplicate'}:raise CashflowError('Review a current movement pair as distinct or possible duplicate.')
    _evidence(ledger,evidence_id);_text(note,'Payment source review')
    ledger.payment_pair_reviews[pair_id]=dict(verdict=verdict,evidence_id=evidence_id,note=note.strip(),actor=ctx.actor)
    return _finish(ledger,key,'payment_pair_reviewed',payload,dict(pair_id=pair_id,verdict=verdict),ctx)
