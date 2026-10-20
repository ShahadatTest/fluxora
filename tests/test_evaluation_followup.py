"""Local evaluation/cleanup regressions. No live model or human participant."""
import copy
import json
import sqlite3
import threading

import pytest

from cashflow_copilot.agent import AgentRunResult,execute_tool,run_agent_question,list_agent_runs
from cashflow_copilot.agent_requests import run_request,request_status
from cashflow_copilot.authz import owner_context_for
from cashflow_copilot.cli import cmd_seed
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.model_adapter import MockProviderAdapter,AdapterError
from cashflow_copilot.recovery import list_backups,checked_backup
from cashflow_copilot.store import SQLiteStore
from tools.live_cases import CASES,assertions,case_ledger,evaluate
from tools.prepare_usability import prepare
from tools.verify_live import main
from tests.test_live_evaluation import Scripted
from tests.test_model_adapter import stub,_StubHandler,_completion,CANARY_KEY


def load(db):
    s=SQLiteStore(db)
    try:return s.load_ledger()
    finally:s.close()


@pytest.mark.parametrize('opening,breach',[(100000,True),(500000,False)])
def test_mock_delay_summary_uses_actual_breach_and_keeps_currencies_separate(opening,breach):
    l=case_ledger('delay');l.accounts['usd'].opening_minor=opening
    before=copy.deepcopy(vars(l))
    r=run_agent_question(l,'What if the client pays seven days later?',MockProviderAdapter(),data_mode='minimized')
    assert 'Conditional calculation' in r.answer and 'EUR separately' in r.answer and 'USD separately' in r.answer
    assert 'first projected breach 2027-01-07' in r.answer if breach else 'USD separately: minimum USD 3,600.00' in r.answer
    assert 'gap itself does not disappear' not in r.answer and 'Recorded facts:' not in r.answer
    assert 'no projected floor breach' in r.answer
    assert {k:v for k,v in vars(l).items() if k!='audit'}=={k:v for k,v in before.items() if k!='audit'}


def test_mock_delay_does_not_invent_usd_when_only_eur_exists():
    l=case_ledger('delay');l.accounts.pop('usd');l.receivables={};l.outflows={}
    r=run_agent_question(l,'What if the client pays seven days later?',MockProviderAdapter(),data_mode='minimized')
    assert 'EUR separately' in r.answer and 'USD' not in r.answer and 'no projected floor breach' in r.answer


@pytest.mark.parametrize('ident,percent',[('partial_collection',50),('no_collection',0)])
@pytest.mark.parametrize('shift',[None,0,1])
def test_collection_reference_accepts_equivalent_zero_shift_but_rejects_real_shift(ident,percent,shift):
    case=next(c for c in CASES if c.id==ident);before=case_ledger(ident);l=copy.deepcopy(before)
    scenario=dict(receivable_id='inv-x',collection_percent=percent)
    if shift is not None:scenario['shift_receivable_days']=shift
    args=dict(scenario=scenario);result=execute_tool(owner_context_for(l.business_id),l,'compute_forecast',args)['result']
    run=AgentRunResult('probe',l.business_id,case.question,'local scripted probe','Not verified prose',
        [dict(name='compute_forecast',ok=True,arguments=args,result=result)],'now','now')
    checks=assertions(case,before,l,run)
    assert all(c['passed'] for c in checks)==(shift in (None,0))


@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_evaluation_records_actual_data_selection_trace_usage_and_pending_manual_review(mode):
    case=next(c for c in CASES if c.id=='breach')
    result=evaluate(Scripted,[case],mode='scripted_test',data_mode=mode)[0]
    assert result['automated_passed'] and result['data_mode']==mode and result['disclosure']['mode']==mode
    assert result['tool_results_for_manual_review'][0]['result']['currencies']['USD']['closing_minor']==70000
    assert 'outbound_tool_reply' in result['tool_results_for_manual_review'][0]
    assert result['manual_review_status']=='pending' and not result['financial_prose_verified']
    assert result['usage']['reported_subtotals']=={} and not result['usage']['complete']


def test_evaluation_invalid_data_selection_never_constructs_provider():
    def factory(case):pytest.fail('Invalid data mode cannot construct a provider')
    with pytest.raises(CashflowError):evaluate(factory,mode='scripted_test',data_mode='invalid')


def test_evaluation_exception_body_does_not_enter_manual_report():
    def factory(case):raise AdapterError(CANARY_KEY)
    result=evaluate(factory,[CASES[0]],mode='scripted_test')[0]
    assert not result['automated_passed'] and CANARY_KEY not in json.dumps(result)
    assert not result['tool_results_for_manual_review'] and result['manual_review_status']=='pending'


def test_cli_local_http_stub_report_has_real_tool_trace_and_never_certifies_prose(stub,tmp_path,monkeypatch):
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY',CANARY_KEY)
    monkeypatch.setenv('CASHFLOW_MODEL_BASE_URL',stub)
    monkeypatch.setenv('CASHFLOW_MODEL_NAME','local-http-stub')
    calls=[dict(id='forecast',type='function',function=dict(name='compute_forecast',arguments='{}'))]
    _StubHandler.responses=[(200,_completion('ping')),(200,_completion(tool_calls=calls)),(200,_completion('UNVERIFIED WRONG FIGURE USD 999999 '+CANARY_KEY))]
    p=tmp_path/'stub-report.json';assert main(['--case','breach','--output',str(p)])==0
    text=p.read_text();report=json.loads(text);row=report['questions'][0]
    assert report['data_mode']=='minimized' and row['automated_passed']
    assert row['tool_results_for_manual_review'][0]['result']['currencies']['USD']['closing_minor']==70000
    assert 'UNVERIFIED WRONG FIGURE' in row['answer_for_manual_review']
    assert report['manual_review_status']=='pending' and row['manual_review_status']=='pending' and not row['financial_prose_verified']
    assert CANARY_KEY not in text and '[redacted]' in text and len(_StubHandler.requests)==3


def history_counts(db):
    c=sqlite3.connect(db)
    try:return {t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['agent_runs','agent_requests','agent_run_metadata','agent_request_policies']}
    finally:c.close()


def completed(db):
    cmd_seed(str(db));l=load(db)
    run_request(db,'a'*32,'Compare the available plans.','mock',MockProviderAdapter(),ctx=owner_context_for(l.business_id),lock=threading.RLock(),data_mode='minimized')
    return l


def test_reset_clears_all_agent_history_atomically_and_backup_retains_previous_snapshot(tmp_path):
    db=tmp_path/'reset.sqlite';l=completed(db);assert all(v==1 for v in history_counts(db).values())
    cmd_seed(str(db))
    assert all(v==0 for v in history_counts(db).values()) and not list_agent_runs(db)
    backups=list_backups(db,ctx=owner_context_for(l.business_id));assert len(backups)==1
    path,_,_=checked_backup(db,backups[0]['id'])
    assert all(v==1 for v in history_counts(path).values())


def test_reset_history_deletion_failure_rolls_back_ledger_and_every_history_table(tmp_path):
    db=tmp_path/'rollback.sqlite';completed(db)
    s=SQLiteStore(db);l=s.load_ledger();l.business_name='Retain this pre-reset name';s.save_ledger(l)
    s.conn.execute("CREATE TRIGGER stop_reset BEFORE DELETE ON agent_requests BEGIN SELECT RAISE(ABORT,'injected local reset failure'); END")
    s.conn.commit();s.close();before=copy.deepcopy(vars(load(db)));counts=history_counts(db)
    with pytest.raises(sqlite3.IntegrityError):cmd_seed(str(db))
    assert vars(load(db))==before and history_counts(db)==counts


def test_new_session_preparation_is_synthetic_and_never_overwrites_participant_data(tmp_path):
    folder=tmp_path/'fresh-session';r=prepare(folder)
    assert r['participant_count']==0 and r['human_status']=='pending' and r['observations']==[]
    treasury=load(folder/'treasury.sqlite')
    assert treasury.actual_cash('USD')==100000 and len(treasury.statement_profiles)==1
    assert len(treasury.accounts)==3 and len(treasury.forecast_routes)==4 and len(treasury.scenarios)==1
    assert load(folder/'baseline.sqlite').applied_event_ids=={'evt-delay-001'}
    (folder/'OBSERVATIONS.md').write_text('Do not overwrite a participant observation.')
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(ValueError):prepare(folder)
    assert {p.name:p.read_bytes() for p in folder.iterdir()}==before
