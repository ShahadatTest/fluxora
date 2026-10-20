"""Evidence-backed collection dates, bounded recurrence and duplicate review.

Owner controls only. Hypothetical collections never become observed cash.
"""
import calendar
import copy
from dataclasses import replace
from datetime import date, timedelta
import re
import unicodedata

from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .forecast import forecast
from .models import Evidence, Outflow
from .owner_workflows import _begin, _finish, _evidence, _text, review_revision
from .planning import _hash
from .settlement import collection_held


def item_identity(ledger, kind, item_id, counterparty, invoice_reference, source, note, revision, *, ctx,
                  due_date=None):
    payload=dict(kind=kind,item_id=item_id,counterparty=counterparty,invoice_reference=invoice_reference,
        due_date=str(due_date) if due_date else None,evidence_id=source,note=note)
    key,old=_begin(ledger,ctx,'item_identity_confirmed',revision,payload)
    if old:return old
    collection=ledger.receivables if kind=='invoice' else ledger.outflows if kind=='bill' else {}
    item=collection.get(item_id)
    if item is None:raise CashflowError('Choose an invoice or bill in this business.')
    for value in [counterparty,invoice_reference]:
        if not isinstance(value,str) or len(value)>200:raise CashflowError('Party/reference must be text up to 200 characters.')
    if due_date is not None and (kind!='invoice' or type(due_date) is not date):
        raise CashflowError('Contractual due date must be an invoice calendar date, or unknown.')
    _evidence(ledger,source);_text(note,'Source review note')
    payload['previous']=dict(counterparty=item.counterparty,invoice_reference=item.invoice_reference,
        due_date=item.due_date.isoformat() if kind=='invoice' and item.due_date else None)
    item.counterparty,item.invoice_reference=counterparty.strip(),invoice_reference.strip()
    eid='ev-record-source-'+key[:24]
    ledger.evidence[eid]=Evidence(eid,'owner_confirmed_record_source',
        f'Owner confirmed {kind} {item.id} party {item.counterparty or "unknown"}, reference {item.invoice_reference or "unknown"}, '
        f'contractual due {due_date or "unknown/not applicable"}; source {source}; note {note.strip()}. '
        'Source metadata only; no payment, consent or legal determination.')
    item.identity_evidence_id=eid
    if kind=='invoice':
        item.due_date=due_date
        ledger.invoice_due_sources[item.id]=eid
    return _finish(ledger,key,'item_identity_confirmed',payload,dict(item_id=item_id,kind=kind),ctx)


def collection_priorities(ledger, *, ctx):
    require_access(ctx,ledger,OP_READ)
    if len(ledger.receivables)>300 or not 1<=ledger.horizon_days<=120:
        raise CashflowError('Collection comparison supports at most 300 invoices and a 1–120 day horizon.')
    baseline=forecast(ledger,ledger.as_of,ledger.horizon_days)
    missing={r.currency for r in ledger.receivables.values() if r.remaining_minor>0}-set(baseline.currencies)
    if missing:raise CashflowError('Collection cash impact needs a separate opening account for: '+', '.join(sorted(missing)))
    groups={ccy:dict(buckets={k:0 for k in ['not_due','1_30','31_60','61_90','over_90','unknown']},invoices=[]) for ccy in baseline.currencies}
    for rec in ledger.receivables.values():
        if rec.remaining_minor<=0:continue
        days=(ledger.as_of-rec.due_date).days if rec.due_date else None
        bucket=('unknown' if days is None else 'not_due' if days<=0 else '1_30' if days<=30 else
            '31_60' if days<=60 else '61_90' if days<=90 else 'over_90')
        if rec.currency not in groups:continue
        work=copy.copy(ledger);work.receivables=dict(ledger.receivables)
        work.receivables[rec.id]=replace(rec,expected_date=ledger.as_of)
        conditional=forecast(work,ledger.as_of,ledger.horizon_days).currencies[rec.currency]
        base=baseline.currencies[rec.currency]
        row=dict(id=rec.id,currency=rec.currency,counterparty=rec.counterparty,invoice_reference=rec.invoice_reference,
            collection_held=collection_held(rec),hold_evidence_id=rec.collection_hold['evidence_id'] if rec.collection_hold else None,
            remaining_minor=rec.remaining_minor,due_date=rec.due_date.isoformat() if rec.due_date else None,
            expected_date=rec.expected_date.isoformat() if rec.expected_date else None,
            overdue_days=max(0,days) if days is not None else None,bucket=bucket,evidence_id=rec.evidence_id,
            due_evidence_id=ledger.invoice_due_sources.get(rec.id,rec.evidence_id),
            baseline_shortfall_minor=base.worst_shortfall_minor,conditional_shortfall_minor=conditional.worst_shortfall_minor,
            gap_reduction_minor=max(0,base.worst_shortfall_minor-conditional.worst_shortfall_minor))
        groups[rec.currency]['buckets'][bucket]+=rec.remaining_minor
        groups[rec.currency]['invoices'].append(row)
    for group in groups.values():
        group['invoices'].sort(key=lambda r:(-r['gap_reduction_minor'],-(r['overdue_days'] or 0),-r['remaining_minor'],r['id']))
    return dict(as_of=ledger.as_of.isoformat(),currencies=groups,
        assumption='Each invoice is tested separately as if its entire remaining balance were collected on the ledger snapshot date. '
            'Outflows precede same-day inflows. This conditional ranking is not a collection promise, a combined plan, a probability or observed cash. '
            'Ageing uses contractual due date only; expected collection date never substitutes for it.')


def _normal(value):return ' '.join(unicodedata.normalize('NFKC',value or '').casefold().split())


def descriptor(kind,item):
    return dict(kind=kind,id=item.id,currency=item.currency,
        amount_minor=item.original_minor if kind=='invoice' else item.amount_minor,
        date=(item.expected_date.isoformat() if item.expected_date else None) if kind=='invoice' else item.date.isoformat(),
        counterparty=item.counterparty,invoice_reference=item.invoice_reference,evidence_id=item.evidence_id,
        identity_evidence_id=item.identity_evidence_id or item.evidence_id)


def duplicate_reason(a,b):
    if a['kind']!=b['kind'] or a['currency']!=b['currency'] or a['id']==b['id']:return None
    pa,pb=_normal(a.get('counterparty')),_normal(b.get('counterparty'))
    ra,rb=_normal(a.get('invoice_reference')),_normal(b.get('invoice_reference'))
    if pa and pa==pb and ra and ra==rb:
        return 'Same normalized party and external invoice reference; amounts/dates may conflict.'
    if pa and pb and pa!=pb:return None
    if a['amount_minor']==b['amount_minor'] and a.get('date') and a['date']==b.get('date'):
        return 'Same amount, currency and recorded expected/bill date, with same or unknown party. This can be legitimate.'
    return None


def duplicate_pairs(ledger, *, ctx):
    require_access(ctx,ledger,OP_READ)
    rows=[descriptor(kind,item) for kind,collection in [('invoice',ledger.receivables),('bill',ledger.outflows)] for item in collection.values()]
    if len(rows)>600:raise CashflowError('Duplicate review supports at most 600 invoice/bill records.')
    result=[];rows.sort(key=lambda r:(r['kind'],r['id']))
    for index,a in enumerate(rows):
        for b in rows[index+1:]:
            reason=duplicate_reason(a,b)
            if not reason:continue
            pair_id=_hash([ledger.business_id,a,b])
            result.append(dict(id=pair_id,kind=a['kind'],left=a,right=b,reason=reason,
                review=ledger.duplicate_reviews.get(pair_id)))
            if len(result)>1000:raise CashflowError('Too many possible duplicate pairs; review and correct the source dataset in smaller groups.')
    return result


def review_duplicate(ledger,pair_id,verdict,source,note,revision,*,ctx):
    payload=dict(pair_id=pair_id,verdict=verdict,evidence_id=source,note=note)
    key,old=_begin(ledger,ctx,'duplicate_pair_reviewed',revision,payload)
    if old:return old
    pair=next((p for p in duplicate_pairs(ledger,ctx=ctx) if p['id']==pair_id),None)
    if pair is None or verdict not in {'distinct','confirmed_duplicate'}:
        raise CashflowError('Review a current pair and choose distinct or confirmed duplicate.')
    _evidence(ledger,source);_text(note,'Duplicate review reason')
    payload['previous_review']=ledger.duplicate_reviews.get(pair_id)
    ledger.duplicate_reviews[pair_id]=dict(verdict=verdict,evidence_id=source,note=note.strip(),actor=ctx.actor,
        left=pair['left'],right=pair['right'])
    return _finish(ledger,key,'duplicate_pair_reviewed',payload,dict(pair_id=pair_id,verdict=verdict),ctx)


def csv_duplicate_warnings(ledger,records):
    """Compare imported invoice/bill rows against current records and earlier preview rows."""
    known=[descriptor(kind,item) for kind,collection in [('invoice',ledger.receivables),('bill',ledger.outflows)] for item in collection.values()]
    from .payment_review import observed_movements
    movements=list(observed_movements(ledger))
    for row in records:
        row['possible_duplicates']=[]
        if row['record_type']=='receipt' and not row['will_skip']:
            row['possible_duplicates']=[dict(id=m['key'],reason='Same signed observed amount, currency and date; owner must verify distinct source movements.')
                for m in movements if m['id']!=row['reference'] and m['currency']==row['currency'] and m['signed_minor']==row['amount_minor'] and m['date']==row['date']]
            movements.append(dict(key='preview-receipt:'+row['reference'],id=row['reference'],currency=row['currency'],signed_minor=row['amount_minor'],date=row['date']))
            continue
        if row['record_type'] not in {'invoice','bill'} or row['will_skip']:continue
        proposed=dict(kind=row['record_type'],id=row['reference'],currency=row['currency'],amount_minor=row['amount_minor'],
            date=row['date'],counterparty=row['counterparty'],invoice_reference=row.get('invoice_reference',''),evidence_id='preview')
        row['possible_duplicates']=[dict(id=old['id'],reason=duplicate_reason(proposed,old)) for old in known if duplicate_reason(proposed,old)]
        known.append(proposed)
    return records


def create_recurring(ledger,template_id,label,currency,amount_minor,start,end,frequency,protected,consent_required,
                     counterparty,source,note,revision,*,ctx):
    payload=dict(id=template_id,label=label,currency=currency,amount_minor=amount_minor,start=str(start),end=str(end) if end else None,
        frequency=frequency,protected=protected,consent_required=consent_required,counterparty=counterparty,evidence_id=source,note=note)
    key,old=_begin(ledger,ctx,'recurring_template_created',revision,payload)
    if old:return old
    if not isinstance(template_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',template_id):
        raise CashflowError('Template ID needs 1–64 letters, digits, dashes or underscores.')
    if template_id in ledger.recurring_bills:raise IdempotencyConflict('Template ID already exists; reuse its schedule or pause it.')
    _text(label,'Template label');_text(note,'Template source note');_evidence(ledger,source)
    if len(label)>120 or len(ledger.recurring_bills)>=100:raise CashflowError('Use a label up to 120 characters and at most 100 templates.')
    if not isinstance(counterparty,str) or len(counterparty)>200:raise CashflowError('Counterparty must be text up to 200 characters.')
    if not any(a.currency==currency for a in ledger.accounts.values()):raise CashflowError('Choose a currency with a separate opening account.')
    if type(amount_minor) is not int or amount_minor<=0:raise CashflowError('Recurring amount must be positive integer minor units.')
    if type(start) is not date or not 0<=(start-ledger.as_of).days<=366:
        raise CashflowError('First occurrence must be on/after this snapshot and within 366 days.')
    if end is not None and (type(end) is not date or end<start):raise CashflowError('End date must be on/after the first occurrence, or blank.')
    if frequency not in {'weekly','monthly'} or type(protected) is not bool or type(consent_required) is not bool:
        raise CashflowError('Choose weekly/monthly frequency and explicit boolean protection/consent flags.')
    ledger.recurring_bills[template_id]=dict(payload,active=True)
    return _finish(ledger,key,'recurring_template_created',payload,dict(template_id=template_id),ctx)


def pause_recurring(ledger,template_id,active,revision,*,ctx):
    payload=dict(template_id=template_id,active=active)
    key,old=_begin(ledger,ctx,'recurring_status_changed',revision,payload)
    if old:return old
    template=ledger.recurring_bills.get(template_id)
    if template is None or type(active) is not bool:raise CashflowError('Choose a current template and explicit active status.')
    template['active']=active
    return _finish(ledger,key,'recurring_status_changed',payload,payload,ctx)


def recurring_preview(ledger,template_id,through,*,ctx):
    require_access(ctx,ledger,OP_READ)
    template=ledger.recurring_bills.get(template_id)
    if template is None:raise CashflowError('Choose a recurring template for this business.')
    if type(through) is not date or not 0<=(through-ledger.as_of).days<=366:
        raise CashflowError('Preview through a date on/after this snapshot and at most 366 days ahead.')
    start=date.fromisoformat(template['start']);end=min(through,date.fromisoformat(template['end'])) if template['end'] else through
    if template['frequency']=='weekly':
        offset=max(0,((ledger.as_of-start).days+6)//7)
        def occurrence(i):return start+timedelta(days=i*7)
    else:
        offset=max(0,(ledger.as_of.year-start.year)*12+ledger.as_of.month-start.month-1)
        def occurrence(i):
            total=start.year*12+start.month-1+i;year,month=divmod(total,12);month+=1
            return date(year,month,min(start.day,calendar.monthrange(year,month)[1]))
    rows=[]
    existing=[descriptor('bill',o) for o in ledger.outflows.values()]
    if template['active']:
        for index in range(offset,offset+60):
            day=occurrence(index)
            if day>end:break
            if day<ledger.as_of:continue
            oid='repeat-'+_hash([ledger.business_id,template_id,day.isoformat()])[:24]
            marker=ledger.recurring_occurrences.get(oid)
            if marker and oid not in ledger.outflows:raise IdempotencyConflict('A generated occurrence is missing its bill; inspect source/recovery before continuing.')
            if oid in ledger.outflows and not marker:raise IdempotencyConflict('Generated bill ID collides with another record; nothing was overwritten.')
            if marker and (marker['template_id']!=template_id or marker['date']!=day.isoformat()):
                raise IdempotencyConflict('Conflicting recurring occurrence identity.')
            proposed=dict(kind='bill',id=oid,currency=template['currency'],amount_minor=template['amount_minor'],date=day.isoformat(),
                counterparty=template['counterparty'],invoice_reference=template_id+'@'+day.isoformat(),evidence_id=template['evidence_id'])
            warnings=[dict(id=o['id'],reason=duplicate_reason(proposed,o)) for o in existing if duplicate_reason(proposed,o)] if not marker else []
            rows.append(dict(proposed,already_created=bool(marker),duplicate_warnings=warnings))
    return dict(template=copy.deepcopy(template),through=through.isoformat(),revision=review_revision(ledger),rows=rows,
        note='Only explicitly confirmed new occurrences become scheduled bills. No observed payment or consent is created. '
            'Past ungenerated occurrences stay omitted and require source review; they are not shifted into today. '
            'Monthly dates use the original anchor day, clamped to that month, then recover the anchor in later months.')


def confirm_recurring(ledger,template_id,through,acknowledge_duplicates,revision,*,ctx):
    payload=dict(template_id=template_id,through=str(through),acknowledge_duplicates=acknowledge_duplicates)
    key,old=_begin(ledger,ctx,'recurring_occurrences_confirmed',revision,payload)
    if old:return old
    preview=recurring_preview(ledger,template_id,through,ctx=ctx);template=preview['template']
    if not template['active']:raise CashflowError('Paused templates cannot generate new bills.')
    _evidence(ledger,template['evidence_id'])
    new=[r for r in preview['rows'] if not r['already_created']]
    if any(r['duplicate_warnings'] for r in new) and acknowledge_duplicates is not True:
        raise CashflowError('Review possible existing bills and explicitly acknowledge duplicate warnings before creating occurrences.')
    if len(ledger.outflows)+len(ledger.receivables)+len(new)>300:raise CashflowError('Confirmed records would exceed the planner limit of 300 invoices/bills.')
    for row in new:
        ledger.outflows[row['id']]=Outflow(row['id'],row['currency'],row['amount_minor'],date.fromisoformat(row['date']),
            template['protected'],row['evidence_id'],template['consent_required'],counterparty=row['counterparty'],invoice_reference=row['invoice_reference'])
        ledger.recurring_occurrences[row['id']]=dict(template_id=template_id,date=row['date'],outflow_id=row['id'],terms_hash=_hash(row))
    return _finish(ledger,key,'recurring_occurrences_confirmed',payload,
        dict(template_id=template_id,created_ids=[r['id'] for r in new],existing_count=len(preview['rows'])-len(new)),ctx)
