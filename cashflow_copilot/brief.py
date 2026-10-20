"""Read-only dated decision brief. Deterministic templates, never model prose."""
from collections import Counter
import copy
from datetime import datetime, timezone

from .authz import OP_READ, require_access
from .errors import CashflowError
from .forecast import forecast
from .funding import account_funding
from .owner_workflows import review_revision
from .planning import _hash
from .settlement import cash_events
from .treasury import treasury_report


def decision_brief(ledger, currency='', *, ctx):
    require_access(ctx,ledger,OP_READ)
    currency=currency or ledger.base_currency
    if currency not in {a.currency for a in ledger.accounts.values()}:
        raise CashflowError('Choose a recorded cash currency for the brief; no currency conversion is made.')
    if len(ledger.receivables)+len(ledger.outflows)>300 or len(ledger.accounts)>20:
        raise CashflowError('Decision brief uses the bounded planner: at most 300 invoices/bills and 20 accounts.')
    from .adaptive import generate_proposals,input_problems
    base=forecast(ledger,ledger.as_of,ledger.horizon_days);curve=base.currencies[currency]
    funding=account_funding(ledger,ctx=ctx);treasury=treasury_report(ledger,ctx=ctx)
    options=[]
    for p in generate_proposals(ledger,ctx=ctx):
        c=p.forecast.currencies.get(currency)
        options.append(dict(id=p.id,name=p.name,global_financially_sufficient=p.financially_sufficient,
            currency_min_minor=c.min_balance_minor if c else None,currency_shortfall_minor=c.worst_shortfall_minor if c else None,
            currency_closing_minor=c.closing_minor if c else None,pending_permissions=list(p.pending_permissions),
            reasons=list(dict.fromkeys(p.blockers+p.notes)),evidence_ids=p.evidence_ids,
            status='conditionally sufficient currency arithmetic; permissions/payment and account funding still pending' if p.financially_sufficient else 'insufficient or blocked under these recorded inputs'))
    def action_currency(a):
        if a.terms.get('currency'):return a.terms['currency']
        rec=ledger.receivables.get(a.terms.get('receivable_id'));out=ledger.outflows.get(a.terms.get('outflow_id'))
        return rec.currency if rec else out.currency if out else None
    scoped=[a for a in ledger.actions.values() if action_currency(a)==currency]
    actions=[dict(id=a.id,kind=a.kind,state=a.state,version=a.version,approved_version=a.approved_version,
                  terms=copy.deepcopy(a.terms),counterparty_response=copy.deepcopy(a.counterparty_response)) for a in scoped]
    events=[e for e in cash_events(ledger) if e['currency']==currency]
    current=[e for e in events if e['date']<=ledger.as_of]
    protected=[dict(id=o.id,date=o.date,remaining_minor=o.remaining_minor,evidence_id=o.evidence_id)
               for o in ledger.outflows.values() if o.currency==currency and o.protected and o.remaining_minor>0]
    account_rows=[]
    for row in funding['accounts']:
        if row['currency']==currency:
            c=row['forecast'];account_rows.append(dict(account_id=row['account_id'],status=row['status'],min_balance_minor=c.min_balance_minor,
                first_breach_date=c.first_breach_date,shortfall_minor=c.worst_shortfall_minor,evidence_id=row['evidence_id']))
    unrouted=[r for r in funding['unassigned'] if r['currency']==currency]
    unknown=[r for r in funding['unknown_timing'] if r['currency']==currency]
    account_clear=not unrouted and not unknown and all(r['status']=='no_shortfall_under_recorded_assumptions' for r in account_rows)
    evidence_ids={a.evidence_id for a in ledger.accounts.values() if a.currency==currency and a.evidence_id}
    evidence_ids.update(i.evidence_id for group in (ledger.receivables,ledger.outflows) for i in group.values() if i.currency==currency)
    evidence_ids.update(r['evidence_id'] for r in ledger.cash_restrictions.values() if ledger.accounts[r['account_id']].currency==currency)
    evidence_ids.update(r['evidence_id'] for r in ledger.forecast_routes.values() if ledger.accounts.get(r['account_id']) and ledger.accounts[r['account_id']].currency==currency)
    evidence_ids.update(ledger.collection_date_source(r.id) for r in ledger.receivables.values() if r.currency==currency)
    for group in (ledger.receivables,ledger.outflows):
        for item in group.values():
            if item.currency==currency:
                evidence_ids.update(a['evidence_id'] for a in item.face_adjustments)
                evidence_ids.update(a['evidence_id'] for a in getattr(item,'amendments',[]))
                evidence_ids.update(a['evidence_id'] for a in getattr(item,'agreement_schedules',[]))
    result=dict(business_id=ledger.business_id,business_name=ledger.business_name,as_of=ledger.as_of,
        generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),currency=currency,horizon_days=ledger.horizon_days,
        all_currency_plan_assessment=True,financial_prose_verified=False,mode='synthetic deterministic decision brief; no model commentary',
        balances=dict(treasury['currencies'][currency],forecast_min_minor=curve.min_balance_minor,first_breach_date=curve.first_breach_date,
                      forecast_closing_minor=curve.closing_minor,worst_shortfall_minor=curve.worst_shortfall_minor),
        account_funding=dict(no_shortfall_under_recorded_assumptions=account_clear,accounts=account_rows,unassigned=unrouted,unknown_timing=unknown),
        options=options,no_feasible_plan_found=not any(p['global_financially_sufficient'] for p in options),
        scope_note='This one-page financial snapshot is scoped to '+currency+'. Proposal sufficiency retains the existing all-currency assessment; it is not account-level funding, approval, consent or cash. Other currencies are reviewed separately.',
        action_states=dict(Counter(a.state for a in scoped)),actions=actions,unknown_currency_action_count=sum(action_currency(a) is None for a in ledger.actions.values()),
        observed_cash_record_count=len(current),future_recorded_cash_count=len(events)-len(current),
        observed_cash_evidence_ids=sorted({e['evidence_id'] for e in current}),protected_obligations=protected,
        assumptions=base.assumptions,clarifications=base.clarifications,input_problems=input_problems(ledger),
        sources=[dict(id=ident,kind=ledger.evidence[ident].kind,summary=ledger.evidence[ident].summary) for ident in sorted(evidence_ids) if ident in ledger.evidence],
        notes=['Expected receipts and plans are assumptions, not observed payments.',
               'Draft, owner approval, recorded external acceptance, observed payment and reconciliation remain distinct.',
               'No bank/source truth, independent identity, model prose or global optimality is certified. Three bounded strategies are compared.',
               'Read-only snapshot; refreshing or printing grants no authorization. Raw originals, credentials and saved agent prose are excluded.'])
    result['snapshot_id']=_hash([review_revision(ledger),currency,result['balances'],result['action_states'],result['options']])
    return result
