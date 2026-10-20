"""Collection stress, reviewed statement mapping and outbound disclosure controls.

Adapters in this file are labelled test doubles or loopback HTTP stubs, not live models.
"""
import copy
from datetime import date, timedelta
import json
import re
import threading

import pytest

from cashflow_copilot import agent,agent_web,recovery,web
from cashflow_copilot.agent_disclosure import disclosure_preview,disclosure_revision,parse_usage
from cashflow_copilot.agent_requests import run_request,request_status
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.completion import statement_report
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.exploration import compound_scenario,save_scenario
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account,Document
from cashflow_copilot.obligations import check_statement_balance,statement_balance_report
from cashflow_copilot.owner_workflows import review_revision
from cashflow_copilot.model_adapter import AdapterResult,AdapterStatus,ToolCall,OpenAICompatibleAdapter
from cashflow_copilot.statement_mapping import DEFAULT_MAPPING,normalize_statement,import_mapped_statement
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx,save,receipt
from tests.test_web import live_server,_get,_post,_mpost,_load
from tests.test_model_adapter import stub,_StubHandler,_completion,CANARY_KEY


def collection(own,percent=50,delays=None):
    return compound_scenario(own,delays or [],[],collections=[dict(invoice_id='custom-x',percent=percent)],ctx=ctx(own))


@pytest.mark.parametrize('percent',[0,1,33,50,99,100])
def test_downside_is_exact_unpaid_remainder_only_and_read_only(own,percent):
    receipt(own,amount=10001)
    before=copy.deepcopy(vars(own));r=collection(own,percent)
    # Independent integer reference: opening + observed receipt + assumed remainder + other invoice - all bills.
    expected=(65000-10001)*percent//100
    assert r['collection_rows'][0]['assumed_minor']==expected
    assert r['scenario'].currencies['USD'].closing_minor==100000+10001+expected+45000-140000
    assert sum(d.inflow_minor for d in r['scenario'].currencies['USD'].days)==10001+expected+45000
    assert r['baseline'].currencies['USD'].closing_minor==70000
    assert vars(own)==before and own.receivables['custom-x'].remaining_minor==54999
    assert 'probability' in ' '.join(r['assumptions']) and not own.actions


def test_collection_and_delay_compose_without_later_residual_or_currency_netting(own):
    own.accounts['eur']=Account('eur','EUR',1234,0)
    r=collection(own,50,[dict(invoice_id='custom-x',delay_days=30)])
    assert r['scenario'].currencies['USD'].closing_minor==5000
    assert r['scenario'].currencies['EUR'].closing_minor==1234
    assert not r['floors_hold']
    assert r['scenario'].currencies['USD'].days[4].outflow_minor==70000 # protected payroll retained


@pytest.mark.parametrize('percent',[0,50,100])
def test_existing_read_tool_computes_stress_and_keeps_sources_and_claims(own,percent):
    own.receivables['custom-x'].expected_date=date(2027,1,7)
    before=copy.deepcopy(vars(own))
    result=agent.execute_tool(ctx(own,'agent'),own,'compute_forecast',dict(scenario=dict(receivable_id='custom-x',collection_percent=percent)))['result']
    assert result['currencies']['USD']['closing_minor']==5000+65000*percent//100
    causes=[r for r in result['currencies']['USD']['breach_causes'] if r['id']=='custom-x']
    assert (causes[0]['amount_minor'] if causes else 0)==65000*percent//100
    assert result['collection_stress'][0]['evidence_ids']==['customer-x']
    assert {k:v for k,v in vars(own).items() if k!='audit'}=={k:v for k,v in before.items() if k!='audit'}


def test_stress_read_tool_needs_explicit_invoice_and_rejects_held_or_negative_shift(own):
    for scenario in [dict(collection_percent=50),dict(receivable_id='custom-x',collection_percent=50,shift_receivable_days=-1)]:
        with pytest.raises(CashflowError):agent.execute_tool(ctx(own),own,'compute_forecast',dict(scenario=scenario))


def test_public_provider_labels_never_reveal_url_credentials_or_key_canary():
    a=OpenAICompatibleAdapter(api_key=CANARY_KEY,model='model-'+CANARY_KEY,base_url='https://user:password@example.test/v1?secret='+CANARY_KEY)
    surface=json.dumps(vars(a.status()))+repr(a)+a.provider_label
    assert CANARY_KEY not in surface and 'password' not in surface


def test_mock_cash_review_does_not_invent_breach_account_or_absent_receipts(own):
    from cashflow_copilot.model_adapter import MockProviderAdapter
    own.accounts['operating'].opening_minor=500000
    own.accounts['eur']=Account('eur','EUR',1234,0)
    receipt(own,amount=10000)
    run=agent.run_agent_question(own,'Why will we breach our cash floor?',MockProviderAdapter(),data_mode='minimized')
    assert 'No USD floor breach' in run.answer and 'EUR separately' in run.answer
    assert 'usd-main' not in run.answer and 'none recorded in this scenario' not in run.answer
    assert 'will be breached' not in run.answer and own.receipts


@pytest.mark.parametrize('bad',[True,-1,101,0.5,'50',None])
def test_invalid_collection_percent_is_rejected_without_state_change(own,bad):
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):collection(own,bad)
    assert vars(own)==before


@pytest.mark.parametrize('bad',['held','unknown','past','settled','missing','duplicate','extra','wrong_business'])
def test_downside_cannot_turn_missing_or_held_sources_into_cash(own,bad):
    changes=[dict(invoice_id='custom-x',percent=50)];caller=ctx(own)
    if bad=='held':own.receivables['custom-x'].collection_hold=dict(active=True,evidence_id='customer-x')
    if bad=='unknown':own.receivables['custom-x'].expected_date=None
    if bad=='past':own.receivables['custom-x'].expected_date=own.as_of-timedelta(days=1)
    if bad=='settled':receipt(own,amount=65000)
    if bad=='missing':changes[0]['invoice_id']='missing'
    if bad=='duplicate':changes*=2
    if bad=='extra':changes[0]['approve']=True
    if bad=='wrong_business':caller=ctx(own,business='other')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):compound_scenario(own,[],[],collections=changes,ctx=caller)
    assert vars(own)==before


def normalized(own,data=b'id,date,currency,amount,reference\nr1,2027-01-03,USD,100.25,source\n',mapping=None,start='2027-01-01',end='2027-01-31'):
    return normalize_statement(own,data,mapping or DEFAULT_MAPPING,'operating',start,end,ctx=ctx(own))


def mapped(own,n=None,name='My synthetic format',revision=None,caller=None):
    return import_mapped_statement(own,n or normalized(own),'Checked source account, dates and interpretation.',name,
        revision or review_revision(own),ctx=caller or ctx(own))


def test_mapped_debit_credit_date_decimal_and_ignored_columns(own):
    m=dict(DEFAULT_MAPPING,delimiter=';',date_format='dmy',decimal=',',mode='debit_credit',
        id_column='',date_column='Posted',currency_column='',reference_column='Memo',debit_column='Debit',credit_column='Credit')
    data=b'Posted;Memo;Debit;Credit;Unused\n03/01/2027;A;12,34;;ignore\n04/01/2027;B;;100,05;other\n'
    n=normalized(own,data,m)
    assert n['ignored_columns']==['Unused']
    assert n['rows']==[dict(id='row-001',date='2027-01-03',currency='USD',signed_minor=-1234,reference='A'),dict(id='row-002',date='2027-01-04',currency='USD',signed_minor=10005,reference='B')]
    before=own.actual_cash('USD');bid=mapped(own,n)['batch_id'];report=statement_report(own,bid,ctx=ctx(own))
    assert report['account_scope']=='operating' and not report['account_scope_required']
    assert own.actual_cash('USD')==before and not own.receipts and not own.actions
    p=next(iter(own.statement_profiles.values()))
    assert p['mapping']==m and 'account_id' not in p and 'start' not in p


def test_us_dates_are_explicit_not_guessed(own):
    data=b'id,date,currency,amount,reference\nr1,01/03/2027,USD,1.00,A\n'
    m=dict(DEFAULT_MAPPING,date_format='mdy')
    assert normalized(own,data,m)['rows'][0]['date']=='2027-01-03'
    with pytest.raises(CashflowError):normalized(own,data,dict(m,date_format='dmy'))


@pytest.mark.parametrize('amount',['1,000.00','1e3','NaN','(1.00)','USD 1','1.001','0','-0.00'])
def test_statement_refuses_inferred_grouping_rounding_and_zero_transactions(own,amount):
    data=f'id;date;currency;amount;reference\nr1;2027-01-03;USD;{amount};source\n'.encode()
    with pytest.raises(CashflowError):normalized(own,data,dict(DEFAULT_MAPPING,delimiter=';'))


@pytest.mark.parametrize('bad',['missing_header','duplicate_header','extra_cell','currency','outside_period','invalid_date','both_sides','duplicate_mapping','over_limit','blank_file'])
def test_mapped_statement_bad_inputs_do_not_mutate(own,bad):
    m=dict(DEFAULT_MAPPING);data=b'id,date,currency,amount,reference\nr1,2027-01-03,USD,1.00,source\n'
    if bad=='missing_header':m['date_column']='Posted'
    if bad=='duplicate_header':data=b'id,date,currency,amount,amount\n'
    if bad=='extra_cell':data+=b'r2,2027-01-03,USD,1.00,source,extra\n'
    if bad=='currency':data=data.replace(b'USD',b'EUR')
    if bad=='outside_period':data=data.replace(b'2027-01-03',b'2027-02-01')
    if bad=='invalid_date':data=data.replace(b'2027-01-03',b'2027-02-30')
    if bad=='both_sides':m.update(mode='debit_credit',debit_column='amount',credit_column='reference');data=data.replace(b',source',b',2.00')
    if bad=='duplicate_mapping':m['reference_column']='date'
    if bad=='over_limit':data=b'id,date,currency,amount,reference\n'+b'r1,2027-01-03,USD,1.00,A\n'*101
    if bad=='blank_file':data=b''
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):normalized(own,data,m)
    assert vars(own)==before


def test_zero_activity_explicit_period_flags_observed_activity_and_checks_boundaries(own):
    receipt(own,amount=10000)
    n=normalized(own,b'id,date,currency,amount,reference\n');bid=mapped(own,n)['batch_id']
    r=statement_report(own,bid,ctx=ctx(own))
    assert r['rows']==[] and r['ledger_movements_without_match'][0]['signed_minor']==10000
    check_statement_balance(own,'zero-check',bid,'operating',date(2027,1,1),date(2027,1,31),100000,100000,'','customer-x','Checked source boundaries.',review_revision(own),ctx=ctx(own))
    report=statement_balance_report(own,'zero-check',ctx=ctx(own))
    assert report['arithmetic_delta_minor']==0 and report['closing_delta_minor']==-10000
    assert report['ledger_movements_without_match'] if 'ledger_movements_without_match' in report else report['problems']
    with pytest.raises(CashflowError):check_statement_balance(own,'wrong-period',bid,'operating',date(2027,1,2),date(2027,1,31),100000,100000,'','customer-x','Checked.',review_revision(own),ctx=ctx(own))


@pytest.mark.parametrize('role',['agent','viewer','wrong_business'])
def test_profile_import_owner_business_gate_precedes_replay(own,role):
    n=normalized(own);rev=review_revision(own);mapped(own,n,revision=rev)
    caller=ctx(own,role) if role!='wrong_business' else ctx(own,business='other')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):mapped(own,n,revision=rev,caller=caller)
    assert vars(own)==before


def test_profile_and_scenario_survive_native_backup_and_restart_and_replay(own,tmp_path):
    n=normalized(own);rev=review_revision(own);first=mapped(own,n,revision=rev)
    collection_rows=[dict(invoice_id='custom-x',percent=0)]
    srev=review_revision(own);saved=save_scenario(own,'No collection',[],[],srev,ctx=ctx(own),collections=collection_rows)
    db=tmp_path/'state.sqlite';save(db,own)
    b=recovery.create_backup(db,'New feature checkpoint.',ctx=ctx(own));pv=recovery.restore_preview(db,b['id'],ctx=ctx(own))
    recovery.restore_backup(db,b['id'],pv['revision'],own.business_id,ctx=ctx(own))
    loaded=_load(db);assert loaded.statement_profiles==own.statement_profiles
    assert mapped(loaded,n,revision=rev)['batch_id']==first['batch_id']
    assert save_scenario(loaded,'No collection',[],[],srev,ctx=ctx(loaded),collections=collection_rows)['scenario_id']==saved['scenario_id']
    assert loaded.scenarios[saved['scenario_id']]['collections']==collection_rows and len(loaded.statement_batches)==1


def test_reinterpreting_same_file_or_named_profile_is_not_silent(own):
    n=normalized(own);mapped(own,n)
    changed=copy.deepcopy(n);changed['rows'][0]['signed_minor']=999
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):mapped(own,changed)
    assert vars(own)==before
    other=normalized(own,b'id,date,currency,amount,reference\nr2,2027-01-04,USD,1.00,X\n')
    other['context']['mapping']['date_format']='dmy'
    with pytest.raises(CashflowError):mapped(own,other)
    assert vars(own)==before


class UsageAdapter:
    provider_label='scripted usage regression adapter; not a live model'
    def __init__(self,usage=True):self.messages=[];self.report=usage
    def status(self):return AdapterStatus('mock',self.provider_label,None,'')
    def chat(self,messages,tools,*,deadline=None):
        self.messages.append(copy.deepcopy(messages))
        usage=dict(prompt_tokens=10,completion_tokens=2,total_tokens=12) if self.report else None
        if len(self.messages)==1:return AdapterResult(tool_calls=[ToolCall('read','get_evidence',{})],usage=usage)
        return AdapterResult(text='Scripted commentary; financial figures remain in deterministic results.',usage=usage)


@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_disclosure_preview_and_wire_filter_keep_authoritative_local_results(own,mode,tmp_path):
    own.receivables['custom-x'].counterparty='PRIVATE CUSTOMER LABEL'
    own.evidence['customer-x'].summary='PRIVATE SOURCE SUMMARY'
    own.documents['doc']=Document('doc','text','RAW ORIGINAL CONTENT')
    before=copy.deepcopy(vars(own));p=disclosure_preview(own,'Why the gap?',mode,ctx=ctx(own))
    assert vars(own)==before and len(p['tool_definitions'])==8
    assert 'RAW ORIGINAL CONTENT' not in json.dumps(p)
    content=json.dumps(p['current_read_tool_samples'])
    assert ('PRIVATE SOURCE SUMMARY' in content)==(mode=='full_metadata')
    assert ('PRIVATE CUSTOMER LABEL' in content)==(mode=='full_metadata')
    assert 'USD 650.00' in content and 'customer-x' in content
    a=UsageAdapter();db=tmp_path/'runs.sqlite'
    result=agent.run_agent_question(own,'Why the gap?',a,data_mode=mode,ctx=ctx(own),db_path=db)
    wire=a.messages[1][-1]['content']
    assert ('PRIVATE SOURCE SUMMARY' in wire)==(mode=='full_metadata')
    assert 'PRIVATE SOURCE SUMMARY' in json.dumps(result.tool_calls[0]['result'])
    assert result.tool_calls[0]['result']['receivables'][0]['original_minor']==65000
    assert result.usage['reported_subtotals']==dict(prompt_tokens=20,completion_tokens=4,total_tokens=24)
    assert result.usage['complete'] and result.tool_calls[0]['included_in_request_attempt']
    saved=agent.list_agent_runs(db)[0];assert saved['disclosure']['mode']==mode and saved['usage']==result.usage


def test_missing_usage_is_unknown_not_zero_and_request_policy_is_durable(own,tmp_path):
    db=tmp_path/'requests.sqlite';save(db,own);a=UsageAdapter(False)
    args=(db,'a'*32,'Why the gap?','mock',a)
    r=run_request(*args,ctx=ctx(own),lock=web.LOCK,data_mode='minimized',expected_revision=disclosure_revision(own))
    assert not r.result.usage['complete'] and r.result.usage['reported_subtotals']=={}
    assert request_status(db,'a'*32,ctx=ctx(own),lock=web.LOCK).result.usage==r.result.usage
    assert run_request(*args,ctx=ctx(own),lock=web.LOCK,data_mode='minimized').replayed and len(a.messages)==2
    with pytest.raises(CashflowError):run_request(*args,ctx=ctx(own),lock=web.LOCK,data_mode='full_metadata')


def test_usage_retained_when_measured_response_attempts_forbidden_tool(own,tmp_path):
    class Forbidden(UsageAdapter):
        def chat(self,*args,**kwargs):return AdapterResult(tool_calls=[ToolCall('pay','pay',{})],usage=dict(prompt_tokens=7,completion_tokens=2,total_tokens=9))
    db=tmp_path/'forbidden.sqlite';save(db,own)
    result=run_request(db,'a'*32,'Do not move money.','mock',Forbidden(),ctx=ctx(own),lock=web.LOCK,data_mode='minimized')
    assert result.status=='failed' and not _load(db).actions
    assert result.result.usage['reported_subtotals']['total_tokens']==9
    assert result.result.disclosure['mode']=='minimized' and agent.list_agent_runs(db)[0]['usage']==result.result.usage


@pytest.mark.parametrize('usage',[None,{},'raw secret',{'prompt_tokens':True},{'total_tokens':-1},{'total_tokens':'12'},{'total_tokens':10**13},{'prompt_tokens':1,'completion_tokens':1,'total_tokens':99}])
def test_usage_parser_does_not_invent_counts_or_accept_provider_text(usage):
    assert parse_usage(dict(usage=usage)) is None


def test_http_stub_usage_allowlist_and_error_body_canary_never_persist(own,stub,tmp_path):
    payload=_completion('Local HTTP stub answer, not a live model.')
    payload['usage']=dict(prompt_tokens=7,completion_tokens=3,total_tokens=10,error_message=CANARY_KEY)
    _StubHandler.responses=[(200,payload),(401,dict(error=dict(message=CANARY_KEY),usage=dict(total_tokens=1)))]
    adapter=OpenAICompatibleAdapter(api_key=CANARY_KEY,base_url=stub,model='local-stub',max_retries=0)
    db=tmp_path/'http-runs.sqlite'
    r=agent.run_agent_question(own,'Question',adapter,data_mode='minimized',db_path=db)
    assert r.usage['reported_subtotals']==dict(prompt_tokens=7,completion_tokens=3,total_tokens=10)
    failed=agent.run_agent_question(own,'Question',adapter,data_mode='minimized',db_path=db)
    assert failed.outcome=='provider_error' and failed.usage['reported_subtotals']=={}
    assert CANARY_KEY not in json.dumps(agent.list_agent_runs(db))
    assert len(_StubHandler.requests)==2


def test_http_stub_receives_only_selected_context_and_reports_partial_usage(own,stub,tmp_path):
    own.receivables['custom-x'].counterparty='CONFIDENTIAL PARTY'
    own.evidence['customer-x'].summary='CONFIDENTIAL SOURCE'
    first=_completion(tool_calls=[dict(id='read',type='function',function=dict(name='get_evidence',arguments='{}'))])
    first['usage']={'prompt_tokens':11}
    final=_completion('Loopback HTTP stub commentary.')
    _StubHandler.responses=[(200,first),(200,final)]
    adapter=OpenAICompatibleAdapter(api_key=CANARY_KEY,base_url=stub,model='local-stub',max_retries=0)
    result=agent.run_agent_question(own,'Why the gap?',adapter,data_mode='minimized')
    assert len(_StubHandler.requests)==2
    wire=json.dumps(_StubHandler.requests[1]['body'])
    assert 'CONFIDENTIAL PARTY' not in wire and 'CONFIDENTIAL SOURCE' not in wire
    assert 'USD 650.00' in wire and 'customer-x' in wire
    assert result.tool_calls[0]['result']['receivables'][0]['original_minor']==65000
    assert result.usage['reported_subtotals']=={'prompt_tokens':11} and not result.usage['complete']


def test_request_usage_and_policy_recover_together_then_old_checkpoint_clears_them(own,tmp_path):
    db=tmp_path/'restore.sqlite';save(db,own)
    old=recovery.create_backup(db,'Before any agent history.',ctx=ctx(own))
    args=(db,'a'*32,'Why the gap?','mock',UsageAdapter())
    done=run_request(*args,ctx=ctx(own),lock=web.LOCK,data_mode='minimized')
    b=recovery.create_backup(db,'With measured metadata.',ctx=ctx(own))
    pv=recovery.restore_preview(db,b['id'],ctx=ctx(own));recovery.restore_backup(db,b['id'],pv['revision'],own.business_id,ctx=ctx(own))
    result=run_request(*args,ctx=ctx(own),lock=web.LOCK,data_mode='minimized')
    assert result.replayed and result.result.usage==done.result.usage and result.result.disclosure==done.result.disclosure
    pv=recovery.restore_preview(db,old['id'],ctx=ctx(own));recovery.restore_backup(db,old['id'],pv['revision'],own.business_id,ctx=ctx(own))
    assert agent.list_agent_runs(db)==[]
    import sqlite3
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM agent_run_metadata').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM agent_request_policies').fetchone()[0]==0


def test_http_preview_makes_no_model_call_confirms_and_replays(own,live_server,monkeypatch):
    agent_web.mount(web)
    base,db=live_server;save(db,own);a=UsageAdapter();monkeypatch.setattr(agent_web,'live_adapter',lambda:a)
    fields=dict(csrf=web.CSRF_TOKEN,request_id='a'*32,business_id=own.business_id,question='Why the gap?',provider='live',data_mode='minimized')
    code,body=_post(base,'/agent/preview',fields)
    assert code==200 and not a.messages and agent.list_agent_runs(db)==[]
    token=re.search("name='token' value='([^']+)'",body)[1]
    confirm=dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes')
    code,body=_post(base,'/agent/ask',confirm)
    assert code==200 and 'prompt tokens: 20' in body and len(a.messages)==2
    assert _post(base,'/agent/ask',confirm)[0]==200 and len(a.messages)==2
    assert _get(base,'/agent/requests/'+'a'*32)[0]==200


@pytest.mark.parametrize('reason',['ledger','business_name','config','expired','fields','confirmation'])
def test_http_preview_changes_block_before_any_model_call(own,live_server,monkeypatch,reason):
    agent_web.mount(web)
    base,db=live_server;save(db,own);a=UsageAdapter();monkeypatch.setattr(agent_web,'live_adapter',lambda:a)
    code,body=_post(base,'/agent/preview',dict(csrf=web.CSRF_TOKEN,request_id='a'*32,business_id=own.business_id,question='Why the gap?',provider='live',data_mode='minimized'))
    assert code==200
    token=re.search("name='token' value='([^']+)'",body)[1]
    fields=dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes')
    if reason=='ledger':own.constraints.max_advance_minor=1;save(db,own)
    if reason=='business_name':own.business_name='Changed after review';save(db,own)
    if reason=='config':monkeypatch.setattr(agent_web,'live_adapter',lambda:UsageAdapter())
    if reason=='expired':web._STAGED[token]['created']-=901
    if reason=='fields':fields['question']='Changed'
    if reason=='confirmation':fields.pop('confirmed')
    assert _post(base,'/agent/ask',fields)[0]==400
    assert not a.messages and agent.list_agent_runs(db)==[]


def test_http_mapping_preview_confirmation_replay_and_saved_profile(own,live_server):
    base,db=live_server;save(db,own)
    fields=dict(DEFAULT_MAPPING,csrf=web.CSRF_TOKEN,business_id=own.business_id,account_id='operating',start='2027-01-01',end='2027-01-31',note='Checked synthetic statement.',profile_name='My format')
    before=copy.deepcopy(vars(_load(db)))
    code,body=_mpost(base,'/statement-mapping/preview',fields,{'file':('sample.csv',b'id,date,currency,amount,reference\nr1,2027-01-03,USD,-1.00,fee\n','text/csv')})
    assert code==200 and vars(_load(db))==before
    token=re.search("name='token' value='([^']+)'",body)[1]
    f=dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes')
    assert _post(base,'/statement-mapping/confirm',f)[0]==200
    loaded=_load(db);after=copy.deepcopy(vars(loaded))
    assert _post(base,'/statement-mapping/confirm',f)[0]==200 and vars(_load(db))==after
    profile=next(iter(loaded.statement_profiles))
    assert _get(base,'/statement-mapping?profile='+profile)[0]==200
    assert loaded.actual_cash('USD')==100000 and not loaded.receipts


def test_http_collection_stress_evaluate_save_and_reload(own,live_server):
    base,db=live_server;save(db,own)
    code,body=_post(base,'/scenarios/evaluate',dict(csrf=web.CSRF_TOKEN,label='Zero collection',collections='custom-x,0'))
    assert code==200 and '0% of' in body and not _load(db).scenarios
    fields=dict(csrf=web.CSRF_TOKEN,operation='scenario_save',revision=review_revision(own),label='Zero collection',collections='custom-x,0')
    assert _post(base,'/extensions/preview',fields)[0]==200
    assert _post(base,'/extensions/confirm',dict(fields,confirmed='yes'))[0]==200
    loaded=_load(db);saved=next(iter(loaded.scenarios))
    code,body=_get(base,'/scenarios?saved='+saved)
    assert code==200 and 'custom-x,0' in body and loaded.receivables['custom-x'].remaining_minor==65000
