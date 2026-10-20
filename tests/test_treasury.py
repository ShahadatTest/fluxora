"""Financial/authority regressions for identities, account scope and restrictions."""
import copy
from datetime import date, timedelta
import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import treasury as t
from cashflow_copilot.authz import RequestContext, owner_context_for
from cashflow_copilot.errors import CashflowError, AuthorizationError
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Account, Evidence, Receivable, Outflow, Allocation
from cashflow_copilot.forecast import forecast
from cashflow_copilot.owner_workflows import review_revision, daily_preview, close_day
from cashflow_copilot.planning import _hash, _revision
from cashflow_copilot.store import SQLiteStore
from cashflow_copilot.settlement import cash_events
from cashflow_copilot.completion import (record_cash_movement, correct_cash_movement, import_statement,
    statement_report, review_statement_row)
from cashflow_copilot.obligations import carry_credit, reverse_credit, check_statement_balance, statement_balance_report
from cashflow_copilot.payment_review import observed_movements
from cashflow_copilot.workbench import review_net_settlement
from cashflow_copilot.extension import correct_payment_date
from cashflow_copilot.agent import TOOLS, _tool_get_evidence, _tool_get_constraints
from cashflow_copilot.reports import report_tables, xlsx_report
from cashflow_copilot.accounting import save_forecast, compare_forecast
from tests.test_web import live_server, _get, _post, _load


@pytest.fixture
def ledger():
    l=Ledger();l.business_id='treasury-test';l.business_name='Synthetic Treasury';l.as_of=date(2027,1,1);l.horizon_days=10
    l.accounts={'usd':Account('usd','USD',100000,10000,'src'),'eur':Account('eur','EUR',5000,1000,'src')}
    l.evidence={'src':Evidence('src','synthetic','Synthetic opening, named parties and restricted deposit source.')}
    l.receivables={'a':Receivable('a','USD',10000,l.as_of+timedelta(days=3),'src',counterparty='Example Ltd'),
                   'b':Receivable('b','USD',20000,l.as_of+timedelta(days=5),'src',counterparty='Example Trading')}
    l.outflows={'payroll':Outflow('payroll','USD',5000,l.as_of+timedelta(days=2),True,'src'),
                'bill':Outflow('bill','USD',2000,l.as_of+timedelta(days=4),False,'src',counterparty='Example Ltd')}
    return l


def call(l,fn,*args,revision=None,ctx=None):
    return fn(l,*args,'src','Checked synthetic source.',revision or review_revision(l),ctx=ctx or owner_context_for(l.business_id))


def add(l): return call(l,t.add_account,'usd-second','USD','Savings')
def transfer(l,ident='move',amount=30000,source='usd',target='usd-second'):
    return call(l,t.observed_transfer,ident,source,target,amount,l.as_of)
def hold(l,ident='held',amount=20000,account='usd'):
    return call(l,t.restrict_cash,ident,account,amount,'restricted_deposit')
def identity(l):
    call(l,t.register_party,'customer-one','customer','Example Ltd')
    call(l,t.update_party,'customer-one','alias','Example Trading')
    call(l,t.link_party,'invoice','a','customer-one')
    call(l,t.link_party,'invoice','b','customer-one')


def test_new_account_transfer_keeps_currency_cash_forecast_and_protected_payroll(ledger):
    l=ledger;before=forecast(l,l.as_of,10);cash=l.actual_cash('USD');payroll=copy.deepcopy(l.outflows['payroll'])
    add(l);transfer(l)
    assert t.account_cash(l,'usd')==70000 and t.account_cash(l,'usd-second')==30000
    assert l.actual_cash('USD')==cash and l.actual_cash('EUR')==5000
    assert forecast(l,l.as_of,10).currencies==before.currencies
    assert l.outflows['payroll']==payroll and not l.receipts and not l.actions
    legs=list(cash_events(l,include_internal=True))
    assert sum(e['amount_minor'] for e in legs)==0 and len(legs)==2
    assert not list(cash_events(l))


def test_transfer_replay_after_restart_and_later_reversal_never_reexecutes(ledger,tmp_path):
    l=ledger;add(l);rev=review_revision(l);transfer(l)
    s=SQLiteStore(tmp_path/'state.sqlite');s.save_ledger(l);s.close()
    s=SQLiteStore(tmp_path/'state.sqlite');l=s.load_ledger()
    result=call(l,t.observed_transfer,'move','usd','usd-second',30000,l.as_of,revision=rev)
    assert result['duplicate'] and t.account_cash(l,'usd-second')==30000
    call(l,t.reverse_transfer,'move');s.save_ledger(l);s.close()
    s=SQLiteStore(tmp_path/'state.sqlite');l=s.load_ledger();s.close()
    result=call(l,t.observed_transfer,'move','usd','usd-second',30000,l.as_of,revision=rev)
    assert result['duplicate'] and t.account_cash(l,'usd-second')==0 and l.actual_cash('USD')==100000
    assert len(list(t.transfer_events(l)))==4


@pytest.mark.parametrize('amount',[0,-1,True,1.5,10**15+1,100001])
def test_bad_or_unfunded_transfer_has_no_partial_leg(ledger,amount):
    add(ledger);before=_hash(vars(ledger))
    with pytest.raises(CashflowError): transfer(ledger,amount=amount)
    assert _hash(vars(ledger))==before


@pytest.mark.parametrize('source,target',[('usd','eur'),('usd','usd'),('missing','usd-second')])
def test_transfer_refuses_fx_same_account_or_missing_account(ledger,source,target):
    add(ledger);before=_hash(vars(ledger))
    with pytest.raises(CashflowError): transfer(ledger,source=source,target=target)
    assert _hash(vars(ledger))==before


@pytest.mark.parametrize('delta',[-1,1])
def test_transfer_must_be_observed_at_current_snapshot(ledger,delta):
    add(ledger)
    with pytest.raises(CashflowError):call(ledger,t.observed_transfer,'move','usd','usd-second',100,ledger.as_of+timedelta(days=delta))
    assert not ledger.internal_transfers


def test_transfer_and_reversal_keys_cannot_collide_with_another_transfer_id(ledger):
    add(ledger);transfer(ledger,ident='move');call(ledger,t.reverse_transfer,'move')
    t.observed_transfer(ledger,'move-reversal','usd','usd-second',30000,ledger.as_of,'src','Checked distinct transfer.',review_revision(ledger),ctx=owner_context_for(ledger.business_id),acknowledge_duplicates=True)
    movements=observed_movements(ledger)
    assert len({r['key'] for r in movements})==6 and len(movements)==6


def test_restricted_cash_reduces_forecast_once_without_book_cash_or_payroll_double_count(ledger):
    l=ledger;before=forecast(l,l.as_of,10);hold(l)
    report=t.treasury_report(l,ctx=owner_context_for(l.business_id))
    assert report['currencies']['USD']==dict(book_minor=100000,restricted_minor=20000,available_minor=80000,floor_minor=10000)
    after=forecast(l,l.as_of,10)
    for a,b in zip(before.currencies['USD'].days,after.currencies['USD'].days):
        assert b.closing_minor==a.closing_minor-20000 and b.outflow_minor==a.outflow_minor
    assert after.currencies['EUR']==before.currencies['EUR']
    assert l.actual_cash('USD')==100000 and l.outflows['payroll'].remaining_minor==5000
    assert not l.receipts and not l.actions and not l.consent_map
    call(l,t.release_cash,'held')
    assert forecast(l,l.as_of,10).currencies==before.currencies and l.actual_cash('USD')==100000
    assert l.cash_restrictions['held']['release']['evidence_id']=='src'


def test_hold_prevents_spending_restricted_cash_in_transfer(ledger):
    add(ledger);hold(ledger,amount=80000)
    with pytest.raises(CashflowError): transfer(ledger,amount=20001)
    transfer(ledger,amount=20000)
    assert t.account_cash(ledger,'usd')==80000
    assert t.treasury_report(ledger,ctx=owner_context_for(ledger.business_id))['currencies']['USD']['available_minor']==20000


@pytest.mark.parametrize('purpose',['payroll','cash_floor','loan','',None])
def test_restriction_is_not_a_duplicate_floor_or_payroll_reservation(ledger,purpose):
    before=_hash(vars(ledger))
    with pytest.raises(CashflowError): call(ledger,t.restrict_cash,'held','usd',100,purpose)
    assert _hash(vars(ledger))==before


@pytest.mark.parametrize('amount',[0,-1,False,1.1,100001])
def test_restriction_rejects_bad_amounts_without_mutation(ledger,amount):
    before=_hash(vars(ledger))
    with pytest.raises(CashflowError): hold(ledger,amount=amount)
    assert _hash(vars(ledger))==before


def test_holds_persist_across_snapshot_and_release_only_explicitly(ledger):
    l=ledger;add(l);transfer(l);hold(l,account='usd-second',amount=10000)
    day=l.as_of+timedelta(days=1);preview=daily_preview(l,day,ctx=owner_context_for(l.business_id))
    rows={r['account_id']:r for r in preview['accounts']}
    assert rows['usd']['calculated_opening_minor']==70000 and rows['usd-second']['calculated_opening_minor']==30000
    confirmations={ident:dict(opening_minor=r['calculated_opening_minor'],evidence_id='src') for ident,r in rows.items()}
    close_day(l,day,confirmations,'Checked each account.',review_revision(l),ctx=owner_context_for(l.business_id))
    assert l.actual_cash('USD')==100000 and t.account_cash(l,'usd-second')==30000
    assert t.restricted_cash(l,currency='USD')==10000
    assert forecast(l,l.as_of,10).currencies['USD'].days[0].opening_minor==90000
    with pytest.raises(CashflowError): call(l,t.reverse_transfer,'move')
    assert t.account_cash(l,'usd-second')==30000


def test_reverse_transfer_cannot_spend_held_destination_money(ledger):
    add(ledger);transfer(ledger);hold(ledger,account='usd-second',amount=10000)
    before=_hash(vars(ledger))
    with pytest.raises(CashflowError): call(ledger,t.reverse_transfer,'move')
    assert _hash(vars(ledger))==before
    call(ledger,t.release_cash,'held');call(ledger,t.reverse_transfer,'move')
    assert t.account_cash(ledger,'usd')==100000 and t.account_cash(ledger,'usd-second')==0


def test_party_alias_links_require_explicit_review_and_credit_stays_noncash(ledger):
    l=ledger;l.receivables['a'].allocations=[Allocation('old',15000,l.as_of,True)]
    before=l.actual_cash('USD');identity(l)
    assert l.receivables['a'].counterparty=='Example Ltd' and l.receivables['b'].counterparty=='Example Trading'
    result=call(l,carry_credit,'credit','invoice','a','b',5000)
    assert result['cash_delta_minor']==0 and l.receivables['b'].remaining_minor==15000 and l.actual_cash('USD')==before
    call(l,t.update_party,'customer-one','rename','Example Holdings')
    assert t.same_party(l,'invoice',l.receivables['a'],l.receivables['b'])
    with pytest.raises(CashflowError): call(l,t.link_party,'invoice','a','')
    call(l,reverse_credit,'credit');call(l,t.link_party,'invoice','a','')
    assert not t.same_party(l,'invoice',l.receivables['a'],l.receivables['b'])
    assert l.actual_cash('USD')==before


def test_register_and_alias_never_silently_merge_items(ledger):
    l=ledger;call(l,t.register_party,'customer-one','customer','Example Ltd')
    call(l,t.update_party,'customer-one','alias','Example Trading')
    assert not l.party_links and not t.same_party(l,'invoice',l.receivables['a'],l.receivables['b'])
    with pytest.raises(CashflowError): call(l,t.link_party,'invoice','b','missing')
    call(l,t.link_party,'invoice','a','customer-one')
    assert not t.same_party(l,'invoice',l.receivables['a'],l.receivables['b'])


def test_similar_or_wrong_role_names_never_supply_identity(ledger):
    call(ledger,t.register_party,'customer-one','customer','Example Ltd')
    with pytest.raises(CashflowError):call(ledger,t.link_party,'invoice','b','customer-one')
    with pytest.raises(CashflowError):call(ledger,t.link_party,'bill','bill','customer-one')
    call(ledger,t.register_party,'supplier-one','supplier','Example Ltd')
    call(ledger,t.link_party,'bill','bill','supplier-one')
    with pytest.raises(CashflowError):call(ledger,t.register_party,'duplicate','customer','example ltd')


def test_alias_retirement_requires_unlink_and_keeps_history(ledger):
    identity(ledger)
    with pytest.raises(CashflowError):call(ledger,t.update_party,'customer-one','retire_alias','Example Trading')
    call(ledger,t.link_party,'invoice','b','')
    call(ledger,t.update_party,'customer-one','retire_alias','Example Trading')
    assert ledger.party_register['customer-one']['aliases']==[]
    assert len(ledger.party_register['customer-one']['history'])==3
    call(ledger,t.update_party,'customer-one','rename','New Name')
    call(ledger,t.update_party,'customer-one','rename','Example Ltd')
    assert ledger.party_register['customer-one']['name']=='Example Ltd'


def test_registered_identity_summary_does_not_net_currencies(ledger):
    identity(ledger)
    ledger.receivables['eur-inv']=Receivable('eur-inv','EUR',900,ledger.as_of,'src',counterparty='Example Ltd')
    call(ledger,t.link_party,'invoice','eur-inv','customer-one')
    report=t.party_summary(ledger,'customer-one',ctx=owner_context_for(ledger.business_id))
    assert report['currencies']['USD']['remaining_minor']==30000 and report['currencies']['EUR']['remaining_minor']==900
    ledger.receivables['a'].counterparty='Changed source label'
    assert t.linked_party(ledger,'invoice',ledger.receivables['a']) is None


def cash(l,ident,amount=5000,kind='owner_capital'):
    return call(l,record_cash_movement,ident,kind,'USD',amount,l.as_of)


def test_observation_assignment_and_correction_follow_same_account(ledger):
    l=ledger;add(l);cash(l,'deposit');call(l,t.assign_movement,'other_cash:deposit','usd-second')
    assert t.account_cash(l,'usd')==100000 and t.account_cash(l,'usd-second')==5000
    call(l,correct_cash_movement,'deposit',3000,l.as_of)
    assert t.account_cash(l,'usd-second')==3000 and l.actual_cash('USD')==103000
    l.roll_forward(l.as_of+timedelta(days=1),ctx=owner_context_for(l.business_id))
    with pytest.raises(CashflowError):call(l,t.assign_movement,'other_cash:deposit','usd')
    assert t.account_cash(l,'usd-second')==3000


def test_new_account_keeps_existing_observations_on_original_default(ledger):
    cash(ledger,'deposit');before=ledger.actual_cash('USD');add(ledger)
    assert t.account_cash(ledger,'usd')==105000 and t.account_cash(ledger,'usd-second')==0
    assert ledger.actual_cash('USD')==before and ledger.cash_defaults['USD']=='usd'
    with pytest.raises(CashflowError):call(ledger,t.assign_movement,'other_cash:deposit','eur')


def statement(l,amount=5000):
    rows=[dict(id='row',currency='USD',signed_minor=amount,date=str(l.as_of),reference='Synthetic bank posting')]
    return import_statement(l,rows,'a'*64,'Synthetic file source.',review_revision(l),ctx=owner_context_for(l.business_id))['batch_id']


def test_statement_candidates_and_boundaries_are_account_scoped(ledger):
    l=ledger;add(l);cash(l,'deposit');call(l,t.assign_movement,'other_cash:deposit','usd-second');batch=statement(l)
    assert statement_report(l,batch,ctx=owner_context_for(l.business_id))['account_scope_required']
    call(l,t.bind_statement,batch,'usd')
    assert not statement_report(l,batch,ctx=owner_context_for(l.business_id))['rows'][0]['candidates']
    with pytest.raises(CashflowError):call(l,review_statement_row,batch,'row','other_cash:deposit','matched')
    with pytest.raises(CashflowError):call(l,check_statement_balance,'check',batch,'usd-second',l.as_of,l.as_of,0,5000,'')
    # Correct source under a fresh immutable imported identity.
    rows=[dict(id='row',currency='USD',signed_minor=5000,date=str(l.as_of),reference='Correct account')]
    batch2=import_statement(l,rows,'b'*64,'Correct account file.',review_revision(l),ctx=owner_context_for(l.business_id))['batch_id']
    call(l,t.bind_statement,batch2,'usd-second')
    call(l,review_statement_row,batch2,'row','other_cash:deposit','matched')
    call(l,check_statement_balance,'check',batch2,'usd-second',l.as_of,l.as_of,0,5000,'')
    r=statement_balance_report(l,'check',ctx=owner_context_for(l.business_id))
    assert r['ledger_opening_minor']==0 and r['ledger_closing_minor']==5000 and r['closing_delta_minor']==0
    assert r['unresolved_row_ids']==[]


def test_transfer_legs_match_separate_account_statements(ledger):
    add(ledger);transfer(ledger);batch=statement(ledger,amount=-30000);call(ledger,t.bind_statement,batch,'usd')
    row=statement_report(ledger,batch,ctx=owner_context_for(ledger.business_id))['rows'][0]
    assert [m['key'] for m in row['candidates']]==['internal_transfer_out:move']
    call(ledger,review_statement_row,batch,'row','internal_transfer_out:move','matched')
    with pytest.raises(CashflowError):call(ledger,correct_payment_date,'internal_transfer_out:move',ledger.as_of+timedelta(days=1))


def test_net_matching_refuses_cross_account_components(ledger):
    l=ledger;add(l);cash(l,'gross',5000);cash(l,'fee',100,'bank_fee')
    call(l,t.assign_movement,'other_cash:gross','usd-second');batch=statement(l,4900);call(l,t.bind_statement,batch,'usd-second')
    with pytest.raises(CashflowError):call(l,review_net_settlement,batch,'row',['other_cash:gross','other_cash:fee'])
    call(l,t.assign_movement,'other_cash:fee','usd-second')
    call(l,review_net_settlement,batch,'row',['other_cash:gross','other_cash:fee'])
    assert statement_report(l,batch,ctx=owner_context_for(l.business_id))['rows'][0]['status']=='matched'


def test_source_reassignment_invalidates_prior_statement_match(ledger):
    cash(ledger,'deposit');batch=statement(ledger)
    call(ledger,review_statement_row,batch,'row','other_cash:deposit','matched')
    add(ledger);call(ledger,t.bind_statement,batch,'usd');call(ledger,t.assign_movement,'other_cash:deposit','usd-second')
    assert statement_report(ledger,batch,ctx=owner_context_for(ledger.business_id))['rows'][0]['status']=='needs_review'


OPERATIONS=['party','identity','link','account','assignment','statement','transfer','reverse','restriction','release']
def operation(l,name,ctx,revision):
    args={
        'party':(t.register_party,'other','customer','Another Party'),
        'identity':(t.update_party,'customer-one','alias','Another Alias'),
        'link':(t.link_party,'invoice','a','customer-one'),
        'account':(t.add_account,'third','USD','Third'),
        'assignment':(t.assign_movement,'other_cash:deposit','usd-second'),
        'statement':(t.bind_statement,next(iter(l.statement_batches)),'usd'),
        'transfer':(t.observed_transfer,'second','usd','usd-second',1,l.as_of),
        'reverse':(t.reverse_transfer,'move'),
        'restriction':(t.restrict_cash,'other-hold','usd',1,'legal_hold'),
        'release':(t.release_cash,'held'),
    }[name]
    return call(l,args[0],*args[1:],ctx=ctx,revision=revision)


@pytest.mark.parametrize('name',OPERATIONS)
@pytest.mark.parametrize('role,business',[('agent','treasury-test'),('viewer','treasury-test'),('owner','another-business')])
def test_every_mutation_authorizes_before_work_and_replay(ledger,name,role,business):
    l=ledger;identity(l);add(l);transfer(l);hold(l,amount=100);cash(l,'deposit');statement(l)
    ctx=RequestContext('tester',role,business);rev=review_revision(l);before=_hash(vars(l))
    with pytest.raises(AuthorizationError):operation(l,name,ctx,rev)
    assert _hash(vars(l))==before
    operation(l,name,owner_context_for(l.business_id),rev)
    before=_hash(vars(l))
    with pytest.raises(AuthorizationError):operation(l,name,ctx,rev)
    assert _hash(vars(l))==before


def test_stale_review_refused_and_new_restriction_invalidates_plan_revision(ledger):
    rev=review_revision(ledger);plan_rev=_revision(ledger,'financial-inputs',[]);hold(ledger)
    with pytest.raises(CashflowError):call(ledger,t.add_account,'new','USD','New',revision=rev)
    assert _revision(ledger,'financial-inputs',[])!=plan_rev


def test_migration_loads_empty_registers_and_roundtrip_preserves_new_graph(ledger,tmp_path):
    path=tmp_path/'legacy.sqlite';s=SQLiteStore(path);s.save_ledger(ledger)
    s.conn.executemany('DELETE FROM meta WHERE key=?',[(name,) for name in t.STATE_FIELDS]);s.conn.commit();l=s.load_ledger()
    assert all(getattr(l,name)=={} for name in t.STATE_FIELDS)
    identity(l);add(l);transfer(l);hold(l);before=_hash(vars(l));s.save_ledger(l);s.close()
    s=SQLiteStore(path);loaded=s.load_ledger();s.close();assert _hash(vars(loaded))==before
    assert loaded.actual_cash('USD')==100000 and t.account_cash(loaded,'usd-second')==30000


def test_agent_reads_authoritative_availability_but_has_no_new_mutation_tool(ledger):
    identity(ledger);add(ledger);transfer(ledger);hold(ledger)
    ctx=RequestContext('agent','agent',ledger.business_id)
    evidence=_tool_get_evidence(ctx,ledger,{},'scripted-test')
    assert evidence['cash_accounts_and_availability']['currencies']['USD']['available_minor']==80000
    assert evidence['reviewed_party_identities'][0]['id']=='customer-one'
    assert len(TOOLS)==8 and all('transfer' not in name and 'restrict' not in name and 'register' not in name for name in TOOLS)
    constraints=_tool_get_constraints(ctx,ledger,{},'scripted-test')
    assert constraints['floors']['USD']['floor_minor']==10000


def test_report_has_one_currency_summary_plus_traceable_account_identity_and_restriction_exports(ledger):
    identity(ledger);add(ledger);transfer(ledger);hold(ledger)
    report=report_tables(ledger,ctx=owner_context_for(ledger.business_id));tables={r['name']:r for r in report['tables']}
    assert len(tables['Summary']['rows'])==2
    assert {'Cash accounts','Cash restrictions','Own transfers','Party identities'}<=set(tables)
    rows=tables['Cash accounts']['rows'];assert sum(r[4].minor for r in rows if r[2]=='USD')==100000
    assert sum(r[6].minor for r in rows if r[2]=='USD')==80000
    assert xlsx_report(report).startswith(b'PK')


def put(db,l):
    s=SQLiteStore(db)
    try:s.save_ledger(l)
    finally:s.close()


def form_data(l,op,**values):
    from cashflow_copilot import web
    return dict(csrf=web.CSRF_TOKEN,revision=review_revision(l),operation=op,
                evidence_id='src',note='Checked synthetic owner operation.',**values)


@pytest.mark.parametrize('path',['/party-register','/cash-accounts','/cash-availability','/app','/entries','/reports','/review','/agent'])
def test_populated_http_pages_discoverable_and_escaped(live_server,ledger,path):
    if path == '/agent':
        from cashflow_copilot import web, agent_web
        agent_web.mount(web)
    base,db=live_server;identity(ledger);add(ledger);transfer(ledger);hold(ledger);cash(ledger,'deposit')
    ledger.account_details['usd-second']['name']='<script>alert(1)</script>'
    ledger.party_register['customer-one']['name']='<script>bad()</script>'
    put(db,ledger)
    status,html=_get(base,path)
    assert status==200 and '<script>alert(1)</script>' not in html and '<script>bad()</script>' not in html
    if path=='/cash-accounts':assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_http_preview_confirmation_replay_and_concurrent_transfer(live_server,ledger):
    base,db=live_server;put(db,ledger)
    fields=form_data(ledger,'account',account_id='usd-second',currency='USD',name='Savings')
    before=_hash(vars(_load(db)))
    status,html=_post(base,'/treasury/preview',fields)
    assert status==200 and 'No saved state changed' in html and _hash(vars(_load(db)))==before
    assert _post(base,'/treasury/confirm',dict(fields,confirmed='yes'))[0]==200
    l=_load(db);fields=form_data(l,'transfer',transfer_id='web-transfer',source_id='usd',target_id='usd-second',amount='100.00',date=str(l.as_of),confirmed='yes')
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _: _post(base,'/treasury/confirm',fields)[0],range(6)))
    assert results==[200]*6
    l=_load(db);assert t.account_cash(l,'usd-second')==10000 and l.actual_cash('USD')==100000
    assert len(l.internal_transfers)==1
    s=SQLiteStore(db);s.close()  # new connection/ledger reload preserves replay
    before=_hash(vars(l));assert _post(base,'/treasury/confirm',fields)[0]==200
    assert _hash(vars(_load(db)))==before


@pytest.mark.parametrize('fault',['csrf','confirmation','forged_role','stale','unknown_operation','amount'])
def test_http_bad_owner_requests_do_not_mutate(live_server,ledger,fault):
    base,db=live_server;add(ledger);put(db,ledger)
    fields=form_data(ledger,'transfer',transfer_id='move',source_id='usd',target_id='usd-second',amount='1.00',date=str(ledger.as_of),confirmed='yes')
    if fault=='csrf':fields['csrf']='bad'
    if fault=='confirmation':fields.pop('confirmed')
    if fault=='forged_role':fields['role']='owner'
    if fault=='stale':fields['revision']='old'
    if fault=='unknown_operation':fields['operation']='send_money'
    if fault=='amount':fields['amount']='NaN'
    before=_hash(vars(_load(db)));status,_=_post(base,'/treasury/confirm',fields)
    assert status in {400,403} and _hash(vars(_load(db)))==before


def test_restriction_changes_decision_cash_without_false_forecast_actual_cash_variance(ledger):
    hold(ledger)
    result=save_forecast(ledger,'Restricted funds comparison',review_revision(ledger),ctx=owner_context_for(ledger.business_id))
    ctx=owner_context_for(ledger.business_id)
    report=compare_forecast(ledger,result['snapshot_id'],ledger.as_of,ctx=ctx)
    row=report['currencies']['USD'][0]
    assert row['decision_closing_minor']==80000 and row['closing_minor']==100000
    assert row['recorded_closing_minor']==100000 and row['difference_minor']==0
    call(ledger,t.release_cash,'held')
    assert compare_forecast(ledger,result['snapshot_id'],ledger.as_of,ctx=ctx)['currencies']['USD'][0]['difference_minor']==0


def test_new_transfer_id_does_not_bypass_possible_duplicate_review(ledger):
    add(ledger);transfer(ledger)
    before=_hash(vars(ledger))
    with pytest.raises(CashflowError,match='Possible duplicate'):transfer(ledger,ident='same-source-another-id')
    assert _hash(vars(ledger))==before


def test_search_includes_transfer_legs_and_never_treats_them_as_external_receipts(ledger):
    from cashflow_copilot.exploration import search_records
    add(ledger);transfer(ledger)
    result=search_records(ledger,'move',ctx=owner_context_for(ledger.business_id))
    assert len([r for r in result['rows'] if r['kind']=='payment'])==2
    assert all(r['url']=='/cash-accounts' for r in result['rows'])


def test_multiple_accounts_do_not_block_planning_or_affordability(ledger):
    from cashflow_copilot.adaptive import input_problems
    from cashflow_copilot.business_tools import affordability
    add(ledger);transfer(ledger);hold(ledger)
    assert not any('Multiple accounts' in p or 'negative' in p for p in input_problems(ledger))
    report=affordability(ledger,'USD',100,ledger.as_of,'Synthetic purchase',ctx=owner_context_for(ledger.business_id))
    assert report['baseline'].currencies['USD'].days[0].opening_minor==80000


def test_negative_account_available_cash_requires_source_review_even_with_positive_pooled_cash(ledger):
    from cashflow_copilot.adaptive import input_problems
    add(ledger);transfer(ledger);hold(ledger,account='usd-second',amount=20000)
    # A real observed expense must still be recorded even when it reveals a
    # source inconsistency; it must not produce an all-clear sufficient plan.
    cash(ledger,'expense',25000,'other_outflow');call(ledger,t.assign_movement,'other_cash:expense','usd-second')
    assert ledger.actual_cash('USD')==75000
    assert any('usd-second' in p and 'negative' in p for p in input_problems(ledger))
