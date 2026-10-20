"""Independent cash controls, owner attestations and non-mutating purchases."""
import copy
from datetime import date, timedelta
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import recovery
from cashflow_copilot.accounting import correct_receipt, save_forecast, compare_forecast, set_collection_hold
from cashflow_copilot.agent import TOOLS, execute_tool
from cashflow_copilot.business_tools import (affordability, data_freshness, confirm_fact_check,
    confirm_split_observation, allocate_split_balance)
from cashflow_copilot.errors import CashflowError, AuthorizationError
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account, Receivable, Receipt
from cashflow_copilot.owner_workflows import (import_observations, review_revision, daily_preview,
    close_day, allocate_observation, confirm_collection_date, review_receipt, reconcile_existing_receipt)
from cashflow_copilot.planning import _revision, _hash
from cashflow_copilot.reports import report_tables, xlsx_report, csv_archive
from cashflow_copilot.settlement import cash_events, effective_receipt, split_unallocated
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx, save
from tests.test_web import live_server, _get, _post, _load


def imported(ledger, amount=120000, oid='bulk-one', day=None):
    row = dict(id=oid, amount_minor=amount, currency='USD', date=(day or ledger.as_of).isoformat(), invoice_reference='')
    return import_observations(ledger,[row],'a'*64,review_revision(ledger),ctx=ctx(ledger))


def split(ledger, assignments=None, oid='bulk-one', revision=None, caller=None, acknowledge=False):
    return confirm_split_observation(ledger,oid,{'custom-x':40000,'custom-y':20000} if assignments is None else assignments,
        'Verified synthetic payment and invoice identities.',acknowledge,revision or review_revision(ledger),ctx=caller or ctx(ledger))


def more(ledger, assignments, day=None, revision=None):
    return allocate_split_balance(ledger,'bulk-one',assignments,day or ledger.as_of,'customer-y',
        'Checked residual allocation source.',revision or review_revision(ledger),ctx=ctx(ledger))


def check(ledger,kind='cash',ident='operating',revision=None,caller=None):
    return confirm_fact_check(ledger,kind,ident,'customer-x','Checked exact current synthetic fact.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def purchase(ledger,amount=10000,day=None,caller=None,ccy='USD'):
    return affordability(ledger,ccy,amount,day or date(2027,1,15),'New synthetic stock',ctx=caller or ctx(ledger))


def test_whole_payment_cash_once_invoices_partial_excess_and_no_consent(own):
    before=copy.deepcopy(vars(own));imported(own)
    assert own.actual_cash('USD')==100000
    result=split(own)
    assert len(result['allocation_ids'])==2 and result['unallocated_minor']==60000
    assert own.receivables['custom-x'].remaining_minor==25000 and own.receivables['custom-y'].remaining_minor==25000
    assert own.actual_cash('USD')==220000
    events=list(cash_events(own))
    assert [(e['id'],e['amount_minor']) for e in events]==[('bulk-one',120000)]
    curve=forecast(own,own.as_of,30).currencies['USD']
    assert curve.days[0].inflow_minor==120000 and curve.closing_minor==130000
    assert not own.consent_map and not own.runtime_evidence_ids and not own.actions
    assert own.receipt_inbox['bulk-one']['status']=='split_recorded'
    assert own.accounts==before['accounts']


def test_unallocated_can_be_allocated_after_rebase_without_new_cash(own):
    imported(own);result=split(own)
    sid=save_forecast(own,'After first split',review_revision(own),ctx=ctx(own))['snapshot_id']
    own.roll_forward(date(2027,1,2),ctx=ctx(own));assert own.actual_cash('USD')==220000
    rev=review_revision(own);added=more(own,{'custom-x':25000,'custom-y':25000},revision=rev)
    assert added['unallocated_minor']==10000 and own.actual_cash('USD')==220000
    assert len(own.receipts)==4 and len(list(cash_events(own)))==1
    before=copy.deepcopy(vars(own));assert more(own,{'custom-x':25000,'custom-y':25000},revision=rev)['duplicate']
    assert vars(own)==before
    comparison=compare_forecast(own,sid,date(2027,1,2),ctx=ctx(own))
    assert comparison['currencies']['USD'][0]['recorded_receipts_minor']==120000
    assert comparison['currencies']['USD'][1]['recorded_receipts_minor']==0
    own.roll_forward(date(2027,1,3),ctx=ctx(own));assert own.actual_cash('USD')==220000


def test_empty_initial_assignments_keep_whole_payment_unallocated(own):
    imported(own);split(own,{})
    assert not own.receipts and own.actual_cash('USD')==220000
    assert split_unallocated(own,own.split_receipts['bulk-one'])==120000
    more(own,{'custom-x':65000,'custom-y':45000})
    assert own.actual_cash('USD')==220000 and split_unallocated(own,own.split_receipts['bulk-one'])==10000


def test_split_child_correction_changes_allocation_not_cash_and_archives_review(own):
    imported(own);result=split(own);rid=result['allocation_ids'][0]
    review_receipt(own,rid,'','customer-x','Reviewed existing allocation.',False,review_revision(own),ctx=ctx(own))
    reconcile_existing_receipt(own,rid,'customer-x','Confirmed allocation source.',review_revision(own),ctx=ctx(own))
    original=copy.deepcopy(own.receipts[rid]);rev=review_revision(own)
    correction=correct_receipt(own,rid,'custom-y',10000,own.as_of,'customer-y','Correct invoice source checked.',rev,ctx=ctx(own))
    assert correction['cash_delta_minor']==0 and own.actual_cash('USD')==220000
    assert own.receipts[rid]==original and rid not in own.receipt_reviews and own.receipt_review_archive
    assert own.receivables['custom-x'].remaining_minor==65000 and own.receivables['custom-y'].remaining_minor==15000
    assert split_unallocated(own,own.split_receipts['bulk-one'])==90000
    assert len(list(cash_events(own)))==1
    before=copy.deepcopy(vars(own));assert correct_receipt(own,rid,'custom-y',10000,own.as_of,'customer-y','Correct invoice source checked.',rev,ctx=ctx(own))['duplicate']
    assert vars(own)==before


def test_split_allocation_correction_cannot_invent_cash_or_consume_earlier_capacity(own):
    imported(own,60000);result=split(own);rid=result['allocation_ids'][0]
    own.receivables['custom-x'].original_minor=200000
    with pytest.raises(CashflowError):
        correct_receipt(own,rid,'custom-x',50000,own.as_of,'customer-x','Too much.',review_revision(own),ctx=ctx(own))
    correct_receipt(own,rid,'custom-x',10000,date(2027,1,3),'customer-x','Rechecked.',review_revision(own),ctx=ctx(own))
    with pytest.raises(CashflowError):more(own,{'custom-x':5000},day=date(2027,1,2))
    more(own,{'custom-x':5000},day=date(2027,1,3))
    assert own.actual_cash('USD',date(2027,1,3))==160000


def test_hold_accepts_actual_split_allocation_without_release(own):
    set_collection_hold(own,'custom-x',True,'customer-x','Dispute.',review_revision(own),ctx=ctx(own))
    imported(own);split(own)
    assert own.receivables['custom-x'].collection_hold['active']
    assert own.actual_cash('USD')==220000
    assert forecast(own,own.as_of,30).currencies['USD'].days[11].inflow_minor==0


@pytest.mark.parametrize('bad',['over_total','over_invoice','foreign_currency','missing_invoice','negative','bool','float','too_many','past','note','duplicate','agent','viewer','business','stale'])
def test_invalid_split_is_atomic(own,bad):
    imported(own,60000)
    assignments={'custom-x':40000,'custom-y':20000};caller=ctx(own);revision=review_revision(own);note='Checked source.'
    if bad=='over_total':assignments={'custom-x':50000,'custom-y':20000}
    if bad=='over_invoice':assignments={'custom-x':65001}
    if bad=='missing_invoice':assignments={'missing':100}
    if bad=='foreign_currency':own.receivables['custom-y'].currency='EUR';assignments={'custom-y':100};revision=review_revision(own)
    if bad in {'negative','bool','float'}:assignments={'custom-x':{'negative':-1,'bool':True,'float':1.5}[bad]}
    if bad=='too_many':assignments={str(i):1 for i in range(51)}
    if bad=='past':own.roll_forward(date(2027,1,2),ctx=ctx(own));revision=review_revision(own)
    if bad=='note':note=' '
    if bad=='duplicate':imported(own,60000,oid='same-source');revision=review_revision(own)
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':revision='stale'
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):confirm_split_observation(own,'bulk-one',assignments,note,False,revision,ctx=caller)
    assert vars(own)==before


def test_original_split_confirmation_survives_restart_and_new_corrections(own,tmp_path):
    imported(own);rev=review_revision(own);result=split(own,revision=rev)
    rid=result['allocation_ids'][0]
    correct_receipt(own,rid,'custom-x',0,own.as_of,'customer-x','Reverse allocation only.',review_revision(own),ctx=ctx(own))
    path=tmp_path/'restart.sqlite';save(path,own);loaded=_load(path);before=copy.deepcopy(vars(loaded))
    assert split(loaded,revision=rev)==dict(result,duplicate=True) and vars(loaded)==before
    assert loaded.actual_cash('USD')==220000 and effective_receipt(loaded,rid).amount_minor==0
    for role in ['agent','viewer']:
        with pytest.raises(AuthorizationError):split(loaded,revision=rev,caller=ctx(loaded,role))
    with pytest.raises(CashflowError):loaded.record_receipt(Receipt('bulk-one','custom-x',120000,loaded.as_of),ctx=ctx(loaded))
    with pytest.raises(CashflowError):allocate_observation(loaded,'bulk-one','custom-x','Checked.',True,review_revision(loaded),ctx=ctx(loaded))


def test_payment_preview_stale_after_unallocated_cash_changes_and_exports_match(own):
    original=_revision(own,'financial-inputs',[]);imported(own);split(own,{})
    assert _revision(own,'financial-inputs',[])!=original
    report=report_tables(own,ctx=ctx(own,'viewer'));summary=report['tables'][0]
    assert summary['headers'][-1]=='Unallocated customer receipts' and summary['rows'][0][-1].minor==120000
    events=report['tables'][4]['rows'];assert len(events)==1 and events[0][4].minor==120000
    assert xlsx_report(report).startswith(b'PK') and csv_archive(report).startswith(b'PK')


def test_daily_close_absorbs_parent_once_then_allocation_has_no_cash_effect(own):
    imported(own);split(own,{})
    preview=daily_preview(own,date(2027,1,2),ctx=ctx(own))
    assert preview['accounts'][0]['observed_inflow_minor']==120000
    close_day(own,date(2027,1,2),{'operating':{'opening_minor':220000,'evidence_id':'customer-x'}},
        'Checked opening.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==220000 and own.fact_checks['cash:operating']['reviewed_on']=='2027-01-02'
    more(own,{'custom-x':65000});assert daily_preview(own,date(2027,1,3),ctx=ctx(own))['accounts'][0]['observed_inflow_minor']==0


def test_source_checks_unknown_fresh_stale_and_changed_keep_finance_unchanged(own):
    report=data_freshness(own,ctx=ctx(own,'viewer'));assert {r['status'] for r in report['rows']}=={'unverified'}
    financial=_revision(own,'financial-inputs',[]);check(own)
    assert _revision(own,'financial-inputs',[])==financial
    row=data_freshness(own,ctx=ctx(own))['rows'][0]
    assert row['status']=='fresh' and row['age_days']==0
    assert data_freshness(own,date(2027,1,2),ctx=ctx(own))['rows'][0]['status']=='fresh'
    assert data_freshness(own,date(2027,1,3),ctx=ctx(own))['rows'][0]['status']=='stale'
    own.accounts['operating'].opening_minor+=1
    assert data_freshness(own,ctx=ctx(own))['rows'][0]['status']=='changed_since_check'
    assert own.actual_cash('USD')==100001


def test_collection_source_check_age_changed_source_missing_and_past_date(own):
    check(own,'collection','custom-x')
    rows=lambda ref=None:data_freshness(own,ref,ctx=ctx(own))['rows'][1]
    assert rows()['status']=='fresh' and rows(date(2027,1,9))['status']=='stale'
    own.receivables['custom-x'].expected_date=date(2027,1,13)
    assert rows()['status']=='changed_since_check'
    check(own,'collection','custom-x');del own.evidence['customer-x']
    assert rows()['status']=='source_missing'
    assert rows(date(2027,1,14))['status']=='date_needs_confirmation'


def test_confirm_date_records_a_source_check_but_does_not_create_a_receipt(own):
    confirm_collection_date(own,'custom-x',date(2027,1,13),'customer-x','Confirmed date.',review_revision(own),ctx=ctx(own))
    row=data_freshness(own,ctx=ctx(own))['rows'][1]
    assert row['status']=='fresh' and row['expected_date']=='2027-01-13'
    assert not own.receipts and own.actual_cash('USD')==100000 and not own.consent_map


@pytest.mark.parametrize('bad',['unknown_kind','unknown_item','source','note','stale','agent','viewer','business','past_date','unknown_date'])
def test_source_check_failures_are_atomic(own,bad):
    args=['cash','operating','customer-x','Checked.',review_revision(own)];caller=ctx(own)
    if bad=='unknown_kind':args[0]='payment'
    if bad=='unknown_item':args[1]='other'
    if bad=='source':args[2]='missing'
    if bad=='note':args[3]=' '
    if bad=='stale':args[4]='old'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad in {'past_date','unknown_date'}:
        own.receivables['custom-x'].expected_date=date(2026,12,31) if bad=='past_date' else None
        args[0:2]=['collection','custom-x'];args[-1]=review_revision(own)
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):confirm_fact_check(own,*args,ctx=caller)
    assert vars(own)==before


def test_source_check_exact_retry_cannot_refresh_newer_fact_after_restart(own,tmp_path):
    rev=review_revision(own);result=check(own,revision=rev)
    own.roll_forward(date(2027,1,2),ctx=ctx(own));check(own)
    path=tmp_path/'checked.sqlite';save(path,own);loaded=_load(path);before=copy.deepcopy(vars(loaded))
    assert check(loaded,revision=rev)==dict(result,duplicate=True) and vars(loaded)==before
    assert loaded.fact_checks['cash:operating']['reviewed_on']=='2027-01-02'


def test_affordability_uses_same_engine_protects_dates_and_never_mutates(own):
    before=copy.deepcopy(vars(own));result=purchase(own,10000)
    assert vars(own)==before and result['currency']=='USD'
    base=result['baseline'].currencies['USD'];after=result['scenario'].currencies['USD']
    assert after.closing_minor==base.closing_minor-10000
    assert result['baseline_already_breached'] and result['status']=='floor_breach'
    assert result['protected'][0]['id']=='staff' and result['protected'][0]['date']=='2027-01-05'
    assert not own.actions and not own.outflows['staff'].amendments


def test_affordability_numerical_headroom_and_source_requirements(own):
    own.accounts['operating'].opening_minor=300000
    first=purchase(own);assert first['status']=='source_review_required'
    for kind,ident in [('cash','operating'),('collection','custom-x'),('collection','custom-y')]:check(own,kind,ident)
    result=purchase(own);assert result['status']=='within_floor_under_assumptions'
    headroom=result['numerical_headroom_minor']
    assert purchase(own,headroom)['scenario'].currencies['USD'].min_balance_minor>=20000
    assert purchase(own,headroom+1)['status']=='floor_breach'


def test_affordability_same_day_collection_does_not_fund_outflow_first(own):
    own.outflows={};own.receivables['custom-x'].expected_date=own.as_of
    result=purchase(own,80001,own.as_of)
    after=result['scenario'].currencies['USD']
    assert after.days[0].intraday_low_minor==19999 and after.days[0].closing_minor==84999
    assert result['status']=='floor_breach'


def test_affordability_keeps_currencies_separate_and_missing_facts_visible(own):
    own.accounts['eur']=Account('eur','EUR',10000000,0)
    usd=purchase(own,100000);assert usd['status']=='floor_breach'
    assert usd['baseline'].currencies['EUR']==usd['scenario'].currencies['EUR']
    own.receivables['custom-x'].expected_date=None
    assert any('unknown' in s for s in purchase(own)['issues'])


@pytest.mark.parametrize('bad',['zero','negative','bool','float','too_large','before','after','unknown_currency','account_id_mismatch','uncovered','horizon','label','business'])
def test_affordability_invalid_inputs_are_readonly(own,bad):
    amount=10000;day=date(2027,1,15);ccy='USD';label='New stock';caller=ctx(own)
    if bad in {'zero','negative','bool','float','too_large'}:amount={'zero':0,'negative':-1,'bool':True,'float':1.5,'too_large':10**15+1}[bad]
    if bad=='before':day=date(2026,12,31)
    if bad=='after':day=date(2027,2,1)
    if bad=='unknown_currency':ccy='EUR'
    if bad=='account_id_mismatch':own.accounts['duplicate']=Account('different-id','USD',1,0)
    if bad=='uncovered':own.receivables['custom-x'].currency='JPY'
    if bad=='horizon':own.horizon_days=0
    if bad=='label':label=' '
    if bad=='business':caller=ctx(own,business='other')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):affordability(own,ccy,amount,day,label,ctx=caller)
    assert vars(own)==before


def test_new_metadata_backup_restore_and_old_empty_meta_compatibility(own,tmp_path):
    path=tmp_path/'recovery.sqlite';save(path,own)
    assert not _load(path).split_receipts and not _load(path).fact_checks
    imported(own);split(own);check(own);save(path,own)
    metadata=recovery.create_backup(path,'Split and source checks',ctx=ctx(own))
    assert metadata['app_version']=='0.17.1'
    more(own,{'custom-x':25000});save(path,own)
    preview=recovery.restore_preview(path,metadata['id'],ctx=ctx(own))
    assert preview['restored']['split_payments']==1 and preview['restored']['source_checks']==1
    # Actual restore is exercised against the engine's reviewed token.
    recovery.restore_backup(path,metadata['id'],preview['revision'],own.business_id,ctx=ctx(own))
    loaded=_load(path)
    assert split_unallocated(loaded,loaded.split_receipts['bulk-one'])==60000 and loaded.fact_checks


def test_http_pages_purchase_preview_and_confirmations_are_guarded(live_server,own):
    base,db=live_server;imported(own);save(db,own)
    before=copy.deepcopy(vars(_load(db)))
    for path in ['/affordability','/freshness','/split-receipts','/split-receipts?observation_id=bulk-one']:
        status,html=_get(base,path);assert status==200 and 'SYNTHETIC DEMO' in html
    assert vars(_load(db))==before
    import re
    token=re.search("name='csrf' value='([^']+)'",_get(base,'/affordability')[1]).group(1)
    fields=dict(csrf=token,label='<script>unsafe</script>',currency='USD',amount='100.00',date='2027-01-15')
    status,html=_post(base,'/affordability/preview',fields)
    assert status==200 and '&lt;script&gt;unsafe' in html and vars(_load(db))==before
    fields['csrf']='wrong';assert _post(base,'/affordability/preview',fields)[0]==403
    assert _post(base,'/freshness/confirm',dict(csrf=token,kind='cash',id='operating',evidence_id='customer-x',note='Checked.',revision=review_revision(own)))[0]==400
    fields=dict(csrf=token,confirmed='yes',observation_id='bulk-one',revision=review_revision(own),note='Checked source.',**{'allocate-custom-x':'650.00','allocate-custom-y':'450.00'})
    status,html=_post(base,'/split-receipts/confirm',fields)
    assert status==200 and 'Unallocated existing cash' in html
    loaded=_load(db);assert loaded.actual_cash('USD')==220000 and len(loaded.receipts)==2
    before=copy.deepcopy(vars(loaded))
    assert _post(base,'/split-receipts/confirm',fields)[0]==200 and vars(_load(db))==before
    assert _get(base,'/receipts/inbox')[0]==200
    assert _get(base,'/reports')[0]==200


def test_concurrent_split_confirmation_cash_once(live_server,own):
    base,db=live_server;imported(own);save(db,own)
    import re
    token=re.search("name='csrf' value='([^']+)'",_get(base,'/split-receipts?observation_id=bulk-one')[1]).group(1)
    fields=dict(csrf=token,confirmed='yes',observation_id='bulk-one',revision=review_revision(own),note='Checked source.',**{'allocate-custom-x':'400.00','allocate-custom-y':'200.00'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: _post(base,'/split-receipts/confirm',fields)[0],range(4)))
    assert results==[200]*4
    loaded=_load(db);assert loaded.actual_cash('USD')==220000 and len(loaded.receipts)==2
    assert len([o for o in loaded.owner_operations.values() if o['kind']=='split_payment_observed'])==1


def test_model_tool_surface_remains_read_or_draft_only(own):
    assert len(TOOLS)==8
    imported(own);split(own);check(own)
    result=execute_tool(ctx(own,'agent'),own,'get_evidence',{},provider_label='deterministic mock')
    assert result['result']['split_payments'][0]['amount_minor']==120000 and result['result']['source_checks']


@pytest.mark.parametrize('bad',['over_total','over_invoice','foreign_invoice','empty','past','source','note','agent','viewer','business','stale'])
def test_additional_allocation_rejects_invalid_changes_atomically(own,bad):
    imported(own);split(own)
    args=['bulk-one',{'custom-x':5000},own.as_of,'customer-x','Checked.',review_revision(own)];caller=ctx(own)
    if bad=='over_total':own.receivables['custom-x'].original_minor=500000;args[1]={'custom-x':70000};args[-1]=review_revision(own)
    if bad=='over_invoice':args[1]={'custom-x':25001}
    if bad=='foreign_invoice':own.receivables['custom-y'].currency='EUR';args[1]={'custom-y':100};args[-1]=review_revision(own)
    if bad=='empty':args[1]={}
    if bad=='past':args[2]=date(2026,12,31)
    if bad=='source':args[3]='missing'
    if bad=='note':args[4]=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':args[-1]='old'
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):allocate_split_balance(own,*args,ctx=caller)
    assert vars(own)==before


@pytest.mark.parametrize('reference',[date(2026,12,31),date(2028,1,3),'2027-01-01',True])
def test_freshness_invalid_reference_date_is_readonly(own,reference):
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):data_freshness(own,reference,ctx=ctx(own))
    assert vars(own)==before


def test_unallocated_cash_is_not_an_affordability_all_clear(own):
    own.accounts['operating'].opening_minor=300000
    imported(own);split(own,{})
    for kind,ident in [('cash','operating'),('collection','custom-x'),('collection','custom-y')]:check(own,kind,ident)
    result=purchase(own)
    assert result['status']=='source_review_required' and any('refund' in s for s in result['issues'])


def test_future_dated_parent_affects_cash_only_on_its_date(own):
    imported(own,day=date(2027,1,3));split(own)
    assert own.actual_cash('USD')==100000 and own.actual_cash('USD',date(2027,1,3))==220000
    rows=forecast(own,own.as_of,30).currencies['USD'].days
    assert rows[0].inflow_minor==0 and rows[2].inflow_minor==120000
    with pytest.raises(CashflowError):more(own,{'custom-x':1},day=date(2027,1,2))


def test_source_check_retry_requires_owner_before_idempotency(own):
    revision=review_revision(own);check(own,revision=revision)
    before=copy.deepcopy(vars(own))
    for role in ['agent','viewer']:
        with pytest.raises(AuthorizationError):check(own,revision=revision,caller=ctx(own,role))
    assert vars(own)==before
