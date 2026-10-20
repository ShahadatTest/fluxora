"""Due dates, conditional collections, reviewed recurrence and duplicate sources."""
import copy
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import operations as ops, web
from cashflow_copilot.adaptive import generate_proposals, validate_current_request
from cashflow_copilot.agent import TOOLS, execute_tool, ToolRejectedError
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import CashflowError, AuthorizationError, IdempotencyConflict
from cashflow_copilot.fixture import validate_fixture, load_fixture
from cashflow_copilot.forecast import forecast
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Account, Outflow, Receivable
from cashflow_copilot.owner_workflows import review_revision
from cashflow_copilot.planning import plan_draft_preview
from cashflow_copilot import recovery
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx, save, receipt, request
from tests.test_web import live_server, _get, _post, _mpost, _load


def terms(ledger,ident='custom-x',due=None,revision=None,caller=None,kind='invoice',party='Customer A',ref='INV-A'):
    return ops.item_identity(ledger,kind,ident,party,ref,'customer-x','Source terms checked.',revision or review_revision(ledger),
        ctx=caller or ctx(ledger),due_date=due)


def template(ledger,ident='rent',start=None,frequency='monthly',revision=None,caller=None,protected=False):
    return ops.create_recurring(ledger,ident,'Synthetic regular cost','USD',1000,start or ledger.as_of,None,frequency,
        protected,True,'Supplier A','supplier-source','Source recurrence terms checked.',revision or review_revision(ledger),ctx=caller or ctx(ledger))


@pytest.mark.parametrize('days,bucket',[(None,'unknown'),(-1,'not_due'),(0,'not_due'),(1,'1_30'),(30,'1_30'),(31,'31_60'),(60,'31_60'),(61,'61_90'),(90,'61_90'),(91,'over_90')])
def test_ageing_contractual_boundaries_do_not_infer_expected_date(own,days,bucket):
    due=own.as_of-timedelta(days=days) if days is not None else None
    terms(own,due=due)
    before=copy.deepcopy(vars(own))
    report=ops.collection_priorities(own,ctx=ctx(own,'viewer'))
    row=next(r for r in report['currencies']['USD']['invoices'] if r['id']=='custom-x')
    assert row['bucket']==bucket and row['due_date']==(due.isoformat() if due else None)
    assert own.receivables['custom-x'].expected_date==date(2027,1,12)
    assert report['currencies']['USD']['buckets'][bucket]>=65000
    assert vars(own)==before


def test_collection_priority_is_separate_deterministic_cash_impact_and_partial_balances(own):
    own.accounts['euro']=Account('euro','EUR',20000,0)
    own.receivables['eu']=Receivable('eu','EUR',10000,None,'customer-x')
    report=ops.collection_priorities(own,ctx=ctx(own))
    rows=report['currencies']['USD']['invoices']
    assert rows[0]['id']=='custom-x' and rows[0]['gap_reduction_minor']==60000
    assert rows[1]['gap_reduction_minor']==45000
    assert report['currencies']['EUR']['invoices'][0]['gap_reduction_minor']==0
    assert own.actual_cash('USD')==100000 and own.actual_cash('EUR')==20000
    receipt(own)
    report=ops.collection_priorities(own,ctx=ctx(own))
    assert next(r for r in report['currencies']['USD']['invoices'] if r['id']=='custom-x')['remaining_minor']==55000
    assert not own.actions and not own.consent_map


def test_expected_date_never_becomes_contractual_due_and_missing_account_refused(own):
    own.receivables['custom-x'].expected_date=date(2026,1,1)
    row=next(r for r in ops.collection_priorities(own,ctx=ctx(own))['currencies']['USD']['invoices'] if r['id']=='custom-x')
    assert row['bucket']=='unknown'
    own.receivables['unfunded']=Receivable('unfunded','EUR',1000,None,'customer-x')
    with pytest.raises(CashflowError):ops.collection_priorities(own,ctx=ctx(own))


@pytest.mark.parametrize('bad',['stale','agent','viewer','business','missing','bill_due','bad_date','long_party','source','note'])
def test_bad_source_terms_do_not_mutate(own,bad):
    args=['invoice','custom-x','Customer A','INV-A','customer-x','Checked source.',review_revision(own)]
    caller=ctx(own);due=date(2026,12,1)
    if bad=='stale':args[6]='old'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='elsewhere')
    if bad=='missing':args[1]='missing'
    if bad=='bill_due':args[0]='bill';args[1]='materials'
    if bad=='bad_date':due='2026-12-01'
    if bad=='long_party':args[2]='a'*201
    if bad=='source':args[4]='missing'
    if bad=='note':args[5]=' '
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):ops.item_identity(own,*args,ctx=caller,due_date=due)
    assert vars(own)==before


def test_source_terms_restart_exact_retry_and_clear_due(own,tmp_path):
    rev=review_revision(own);terms(own,due=date(2026,12,1),revision=rev)
    db=str(tmp_path/'terms.sqlite');save(db,own);loaded=_load(db)
    assert terms(loaded,due=date(2026,12,1),revision=rev)['duplicate']
    assert loaded.receivables['custom-x'].identity_evidence_id==loaded.invoice_due_sources['custom-x']
    with pytest.raises(AuthorizationError):terms(loaded,due=date(2026,12,1),revision=rev,caller=ctx(loaded,'agent'))
    terms(loaded,due=None)
    assert loaded.receivables['custom-x'].due_date is None and loaded.receivables['custom-x'].original_minor==65000


@pytest.mark.parametrize('year,febday',[(2027,28),(2028,29)])
def test_monthly_anchor_clamps_then_recovers_and_templates_have_no_financial_effect(own,year,febday):
    own.as_of=date(year,1,1);before=forecast(own,own.as_of,30)
    template(own,start=date(year,1,31),protected=True)
    assert forecast(own,own.as_of,30)==before and own.actual_cash('USD')==100000
    preview=ops.recurring_preview(own,'rent',date(year,3,31),ctx=ctx(own))
    assert [r['date'] for r in preview['rows']]==[f'{year}-01-31',f'{year}-02-{febday}',f'{year}-03-31']
    result=ops.confirm_recurring(own,'rent',date(year,3,31),False,preview['revision'],ctx=ctx(own))
    assert len(result['created_ids'])==3
    assert all(own.outflows[i].protected and own.outflows[i].counterparty_consent_required_for_change and own.outflows[i].status=='scheduled' for i in result['created_ids'])
    assert not own.receipts and not own.consent_map and own.actual_cash('USD')==100000


def test_weekly_confirm_extend_restart_pause_and_replay_never_recreate_or_repause(own,tmp_path):
    template(own,frequency='weekly')
    preview=ops.recurring_preview(own,'rent',date(2027,1,22),ctx=ctx(own))
    rev=preview['revision'];result=ops.confirm_recurring(own,'rent',date(2027,1,22),False,rev,ctx=ctx(own))
    assert len(result['created_ids'])==4
    db=str(tmp_path/'weekly.sqlite');save(db,own);loaded=_load(db)
    assert ops.confirm_recurring(loaded,'rent',date(2027,1,22),False,rev,ctx=ctx(loaded))['duplicate']
    result=ops.confirm_recurring(loaded,'rent',date(2027,2,5),False,review_revision(loaded),ctx=ctx(loaded))
    assert len(result['created_ids'])==2 and len(loaded.recurring_occurrences)==6
    pause_rev=review_revision(loaded);ops.pause_recurring(loaded,'rent',False,pause_rev,ctx=ctx(loaded))
    assert ops.recurring_preview(loaded,'rent',date(2027,3,1),ctx=ctx(loaded))['rows']==[]
    ops.pause_recurring(loaded,'rent',True,review_revision(loaded),ctx=ctx(loaded))
    before=copy.deepcopy(vars(loaded))
    assert ops.pause_recurring(loaded,'rent',False,pause_rev,ctx=ctx(loaded))['duplicate']
    assert vars(loaded)==before
    loaded.roll_forward(date(2027,2,10),ctx=ctx(loaded))
    dates=[r['date'] for r in ops.recurring_preview(loaded,'rent',date(2027,2,26),ctx=ctx(loaded))['rows']]
    assert dates==['2027-02-12','2027-02-19','2027-02-26']


@pytest.mark.parametrize('bad',['id','duplicate_id','amount','bool_amount','currency','past','far','end','frequency','bool_flag','source','stale','agent','business'])
def test_bad_recurring_template_has_no_effect(own,bad):
    args=['rent','Regular cost','USD',1000,own.as_of,None,'monthly',False,True,'Supplier A','supplier-source','Checked source.',review_revision(own)]
    caller=ctx(own)
    if bad=='id':args[0]='../x'
    if bad=='duplicate_id':template(own);args[-1]=review_revision(own)
    if bad=='amount':args[3]=0
    if bad=='bool_amount':args[3]=True
    if bad=='currency':args[2]='EUR'
    if bad=='past':args[4]=date(2026,12,31)
    if bad=='far':args[4]=date(2029,1,1)
    if bad=='end':args[5]=date(2026,12,31)
    if bad=='frequency':args[6]='daily'
    if bad=='bool_flag':args[7]='false'
    if bad=='source':args[10]='missing'
    if bad=='stale':args[-1]='old'
    if bad=='agent':caller=ctx(own,'agent')
    if bad=='business':caller=ctx(own,business='different')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):ops.create_recurring(own,*args,ctx=caller)
    assert vars(own)==before


def test_recurring_duplicate_warning_acknowledgement_collision_and_end(own):
    own.outflows['existing-rent']=Outflow('existing-rent','USD',1000,own.as_of,False,'supplier-source',counterparty='Supplier A')
    template(own);preview=ops.recurring_preview(own,'rent',own.as_of,ctx=ctx(own))
    assert preview['rows'][0]['duplicate_warnings'][0]['id']=='existing-rent'
    with pytest.raises(CashflowError):ops.confirm_recurring(own,'rent',own.as_of,False,preview['revision'],ctx=ctx(own))
    result=ops.confirm_recurring(own,'rent',own.as_of,True,preview['revision'],ctx=ctx(own))
    oid=result['created_ids'][0];own.recurring_occurrences.pop(oid)
    with pytest.raises(IdempotencyConflict):ops.recurring_preview(own,'rent',own.as_of,ctx=ctx(own))
    own.outflows.pop(oid);own.recurring_bills['rent']['end']='2027-01-15'
    assert len(ops.recurring_preview(own,'rent',date(2027,3,1),ctx=ctx(own))['rows'])==1


def test_recurring_stale_confirm_and_unauthorized_replay_refused(own):
    template(own);preview=ops.recurring_preview(own,'rent',date(2027,1,31),ctx=ctx(own))
    terms(own)
    with pytest.raises(CashflowError):ops.confirm_recurring(own,'rent',date(2027,1,31),False,preview['revision'],ctx=ctx(own))
    rev=review_revision(own);ops.confirm_recurring(own,'rent',date(2027,1,31),False,rev,ctx=ctx(own))
    with pytest.raises(AuthorizationError):ops.confirm_recurring(own,'rent',date(2027,1,31),False,rev,ctx=ctx(own,'agent'))
    with pytest.raises(CashflowError):ops.recurring_preview(own,'rent',date(2029,1,1),ctx=ctx(own))


def duplicate(ledger):
    ledger.receivables['another-id']=Receivable('another-id','USD',66000,date(2027,1,15),'customer-y',counterparty=' CUSTOMER   A ',invoice_reference='inv-a')
    terms(ledger)
    return ops.duplicate_pairs(ledger,ctx=ctx(ledger))[0]


def test_duplicate_strong_reference_with_conflicting_amount_and_source_freshness(own,tmp_path):
    pair=duplicate(own)
    assert 'amounts/dates may conflict' in pair['reason']
    rev=review_revision(own);before=own.actual_cash('USD')
    ops.review_duplicate(own,pair['id'],'distinct','customer-x','Checked: different real source obligations.',rev,ctx=ctx(own))
    db=str(tmp_path/'duplicate.sqlite');save(db,own);loaded=_load(db)
    assert ops.review_duplicate(loaded,pair['id'],'distinct','customer-x','Checked: different real source obligations.',rev,ctx=ctx(loaded))['duplicate']
    assert own.actual_cash('USD')==before and len(own.receivables)==3
    terms(loaded)  # new source attestation invalidates the previous pair fingerprint
    assert ops.duplicate_pairs(loaded,ctx=ctx(loaded))[0]['review'] is None
    with pytest.raises(CashflowError):ops.review_duplicate(loaded,pair['id'],'distinct','customer-x','Old pair source.',review_revision(loaded),ctx=ctx(loaded))


@pytest.mark.parametrize('change,expected',[('same',True),('currency',False),('kind',False),('known_other_party',False),('unknown_date',False),('different_amount',False)])
def test_duplicate_heuristic_bounds(change,expected):
    a=dict(kind='bill',id='a',currency='USD',amount_minor=1000,date='2027-01-01',counterparty='',invoice_reference='')
    b=dict(a,id='b')
    if change=='currency':b['currency']='EUR'
    if change=='kind':b['kind']='invoice'
    if change=='known_other_party':a['counterparty']='A';b['counterparty']='B'
    if change=='unknown_date':a['date']=None
    if change=='different_amount':b['amount_minor']=1001
    assert bool(ops.duplicate_reason(a,b))==expected


def test_confirmed_duplicate_keeps_records_blocks_sufficient_plans_and_target_approval(own):
    pair=duplicate(own);ops.review_duplicate(own,pair['id'],'confirmed_duplicate','customer-x','Same external obligation was imported twice.',review_revision(own),ctx=ctx(own))
    assert len(own.receivables)==3 and all(not p.financially_sufficient for p in generate_proposals(own,ctx=ctx(own)))
    with pytest.raises(CashflowError):plan_draft_preview(own,'auto-collections',ctx=ctx(own))
    action=request(own)
    with pytest.raises(CashflowError):validate_current_request(own,action)
    ops.review_duplicate(own,pair['id'],'distinct','customer-y','Further source inspection resolved the reference collision.',review_revision(own),ctx=ctx(own))
    assert any(p.financially_sufficient for p in generate_proposals(own,ctx=ctx(own)))


def test_old_duplicate_verdict_retry_cannot_revive_superseded_verdict(own):
    pair=duplicate(own);rev=review_revision(own)
    args=(pair['id'],'confirmed_duplicate','customer-x','Same source detected.',rev)
    ops.review_duplicate(own,*args,ctx=ctx(own))
    ops.review_duplicate(own,pair['id'],'distinct','customer-y','Source review revised.',review_revision(own),ctx=ctx(own))
    before=copy.deepcopy(vars(own))
    assert ops.review_duplicate(own,*args,ctx=ctx(own))['duplicate']
    assert vars(own)==before and own.duplicate_reviews[pair['id']]['verdict']=='distinct'
    with pytest.raises(AuthorizationError):ops.review_duplicate(own,*args,ctx=ctx(own,'agent'))


@pytest.mark.parametrize('bad',['agent','viewer','business','source','verdict','stale'])
def test_invalid_duplicate_review_no_mutation(own,bad):
    pair=duplicate(own);caller=ctx(own);source='customer-x';verdict='distinct';rev=review_revision(own)
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='different')
    if bad=='source':source='missing'
    if bad=='verdict':verdict='delete'
    if bad=='stale':rev='old'
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):ops.review_duplicate(own,pair['id'],verdict,source,'Checked source.',rev,ctx=caller)
    assert vars(own)==before


def test_fixture_optional_metadata_validates_and_preserves_unknown_due():
    data=load_fixture();data['receivables'][0].update(due_date='2026-10-01',counterparty='Synthetic Customer',invoice_reference='X-1')
    ledger=Ledger.from_fixture(validate_fixture(data));rec=next(iter(ledger.receivables.values()))
    assert rec.due_date==date(2026,10,1) and rec.invoice_reference=='X-1'
    data['receivables'][0]['due_date']='bad'
    with pytest.raises(CashflowError):validate_fixture(data)


def csv_preview(base,content):
    status,html=_mpost(base,'/ledger/import/csv',{'csrf':web.CSRF_TOKEN},{'file':('records.csv',content.encode(),'text/csv')})
    assert status==200
    token=re.search(r'name="token" value="([^"]+)"',html).group(1)
    names=['record_type','reference','amount_major','currency','date','counterparty','note','due_date','invoice_reference']
    fields=dict(csrf=web.CSRF_TOKEN,token=token,**{f'col_{i}':v for i,v in enumerate(names)})
    status,html=_post(base,'/ledger/import/csv/preview',fields);assert status==200
    token=re.search(r'name="token" value="([^"]+)"',html).group(1)
    return token,html


def test_http_csv_different_id_duplicates_skip_keep_intrabatch_and_stale_preview(own,live_server):
    base,db=live_server;save(db,own)
    content='record_type,reference,amount_major,currency,date,counterparty,note,due_date,invoice_reference\n'
    content+='invoice,extra-a,650.00,USD,2027-01-12,,Synthetic extra,2026-12-01,EXT1\n'
    token,html=csv_preview(base,content)
    assert 'Possible different-ID duplicate' in html
    assert _post(base,'/ledger/import/confirm',dict(csrf=web.CSRF_TOKEN,token=token))[0]==400
    token,_=csv_preview(base,content)
    assert _post(base,'/ledger/import/confirm',dict(csrf=web.CSRF_TOKEN,token=token,skip_row_2='yes'))[0]==200
    assert 'extra-a' not in _load(db).receivables
    token,_=csv_preview(base,content)
    assert _post(base,'/ledger/import/confirm',dict(csrf=web.CSRF_TOKEN,token=token,acknowledge_duplicates='yes'))[0]==200
    loaded=_load(db);assert loaded.receivables['extra-a'].due_date==date(2026,12,1)
    assert loaded.receivables['extra-a'].expected_date==date(2027,1,12)
    token,_=csv_preview(base,content.replace('extra-a','extra-b'))
    terms(loaded);save(db,loaded)
    assert _post(base,'/ledger/import/confirm',dict(csrf=web.CSRF_TOKEN,token=token,acknowledge_duplicates='yes'))[0]==400
    assert 'extra-b' not in _load(db).receivables
    rows=[dict(record_type='bill',reference=x,currency='USD',amount_minor=1000,date='2027-02-01',counterparty='S',invoice_reference='',will_skip=False) for x in ['new-a','new-b']]
    assert ops.csv_duplicate_warnings(own,rows)[1]['possible_duplicates'][0]['id']=='new-a'


def test_http_pages_csrf_explicit_confirmation_concurrent_occurrence_creation(own,live_server):
    base,db=live_server;template(own);save(db,own)
    for path in ['/collections','/duplicates','/recurring','/records?kind=invoice&item_id=custom-x','/recurring/rent/preview']:
        assert _get(base,path)[0]==200
    fields=dict(csrf=web.CSRF_TOKEN,confirmed='yes',through='2027-03-01',revision=review_revision(own))
    assert _post(base,'/recurring/rent/confirm',dict(fields,csrf='bad'))[0]==403
    assert _post(base,'/recurring/rent/confirm',dict(fields,confirmed='no'))[0]==400
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(r[0]==200 for r in pool.map(lambda _: _post(base,'/recurring/rent/confirm',fields),range(2)))
    assert len(_load(db).recurring_occurrences)==3


def test_csv_standard_optional_header_mapping_does_not_confuse_dates_or_reference(live_server):
    base,_=live_server
    csv='record_type,reference,amount_major,currency,date,counterparty,note,due_date,invoice_reference\ninvoice,a,10,USD,2027-01-01,A,note,2026-12-01,EXT-A\n'
    status,html=_mpost(base,'/ledger/import/csv',{'csrf':web.CSRF_TOKEN},{'file':('sample.csv',csv.encode(),'text/csv')})
    assert status==200
    for index,name in [(1,'reference'),(4,'date'),(7,'due_date'),(8,'invoice_reference')]:
        select=re.search(rf"<select name='col_{index}'>(.*?)</select>",html,re.S).group(1)
        assert f"value='{name}' selected" in select


def test_model_cannot_invoke_owner_operations(own):
    assert len(TOOLS)==8
    for name in ['item_identity','create_recurring','confirm_recurring','pause_recurring','review_duplicate']:
        with pytest.raises(ToolRejectedError):execute_tool(ctx(own,'agent'),own,name,{})


def test_backup_restores_new_metadata_and_recurring_idempotency(own,tmp_path):
    terms(own,due=date(2026,12,1));template(own)
    rev=review_revision(own);ops.confirm_recurring(own,'rent',date(2027,2,1),False,rev,ctx=ctx(own))
    db=str(tmp_path/'recovery.sqlite');save(db,own);backup=recovery.create_backup(db,'Operations',ctx=ctx(own))
    changed=_load(db);ops.pause_recurring(changed,'rent',False,review_revision(changed),ctx=ctx(changed));save(db,changed)
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(changed))
    recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(changed))
    restored=_load(db)
    assert restored.recurring_bills['rent']['active'] and restored.receivables['custom-x'].due_date==date(2026,12,1)
    assert ops.confirm_recurring(restored,'rent',date(2027,2,1),False,rev,ctx=ctx(restored))['duplicate']
