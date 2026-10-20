"""Deterministic, business-scoped search, compound scenarios and cash calendar."""
from __future__ import annotations

import copy
from datetime import date, timedelta
from urllib.parse import quote

from .authz import OP_READ, require_access
from .completion import _amount, _currency
from .errors import CashflowError
from .extension import refund_entries, effective_refund, movement_date
from .forecast import forecast
from .models import Outflow
from .owner_workflows import _begin, _finish, _text
from .planning import _hash, _revision
from .settlement import bill_schedule, cash_events, collection_held, bill_payment_entries


def compound_scenario(ledger, delays, expenses, *, ctx, collections=None):
    require_access(ctx, ledger, OP_READ)
    collections = [] if collections is None else collections
    if any(not isinstance(v,list) or len(v)>20 for v in (delays,expenses,collections)) or not (delays or expenses or collections):
        raise CashflowError('Use 1â€“20 invoice delays, collection assumptions and/or hypothetical expenses per category.')
    work=copy.deepcopy(ledger); seen=set(); sources=set(); assumptions=[]
    for change in delays:
        if not isinstance(change,dict) or set(change)!={'invoice_id','delay_days'}:
            raise CashflowError('Use invoice ID and whole delay days only.')
        ident, days=change['invoice_id'],change['delay_days']
        if not isinstance(ident,str) or ident in seen: raise CashflowError('Choose each invoice once.')
        seen.add(ident); rec=work.receivables.get(ident)
        if rec is None or rec.remaining_minor<=0 or rec.expected_date is None or collection_held(rec):
            raise CashflowError('Delay only an outstanding, unheld invoice with a recorded expected date.')
        if type(days) is not int or not 0<=days<=90: raise CashflowError('Each assumed delay must be 0â€“90 whole days.')
        rec.expected_date+=timedelta(days=days); sources.add(rec.evidence_id)
        assumptions.append(f'{ident}: assumes collection {days} days later, on {rec.expected_date}; remaining {rec.remaining_minor} {rec.currency} minor units.')
        if rec.expected_date>=ledger.as_of+timedelta(days=ledger.horizon_days):
            assumptions.append(f'{ident}: assumed collection falls outside this forecast window and supplies no cash inside it.')
    limits={}; collection_rows=[]
    for change in collections:
        if not isinstance(change,dict) or set(change)!={'invoice_id','percent'}:
            raise CashflowError('Use invoice ID and whole collection percent only.')
        ident,percent=change['invoice_id'],change['percent']
        if not isinstance(ident,str) or ident in limits:raise CashflowError('Choose each collection invoice once.')
        rec=work.receivables.get(ident)
        if rec is None or rec.remaining_minor<=0 or collection_held(rec) or rec.expected_date is None or rec.expected_date<ledger.as_of:
            raise CashflowError('Use an outstanding, unheld invoice with a current/future expected date (after any assumed delay).')
        if type(percent) is not int or not 0<=percent<=100:raise CashflowError('Collection percent must be a whole number from 0 to 100.')
        minor=rec.remaining_minor*percent//100
        limits[ident]=minor
        refs={rec.evidence_id,ledger.collection_date_source(ident)}
        refs.update(c['evidence_id'] for c in rec.face_adjustments)
        sources.update(refs)
        collection_rows.append(dict(invoice_id=ident,currency=rec.currency,percent=percent,
            remaining_minor=rec.remaining_minor,assumed_minor=minor,excluded_minor=rec.remaining_minor-minor,
            expected_date=str(rec.expected_date),evidence_ids=sorted(refs)))
        assumptions.append(f'{ident}: assumes {percent}% of the CURRENT unpaid remainder ({minor} {rec.currency} minor units), rounded down to a minor unit, on {rec.expected_date}. The excluded remainder supplies no later cash in this scenario; it stays owed in the ledger. This percentage is a stress assumption, not a probability.')
    for n,expense in enumerate(expenses):
        if not isinstance(expense,dict) or set(expense)!={'label','currency','amount_minor','date'}:
            raise CashflowError('Use hypothetical expense label, currency, amount and date only.')
        _text(expense['label'],'Expense label')
        if len(expense['label'])>120: raise CashflowError('Expense label must be at most 120 characters.')
        _currency(ledger,expense['currency']); _amount(expense['amount_minor'])
        try: day=date.fromisoformat(expense['date'])
        except (TypeError,ValueError): raise CashflowError('Use a valid hypothetical expense date.')
        if not ledger.as_of<=day<ledger.as_of+timedelta(days=ledger.horizon_days):
            raise CashflowError('Hypothetical expense date must fall inside the current forecast window.')
        ident=f'__scenario_expense_{n}'
        while ident in work.outflows: ident+='x'
        work.outflows[ident]=Outflow(ident,expense['currency'],expense['amount_minor'],day,True,'hypothetical-expense')
        assumptions.append(f"Hypothetical expense {expense['label']}: {expense['amount_minor']} {expense['currency']} minor on {day}; no purchase or payment recorded.")
    baseline=forecast(ledger,ledger.as_of,ledger.horizon_days)
    scenario=forecast(work,work.as_of,work.horizon_days,collection_limits=limits)
    problems=[]
    from .adaptive import input_problems
    problems=input_problems(ledger)
    sufficient=all(c.first_breach_date is None for c in scenario.currencies.values())
    return dict(baseline=baseline,scenario=scenario,delays=copy.deepcopy(delays),expenses=copy.deepcopy(expenses),collections=copy.deepcopy(collections),collection_rows=collection_rows,
        assumptions=assumptions,evidence_ids=sorted(sources),input_problems=problems,
        floors_hold=sufficient,financially_sufficient=sufficient and not problems,
        conclusion=('No feasible cash-floor outcome under these assumptions.' if not sufficient else
            'Cash floors hold arithmetically, but unresolved source/input issues prevent a feasibility claim.' if problems else
            'Cash floors hold under these assumptions; payment timing and new expenses remain hypothetical.'),
        note='Read-only conditional calculation. It creates no draft, approval, consent, invoice, receipt or cash movement. No FX conversion or currency netting.')


def save_scenario(ledger,label,delays,expenses,revision,*,ctx,collections=None):
    payload=dict(label=label,delays=delays,expenses=expenses)
    if collections:payload['collections']=collections
    key,old=_begin(ledger,ctx,'scenario_saved',revision,payload)
    if old:return old
    _text(label,'Scenario name')
    if len(label)>120 or len(ledger.scenarios)>=30:raise CashflowError('Use a name up to 120 characters; at most 30 saved scenarios.')
    result=compound_scenario(ledger,delays,expenses,ctx=ctx,collections=collections)
    ident='scenario-'+key[:24]
    ledger.scenarios[ident]=dict(id=ident,business_id=ledger.business_id,label=label.strip(),
        delays=copy.deepcopy(delays),expenses=copy.deepcopy(expenses),collections=copy.deepcopy(collections or []),basis=_revision(ledger,'scenario',[]),
        as_of=str(ledger.as_of),actor=ctx.actor,conclusion_at_save=result['conclusion'])
    return _finish(ledger,key,'scenario_saved',payload,dict(scenario_id=ident),ctx)


SEARCH_KINDS={'invoice','bill','payment','refund','evidence','action','statement','agreement','scenario','promise','attachment','credit','loan','statement_balance'}


def search_records(ledger,query='',kind='',currency='',status='',from_date=None,to_date=None,offset=0,*,ctx):
    require_access(ctx,ledger,OP_READ)
    if not isinstance(query,str) or len(query)>200:raise CashflowError('Search text must be at most 200 characters.')
    if kind and kind not in SEARCH_KINDS:raise CashflowError('Choose a listed record type.')
    if currency and currency not in {a.currency for a in ledger.accounts.values()}:raise CashflowError('Choose a business account currency.')
    if not isinstance(status,str) or len(status)>64:raise CashflowError('Status filter must be at most 64 characters.')
    if type(offset) is not int or not 0<=offset<=100000:raise CashflowError('Invalid result offset.')
    for day in [from_date,to_date]:
        if day is not None and type(day) is not date:raise CashflowError('Use calendar dates for search filters.')
    if from_date and to_date and from_date>to_date:raise CashflowError('Search start must precede end.')
    rows=[]; all_statuses=set()
    def add(k,ident,ccy,day,state,text,url,sources,minor=None):
        all_statuses.add(state)
        source_ids=sorted(set(sources)-{''})
        # Evidence summaries are searchable, never parsed as instructions.
        hay=' '.join([k,ident,ccy or '',str(day or ''),state,text,' '.join(source_ids),
            ' '.join(ledger.evidence[s].summary for s in source_ids if s in ledger.evidence)]).casefold()
        if kind and k!=kind or currency and ccy!=currency or status and state!=status:return
        if any(term not in hay for term in query.casefold().split()):return
        if (from_date or to_date) and day is None:return
        if from_date and day<from_date or to_date and day>to_date:return
        rows.append(dict(kind=k,id=ident,currency=ccy,date=str(day) if day else None,status=state,
            description=text[:1000],url=url,evidence_ids=source_ids,amount_minor=minor))
    for rec in ledger.receivables.values():
        state='credit' if rec.credit_balance_minor else 'held' if collection_held(rec) else 'outstanding' if rec.remaining_minor else 'settled'
        add('invoice',rec.id,rec.currency,rec.expected_date,state,f'{rec.counterparty} {rec.invoice_reference}',
            '/invoice-adjustments#record-'+quote(rec.id,safe=''),[rec.evidence_id,rec.identity_evidence_id]+[c['evidence_id'] for c in rec.face_adjustments],rec.remaining_minor)
    for out in ledger.outflows.values():
        add('bill',out.id,out.currency,out.date,'overpaid' if out.overpaid_minor else out.status,
            f'{out.counterparty} {out.invoice_reference}', '/bill-adjustments#record-'+quote(out.id,safe=''),
            [out.evidence_id,out.identity_evidence_id]+[c['evidence_id'] for c in out.face_adjustments],out.remaining_minor)
    from .payment_review import observed_movements
    movements=observed_movements(ledger); movement_index={r['key']:r for r in movements}
    originals={}
    children={rid for p in ledger.split_receipts.values() for rid in p['allocation_ids']}
    for r in ledger.receipts.values():
        if r.state!='observed' or r.id in children:continue
        changes=[c for c in ledger.receipt_adjustments.values() if c['receipt_id']==r.id]
        rec=ledger.receivables[r.receivable_id]
        originals['receipt:'+r.id]=dict(currency=rec.currency,date=r.date_received,evidence_id=r.evidence_id,
            target=r.receivable_id,text=rec.counterparty+' '+rec.invoice_reference,history=changes)
    for p in ledger.split_receipts.values():
        originals['split_payment:'+p['id']]=dict(currency=p['currency'],date=date.fromisoformat(p['date']),
            evidence_id=p['evidence_id'],target=p['id'],text=p['note'],
            history=[c for c in ledger.split_adjustments.values() if c['payment_id']==p['id']])
    for o in ledger.outflows.values():
        for p in bill_payment_entries(o):
            originals['bill_payment:'+p['id']]=dict(currency=o.currency,date=date.fromisoformat(p['date']),evidence_id=p['evidence_id'],
                target=o.id,text=o.counterparty+' '+o.invoice_reference+' '+p.get('note',''),history=[c for c in o.payment_adjustments if c['payment_id']==p['id']])
    for m in ledger.cash_movements.values():
        originals['other_cash:'+m['id']]=dict(currency=m['currency'],date=date.fromisoformat(m['date']),evidence_id=m['evidence_id'],
            target=m['classification'],text=m['note'],history=m['adjustments'])
    payment_rows=[r for r in movements if r['kind'] not in {'refund','unallocated_refund'}]
    for key,original in originals.items():
        if key not in movement_index:
            payment_rows.append(dict(key=key,currency=original['currency'],date=str(movement_date(ledger,key,original['date'])),
                target=original['target'],evidence_id=original['evidence_id'],signed_minor=0,kind=key.split(':')[0]))
    for row in payment_rows:
        if row['kind']=='refund':continue
        if row['kind'].startswith('internal_transfer'):
            add('payment',row['key'],row['currency'],date.fromisoformat(row['date']),'observed',
                'Own-account transfer leg '+row['target']+' Â· '+row['account_id']+'. Zero business cash effect; no payment executed.',
                '/cash-accounts',[row['evidence_id']],row['signed_minor'])
            continue
        original=originals[row['key']];history=original['history']
        related=ledger.receivables.get(row['target']) or ledger.outflows.get(row['target'])
        text=row['target']+' '+original['text']+' '+' '.join(c.get('note','') for c in history)
        if related:text+=' '+related.counterparty+' '+related.invoice_reference
        add('payment',row['key'],row['currency'],date.fromisoformat(row['date']),'observed' if row['signed_minor'] else 'reversed',text,
            ('/payment-dates#movement-'+quote(row['key'],safe='')) if row['signed_minor'] else '/ledger',
            [row['evidence_id'],original['evidence_id']]+[c['evidence_id'] for c in history]+[c['evidence_id'] for c in ledger.date_corrections.values() if c['movement_key']==row['key']],row['signed_minor'])
    for source_kind,item,entry in refund_entries(ledger):
        add('refund',entry['id'],item.currency,movement_date(ledger,'refund:'+entry['id'],entry['date']),
            'observed' if effective_refund(entry) else 'reversed',source_kind+' '+item.id+' '+item.counterparty+' '+item.invoice_reference+' '+entry['note'],
            '/refunds#refund-'+quote(entry['id'],safe=''),[entry['evidence_id']]+[c['evidence_id'] for c in entry['adjustments']],effective_refund(entry)*(-1 if source_kind=='invoice' else 1))
    for e in ledger.evidence.values():add('evidence',e.id,'',None,e.kind,e.summary,'/ledger#evidence-'+quote(e.id,safe=''),[e.id])
    for action in ledger.actions.values():
        if action.business_id not in {'',ledger.business_id}:continue
        target=str(action.terms.get('receivable_id',action.terms.get('outflow_id','')))
        add('action',action.id,str(action.terms.get('currency','')),None,action.state,action.kind+' '+target,
            '/actions#'+quote(action.id,safe=''),[],action.terms.get('amount_minor'))
    for batch in ledger.statement_batches.values():
        if batch['business_id']!=ledger.business_id:continue
        for row in batch['rows']:
            from .completion import _row_key
            review=ledger.statement_reviews.get(_row_key(batch['id'],row['id']))
            state=review['status'] if review else 'unreviewed'
            if state=='matched':
                from .workbench import current_match_members
                if not current_match_members(review,movement_index):state='needs_review'
            add('statement',batch['id']+'/'+row['id'],row['currency'],date.fromisoformat(row['date']),state,row['reference'],
                '/statements?batch_id='+quote(batch['id'],safe=''),[batch['evidence_id']],row['signed_minor'])
    for proposal in ledger.instalment_proposals.values():
        add('agreement',proposal['id'],proposal['currency'],date.fromisoformat(proposal['schedule'][0]['date']),proposal['state'],
            proposal['bill_id']+' '+proposal['note'],'/instalments#agreement-'+quote(proposal['id'],safe=''),[proposal['evidence_id']],proposal['remaining_minor'])
    for saved in ledger.scenarios.values():
        if saved['business_id']==ledger.business_id:
            add('scenario',saved['id'],'',date.fromisoformat(saved['as_of']),'saved',saved['label'],
                '/scenarios?saved='+quote(saved['id'],safe=''),[],None)
    for r in ledger.unallocated_refunds.values():
        add('refund',r['id'],r['currency'],movement_date(ledger,'unallocated_refund:'+r['id'],r['date']),'observed' if effective_refund(r) else 'reversed',
            'Unallocated payment '+r['payment_id']+' '+r['note'],'/unallocated-refunds',[r['evidence_id']],-effective_refund(r))
    from .workbench import promise_report,attachment_inventory
    for p in promise_report(ledger,ctx=ctx):
        add('promise',p['id'],p['currency'],date.fromisoformat(p['due_date']),p['status'],p['invoice_id']+' '+p['counterparty']+' '+p['note'],
            '/promises#promise-'+quote(p['id'],safe=''),[p['evidence_id']],p['remaining_minor'])
    for a in attachment_inventory(ledger,ctx=ctx):
        add('attachment',a['id'],'',None,'active' if a['active'] else 'retired',a['filename']+' '+a['sha256']+' '+a['note'],
            '/attachments?evidence_id='+quote(a['evidence_id'],safe=''),[a['evidence_id']])
    for t in ledger.credit_transfers.values():
        add('credit',t['id'],t['currency'],date.fromisoformat(t['date']),'reversed' if t['reversed'] else 'active',
            t['party']+' '+t['source_id']+' to '+t['target_id']+' '+t['note'],'/credits',[t['evidence_id']],t['amount_minor'])
    for loan in ledger.loans.values():
        add('loan',loan['id'],loan['currency'],date.fromisoformat(loan['opening_date']),'registered',
            loan['lender']+' '+loan['reference']+' '+loan['note'],'/loans',[loan['evidence_id']],loan['opening_principal_minor'])
    for c in ledger.statement_balance_checks.values():
        add('statement_balance',c['id'],c['currency'],date.fromisoformat(c['end']),'recorded_terms',
            c['batch_id']+' '+c['note'],'/statement-balances?check_id='+quote(c['id'],safe=''),[c['evidence_id']])
    rows.sort(key=lambda r:(r['kind'],r['id']))
    return dict(total=len(rows),rows=rows[offset:offset+50],offset=offset,next_offset=offset+50 if offset+50<len(rows) else None,
        statuses=sorted(all_statuses),note='Business-scoped literal text search. Date filters use expected invoice date, contractual bill date, effective payment/refund date or statement row date. Undated evidence/actions are excluded when date filters are set. Currency filters exclude currency-neutral evidence; search without a currency filter to find it. Amounts remain separate by currency.')


def cash_calendar(ledger,start=None,days=30,currency='',*,ctx):
    require_access(ctx,ledger,OP_READ)
    start=start or ledger.as_of
    if type(start) is not date or type(days) is not int or not 1<=days<=62 or not ledger.as_of<=start<=ledger.as_of+timedelta(days=365):
        raise CashflowError('Calendar starts on/after the open snapshot within 365 days; show 1â€“62 days.')
    if currency and currency not in {a.currency for a in ledger.accounts.values()}:raise CashflowError('Choose a business account currency.')
    horizon=(start-ledger.as_of).days+days
    result=forecast(ledger,ledger.as_of,horizon);events={};unknown=[]
    def add(day,ccy,label,amount,state,sources,url):
        if currency and ccy!=currency:return
        if start<=day<start+timedelta(days=days):events.setdefault((day,ccy),[]).append(dict(label=label,amount_minor=amount,state=state,evidence_ids=sources,url=url))
    for event in cash_events(ledger):
        add(event['date'],event['currency'],event['kind']+' '+event['id'],event['amount_minor'],'recorded',
            [event['evidence_id']]+event.get('date_evidence_ids',[]),'/payment-dates')
    for rec in ledger.receivables.values():
        if not rec.remaining_minor or currency and rec.currency!=currency:continue
        if collection_held(rec) or rec.expected_date is None or rec.expected_date<ledger.as_of:
            unknown.append(dict(id=rec.id,currency=rec.currency,amount_minor=rec.remaining_minor,
                reason='held/disputed' if collection_held(rec) else 'unknown date' if rec.expected_date is None else 'expected date before opening; needs review',
                evidence_id=rec.evidence_id))
        else:add(rec.expected_date,rec.currency,'Expected collection '+rec.id,rec.remaining_minor,'expected',
            [rec.evidence_id,ledger.collection_date_source(rec.id)],'/ledger')
    for out in ledger.outflows.values():
        if out.status!='scheduled':continue
        for day,amount in bill_schedule(out):
            refs=[out.evidence_id]
            if out.agreement_schedules and out.agreement_schedules[-1]['face_count']==len(out.face_adjustments):refs.append(out.agreement_schedules[-1]['evidence_id'])
            add(max(day,ledger.as_of),out.currency,('Protected bill ' if out.protected else 'Unpaid bill ')+out.id+(' (overdue, reserved at opening)' if day<ledger.as_of else ''),
                -amount,'scheduled',refs,'/instalments' if out.counterparty_consent_required_for_change else '/payments')
    rows=[]
    for ccy,curve in result.currencies.items():
        if currency and ccy!=currency:continue
        for d in curve.days:
            if d.date>=start:
                rows.append(dict(date=str(d.date),currency=ccy,opening_minor=d.opening_minor,
                    inflow_minor=d.inflow_minor,outflow_minor=d.outflow_minor,intraday_low_minor=d.intraday_low_minor,
                    closing_minor=d.closing_minor,floor_minor=curve.floor_minor,floor_breached=d.floor_breached,
                    events=events.get((d.date,ccy),[])))
    rows.sort(key=lambda r:(r['date'],r['currency']))
    from .workbench import promise_report
    promises=[p for p in promise_report(ledger,ctx=ctx) if not p['cancelled'] and p['remaining_minor'] and (not currency or p['currency']==currency) and start<=date.fromisoformat(p['due_date'])<start+timedelta(days=days)]
    return dict(rows=rows,undated=unknown,promises=promises,clarifications=result.clarifications,
        note='Deterministic forecast calendar, not a bank balance or promised payment. Recorded observations, expected collections and scheduled obligations stay labelled separately. Unmaterialized recurring templates are not scheduled bills. Payroll protection remains. Same-day outflows precede inflows; currencies are never netted.')
