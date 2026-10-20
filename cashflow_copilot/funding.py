"""Account-specific future funding and weekly aggregation of the same engine.

All routes are reviewed assumptions. No transfer, bill, cash or consent is created.
"""
from datetime import date, timedelta

from .authz import OP_READ, require_access
from .errors import CashflowError
from .forecast import forecast
from .owner_workflows import _begin, _finish, _evidence, _text, review_revision
from .planning import _hash
from .settlement import bill_schedule, collection_held, cash_events
from .treasury import currency_accounts, restricted_cash, currency_opening


def _item(ledger, kind, item_id):
    group = ledger.receivables if kind == 'invoice' else ledger.outflows if kind == 'bill' else {}
    return group.get(item_id)


def route_signature(ledger, kind, item_id):
    return _hash([ledger.business_id, kind, _item(ledger,kind,item_id),
                  ledger.collection_date_sources.get(item_id) if kind == 'invoice' else None])


def projected_account(ledger, kind, item_id):
    item = _item(ledger, kind, item_id)
    if item is None: raise CashflowError('Choose an existing invoice or bill.')
    if item.evidence_id not in ledger.evidence:
        return dict(account_id=None,status='missing_source',evidence_id=item.evidence_id,note='Source evidence is missing; no account funding conclusion.')
    route = ledger.forecast_routes.get(kind+':'+item_id)
    if route:
        account = ledger.accounts.get(route['account_id'])
        valid = account is not None and account.currency == item.currency and route['evidence_id'] in ledger.evidence and route['signature'] == route_signature(ledger,kind,item_id)
        return dict(account_id=account.id if valid else None, status='reviewed' if valid else 'stale',
                    evidence_id=route['evidence_id'], note=route['note'])
    accounts = currency_accounts(ledger,item.currency)
    return dict(account_id=accounts[0].id if len(accounts)==1 else None,
                status='single_account_assumption' if len(accounts)==1 else 'unassigned', evidence_id=item.evidence_id, note='No new authority; single-account assumption only.' if len(accounts)==1 else 'Explicit account review needed.')


def assign_forecast_route(ledger, kind, item_id, account_id, evidence_id, note, revision, *, ctx):
    payload = dict(kind=kind,item_id=item_id,account_id=account_id,evidence_id=evidence_id,note=note)
    key,old = _begin(ledger,ctx,'forecast_account_reviewed',revision,payload)
    if old: return old
    item = _item(ledger,kind,item_id);account = ledger.accounts.get(account_id)
    _evidence(ledger,evidence_id);_text(note,'Expected account source / assumption')
    if item is None or account is None or item.currency != account.currency:
        raise CashflowError('Choose an existing invoice/bill and an own account in the same currency.')
    if item.remaining_minor <= 0: raise CashflowError('Only remaining claims/obligations need a future account route.')
    if kind=='invoice' and collection_held(item): raise CashflowError('Resolve the collection hold before assigning expected inflow; a route cannot release it.')
    ledger.forecast_routes[kind+':'+item_id] = dict(payload,signature=route_signature(ledger,kind,item_id),actor=ctx.actor)
    return _finish(ledger,key,'forecast_account_reviewed',payload,dict(kind=kind,item_id=item_id,account_id=account_id,cash_delta_minor=0),ctx)


def projection_drivers(ledger, horizon_days):
    """Dated signed components of the unmodified baseline currency forecast."""
    if type(horizon_days) is not int or not 1<=horizon_days<=120: raise CashflowError('Use 1â€“120 forecast days.')
    start,last=ledger.as_of,ledger.as_of+timedelta(days=horizon_days-1)
    result=[]
    for event in cash_events(ledger):
        if start<=event['date']<=last:
            result.append(dict(kind=event['kind'],id=event['id'],currency=event['currency'],date=event['date'],
                signed_minor=event['amount_minor'],evidence_ids=sorted({event['evidence_id'],*event.get('date_evidence_ids',[])}-{''}),
                basis='recorded observation / correction',account_id=event['account_id']))
    for rec in ledger.receivables.values():
        if rec.remaining_minor>0 and not collection_held(rec) and rec.expected_date and start<=rec.expected_date<=last:
            route=projected_account(ledger,'invoice',rec.id)
            result.append(dict(kind='expected_collection',id=rec.id,currency=rec.currency,date=rec.expected_date,
                signed_minor=rec.remaining_minor,evidence_ids=sorted({rec.evidence_id,ledger.collection_date_source(rec.id),route['evidence_id']}-{''}),
                basis='expected, not observed or promised cash',account_id=route['account_id']))
    for out in ledger.outflows.values():
        if out.status!='scheduled': continue
        route=projected_account(ledger,'bill',out.id)
        for day,minor in bill_schedule(out):
            if minor>0 and day<=last:
                refs={out.evidence_id,route['evidence_id']}
                refs.update(a['evidence_id'] for a in out.face_adjustments)
                refs.update(a['evidence_id'] for a in out.agreement_schedules)
                refs.update(a['evidence_id'] for a in out.amendments)
                result.append(dict(kind='protected_payroll_or_bill' if out.protected else 'scheduled_unpaid_bill',id=out.id,currency=out.currency,date=max(day,start),
                    contractual_date=day,signed_minor=-minor,evidence_ids=sorted(refs-{''}),basis='scheduled obligation; not an observed payment',account_id=route['account_id']))
    return sorted(result,key=lambda r:(r['date'],r['currency'],r['kind'],r['id']))


def account_funding(ledger, *, ctx, horizon_days=None):
    require_access(ctx,ledger,OP_READ)
    horizon=ledger.horizon_days if horizon_days is None else horizon_days
    if type(horizon) is not int or not 1<=horizon<=120 or len(ledger.accounts)>20 or len(ledger.receivables)+len(ledger.outflows)>600:
        raise CashflowError('Funding review supports 1â€“120 days, 20 accounts and 600 invoices/bills.')
    last=ledger.as_of+timedelta(days=horizon-1);routes=[];missing=[];unknown=[]
    for kind,group in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
        for item in group.values():
            if item.remaining_minor<=0: continue
            route=projected_account(ledger,kind,item.id)
            held=kind=='invoice' and collection_held(item)
            timed=(item.expected_date is not None and ledger.as_of<=item.expected_date<=last) if kind=='invoice' else any(day<=last and m>0 for day,m in bill_schedule(item))
            row=dict(kind=kind,item_id=item.id,currency=item.currency,remaining_minor=item.remaining_minor,
                     source_evidence_id=item.evidence_id,protected=kind=='bill' and item.protected,held=held,timed=timed,**route)
            routes.append(row)
            if timed and not held and route['account_id'] is None: missing.append(row)
            if kind=='invoice' and not held and (item.expected_date is None or item.expected_date<ledger.as_of): unknown.append(row)
    accounts=[]
    for a in ledger.accounts.values():
        result=forecast(ledger,ledger.as_of,horizon,account_id=a.id);curve=result.currencies[a.currency]
        incomplete=any(r['currency']==a.currency for r in missing+unknown) or a.evidence_id not in ledger.evidence
        accounts.append(dict(account_id=a.id,currency=a.currency,name=ledger.account_details.get(a.id,{}).get('name',a.id),
            evidence_id=a.evidence_id,restricted_minor=restricted_cash(ledger,account_id=a.id),forecast=curve,
            status='incomplete_account_routing_or_timing' if incomplete else 'account_floor_shortfall' if curve.first_breach_date else 'no_shortfall_under_recorded_assumptions'))
    drivers=projection_drivers(ledger,horizon)
    unassigned=[r for r in drivers if r['account_id'] is None]
    baseline=forecast(ledger,ledger.as_of,horizon);bridges=[]
    for ccy,curve in baseline.currencies.items():
        scoped=[r for r in accounts if r['currency']==ccy]
        for day in curve.days:
            unresolved=sum(r['signed_minor'] for r in unassigned if r['currency']==ccy and r['date']<=day.date)
            assigned=sum(r['forecast'].balance_on(day.date) for r in scoped)
            if assigned+unresolved!=day.closing_minor:
                raise CashflowError('Account and currency projections do not reconcile; review the source graph.')
        bridges.append(dict(currency=ccy,assigned_account_closing_minor=sum(r['forecast'].closing_minor for r in scoped),
            unassigned_net_minor=sum(r['signed_minor'] for r in unassigned if r['currency']==ccy),pooled_closing_minor=curve.closing_minor))
    return dict(as_of=ledger.as_of,horizon_days=horizon,accounts=accounts,routes=routes,unassigned=missing,unknown_timing=unknown,
        unassigned_drivers=unassigned,currency_bridges=bridges,complete=not missing and not unknown and all(a.evidence_id in ledger.evidence for a in ledger.accounts.values()),
        note='Account-specific baseline uses reviewed future-account assumptions and already-observed account cash. It does not execute/assume balancing transfers. Unassigned or stale routes and unknown collection timing prevent an all-clear. Outflows precede same-day inflows; pooled currency sufficiency is not account funding or payment authorization.')


def weekly_cashflow(ledger, *, ctx):
    require_access(ctx,ledger,OP_READ)
    if len(ledger.receivables)+len(ledger.outflows)>600: raise CashflowError('Weekly review supports at most 600 invoices/bills.')
    result=forecast(ledger,ledger.as_of,91);drivers=projection_drivers(ledger,91);currencies={}
    for ccy,curve in result.currencies.items():
        weeks=[]
        for i in range(13):
            days=curve.days[i*7:(i+1)*7];first,last=days[0],days[-1]
            sources=[r for r in drivers if r['currency']==ccy and first.date<=r['date']<=last.date]
            signed=sum(r['signed_minor'] for r in sources)
            if first.opening_minor+signed!=last.closing_minor:
                raise CashflowError('Weekly source bridge does not reconcile with the deterministic engine.')
            low=min(days,key=lambda r:(r.intraday_low_minor,r.date))
            weeks.append(dict(week=i+1,start=first.date,end=last.date,opening_minor=first.opening_minor,
                inflow_minor=sum(d.inflow_minor for d in days),outflow_minor=sum(d.outflow_minor for d in days),
                closing_minor=last.closing_minor,min_balance_minor=low.intraday_low_minor,min_date=low.date,
                first_breach_date=next((d.date for d in days if d.floor_breached),None),floor_minor=curve.floor_minor,
                shortfall_minor=max(0,curve.floor_minor-low.intraday_low_minor),drivers=sources,days=days))
        currencies[ccy]=dict(book_opening_minor=currency_opening(ledger,ccy),restricted_minor=restricted_cash(ledger,currency=ccy),weeks=weeks)
    return dict(as_of=ledger.as_of,horizon_days=91,currencies=currencies,assumptions=result.assumptions,clarifications=result.clarifications,
        note='13 consecutive seven-day periods from the visible business snapshot, computed by the existing 91-day daily engine. Weekly closing can hide an earlier intraday breach: minimum/date and daily drill-down remain visible. Recorded movements, expected collections and scheduled obligations are distinct. Active restrictions are deducted once; no FX funding, unconfirmed recurring bill or assumed restriction release is added. This view does not change the configured planning horizon.')


def funding_summary(ledger, *, ctx):
    report=account_funding(ledger,ctx=ctx)
    return dict(as_of=str(report['as_of']),horizon_days=report['horizon_days'],complete=report['complete'],
        unassigned=report['unassigned'],unknown_timing=report['unknown_timing'],currency_bridges=report['currency_bridges'],
        accounts=[dict(account_id=r['account_id'],currency=r['currency'],status=r['status'],evidence_id=r['evidence_id'],
            min_balance_minor=r['forecast'].min_balance_minor,min_date=str(r['forecast'].min_balance_date),
            first_breach_date=str(r['forecast'].first_breach_date) if r['forecast'].first_breach_date else None,
            shortfall_minor=r['forecast'].worst_shortfall_minor,closing_minor=r['forecast'].closing_minor) for r in report['accounts']],note=report['note'])
