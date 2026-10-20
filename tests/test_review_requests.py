"""Atomic request effects, crash/replay, read responsiveness and daily guide."""
import copy,json,re,sqlite3,subprocess,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
from datetime import date,timedelta
from pathlib import Path
import pytest

from cashflow_copilot import agent,agent_requests,agent_web,recovery,web
from cashflow_copilot.agent_requests import run_request,request_status
from cashflow_copilot.attention import attention_items
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import CashflowError,AuthorizationError
from cashflow_copilot.model_adapter import AdapterResult,AdapterStatus,ToolCall
from cashflow_copilot.obligations import check_statement_balance
from cashflow_copilot.operations import create_recurring,confirm_recurring
from cashflow_copilot.owner_workflows import review_revision
from cashflow_copilot.review import daily_review
from cashflow_copilot.store import SQLiteStore
from tests.test_adaptive_priorities import own
from tests.test_obligations import bdata,bounds,cdata
from tests.test_owner_workflows import ctx,save
from tests.test_web import live_server,_get,_post,_load

class DraftAdapter:
    provider_label='mock request-regression adapter; not a live model'
    def __init__(self):self.calls=0
    def status(self):return AdapterStatus('mock',self.provider_label,None,'')
    def chat(self,messages,tools,*,deadline=None):
        self.calls+=1
        if self.calls==1:return AdapterResult(tool_calls=[ToolCall('draft','draft_action',dict(action_id='candidate-one',kind='request_early_payment',receivable_id='custom-y',amount_minor=5000,date='2027-01-03'))])
        return AdapterResult(text='Draft prepared for owner review. No approval or payment.')

class GateAdapter(DraftAdapter):
    def __init__(self):super().__init__();self.enter=threading.Event();self.release=threading.Event()
    def chat(self,*args,**kwargs):
        if not self.calls:self.enter.set();assert self.release.wait(5)
        return super().chat(*args,**kwargs)

def execute(db,l,adapter=None,ident='a'*32,question='Prepare a source-backed draft.',lock=None,caller=None,budget=60):
    return run_request(db,ident,question,'mock',adapter or DraftAdapter(),ctx=caller or ctx(l),lock=lock or web.LOCK,time_budget_s=budget)

def test_success_replay_and_changed_payload_are_durable_and_atomic(own,tmp_path):
    db=tmp_path/'request.sqlite';save(db,own);a=DraftAdapter();result=execute(db,own,a)
    loaded=_load(db);assert result.status=='completed' and len(loaded.actions)==1 and loaded.actions['candidate-one'].state=='draft'
    assert loaded.actual_cash('USD')==100000 and loaded.consent_map=={} and loaded.runtime_evidence_ids==set()
    assert len(agent.list_agent_runs(db))==1
    replay=execute(db,own,a);assert replay.replayed and replay.result.run_id==result.result.run_id and a.calls==2
    assert request_status(db,'a'*32,ctx=ctx(own),lock=web.LOCK).status=='completed'
    with pytest.raises(CashflowError):execute(db,own,a,question='Different question')
    assert len(_load(db).actions)==1 and len(agent.list_agent_runs(db))==1


@pytest.mark.parametrize('caller',['agent','viewer','business'])
def test_request_mutation_authorization_precedes_completed_replay(own,tmp_path,caller):
    db=tmp_path/'auth.sqlite';save(db,own);execute(db,own)
    bad=ctx(own,business='other') if caller=='business' else ctx(own,caller)
    with pytest.raises(AuthorizationError):execute(db,own,caller=bad)


@pytest.mark.parametrize('reason',['mutation','same_graph_save','reset_business'])
def test_concurrent_change_discards_snapshot_without_overwriting_owner_state(own,tmp_path,reason):
    db=tmp_path/'stale.sqlite';save(db,own);a=GateAdapter();lock=threading.RLock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(execute,db,own,a,lock=lock)
        try:
            assert a.enter.wait(2)
            with lock:
                current=_load(db)
                if reason=='mutation':current.constraints.max_advance_minor=1234
                if reason=='reset_business':current.business_id='replacement'
                save(db,current)
        finally:a.release.set()
        if reason=='reset_business':
            with pytest.raises(AuthorizationError):pending.result()
        else:
            assert pending.result().status=='stale'
            assert agent.list_agent_runs(db)[0]['tool_calls']==[]
    current=_load(db);assert not current.actions
    if reason=='mutation':assert current.constraints.max_advance_minor==1234


def test_running_duplicate_returns_pending_without_another_provider_call(own,tmp_path):
    db=tmp_path/'pending.sqlite';save(db,own);a=GateAdapter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(execute,db,own,a)
        try:
            assert a.enter.wait(2)
            result=execute(db,own,a)
            assert result.status=='running' and result.replayed and a.calls==0
            assert request_status(db,'a'*32,ctx=ctx(own),lock=web.LOCK).status=='running'
        finally:a.release.set()
        assert pending.result().status=='completed'
    assert len(_load(db).actions)==1 and len(agent.list_agent_runs(db))==1


@pytest.mark.parametrize('failure',['late_final','provider_exception','forbidden_tool','empty_final','boundary_mutation'])
def test_failed_or_late_run_never_saves_partial_drafts_or_raw_errors(own,tmp_path,monkeypatch,failure):
    db=tmp_path/'failure.sqlite';save(db,own)
    class Fault(DraftAdapter):
        def chat(self,messages,tools,*,deadline=None):
            if self.calls==1:
                self.calls+=1
                if failure=='late_final':time.sleep(.03);return AdapterResult(text='LATE SHOULD NOT APPEAR')
                if failure=='provider_exception':raise RuntimeError('DUMMY-SECRET-DO-NOT-PERSIST')
                if failure=='forbidden_tool':return AdapterResult(tool_calls=[ToolCall('evil','approve_payment',{})])
                if failure=='empty_final':return AdapterResult(text='')
            return super().chat(messages,tools,deadline=deadline)
    if failure=='boundary_mutation':
        original=agent.run_agent_question
        def changed(ledger,*args,**kw):
            result=original(ledger,*args,**kw);ledger.accounts['operating'].opening_minor+=100;return result
        monkeypatch.setattr(agent,'run_agent_question',changed)
    a=Fault();result=execute(db,own,a,budget=.01 if failure=='late_final' else 60)
    assert result.status=='failed' and not _load(db).actions and _load(db).actual_cash('USD')==100000
    assert agent.list_agent_runs(db)[0]['tool_calls']==[]
    blob=db.read_bytes();assert b'DUMMY-SECRET' not in blob and b'LATE SHOULD NOT APPEAR' not in blob
    calls=a.calls;assert execute(db,own,a).replayed and a.calls==calls


def test_sql_failure_rolls_back_history_and_draft_in_one_transaction(own,tmp_path):
    db=tmp_path/'sql.sqlite';save(db,own);s=SQLiteStore(db)
    s.conn.executescript(agent.AGENT_RUNS_SCHEMA+"CREATE TRIGGER reject_history BEFORE INSERT ON agent_runs BEGIN SELECT RAISE(ABORT,'synthetic history fault'); END;")
    s.close()
    with pytest.raises(sqlite3.IntegrityError):execute(db,own)
    assert _load(db).actions=={} and agent.list_agent_runs(db)==[]
    assert request_status(db,'a'*32,ctx=ctx(own),lock=web.LOCK).status=='interrupted'
    s=SQLiteStore(db);s.conn.execute('DROP TRIGGER reject_history');s.conn.commit();s.close()
    assert execute(db,own,ident='b'*32).status=='completed' and len(_load(db).actions)==1


def test_deadline_expiry_waiting_for_commit_lock_discards_completed_candidate(own,tmp_path):
    db=tmp_path/'deadline.sqlite';save(db,own);lock=threading.RLock();a=GateAdapter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(execute,db,own,a,lock=lock,budget=.1)
        assert a.enter.wait(2)
        with lock:
            a.release.set();time.sleep(.15)
        result=pending.result()
    assert result.status=='failed' and 'deadline' in result.result.answer and not _load(db).actions


CRASH_CODE=r'''
import os,sys,threading
from cashflow_copilot import agent_requests
from cashflow_copilot.model_adapter import AdapterResult,ToolCall
from cashflow_copilot.store import SQLiteStore
from cashflow_copilot.authz import owner_context_for
class Adapter:
 provider_label='mock crash test; no live model'
 def chat(self,messages,tools,*,deadline=None):
  if any(m['role']=='tool' for m in messages):return AdapterResult(text='Draft for review.')
  return AdapterResult(tool_calls=[ToolCall('one','draft_action',dict(action_id='candidate-one',kind='request_early_payment',receivable_id='custom-y',amount_minor=5000,date='2027-01-03'))])
if sys.argv[2]=='before_commit':
 def die(conn,result):os._exit(71)
 agent_requests._write_run=die
else:
 class Connection:
  def __init__(self,c):self.c=c
  def __getattr__(self,k):return getattr(self.c,k)
  def commit(self):
   row=self.c.execute("SELECT status FROM agent_requests WHERE request_id=?",('a'*32,)).fetchone()
   completed=bool(row and row[0]=='completed');self.c.commit()
   if completed:os._exit(72)
 class Store(SQLiteStore):
  def __init__(self,path):super().__init__(path);self.conn=Connection(self.conn)
 agent_requests.SQLiteStore=Store
s=SQLiteStore(sys.argv[1]);l=s.load_ledger();s.close()
agent_requests.run_request(sys.argv[1],'a'*32,'Prepare a source-backed draft.','mock',Adapter(),ctx=owner_context_for(l.business_id),lock=threading.RLock())
'''


@pytest.mark.parametrize('phase,code',[('before_commit',71),('after_commit',72)])
def test_real_process_crash_and_restart_never_duplicates_or_splits_effects(own,tmp_path,phase,code):
    db=tmp_path/'crash.sqlite';save(db,own)
    result=subprocess.run([sys.executable,'-c',CRASH_CODE,str(db),phase],cwd=Path(__file__).resolve().parent.parent,capture_output=True,text=True,timeout=15)
    assert result.returncode==code,result.stderr
    loaded=_load(db);runs=agent.list_agent_runs(db);a=DraftAdapter()
    outcome=execute(db,own,a)
    assert a.calls==0 and outcome.replayed
    if phase=='before_commit':assert not loaded.actions and not runs and outcome.status=='interrupted'
    else:assert len(loaded.actions)==1 and len(runs)==1 and outcome.status=='completed'


def test_native_backup_restore_retains_completed_request_identity(own,tmp_path):
    db=tmp_path/'restore.sqlite';save(db,own);done=execute(db,own)
    backup=recovery.create_backup(db,'Atomic request checkpoint',ctx=ctx(own));preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own))
    recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    a=DraftAdapter();replay=execute(db,own,a)
    assert replay.replayed and replay.result.run_id==done.result.run_id and a.calls==0 and len(_load(db).actions)==1


def test_statement_mismatch_reaches_queue_and_daily_review_without_cash_change(own):
    bid=bdata(own);before=own.actual_cash('USD');bounds(own,bid,closing=110001)
    snapshot=copy.deepcopy(vars(own));items=attention_items(own,ctx=ctx(own))
    row=next(i for i in items if i['code']=='statement_boundary')
    assert 'check-one' in row['href'] and row['urgency']=='high' and row['evidence_ids']==['customer-x']
    r=daily_review(own,ctx=ctx(own));stage=next(s for s in r['stages'] if s['id']=='matching')
    assert stage['count'] and row in stage['items'] and own.actual_cash('USD')==before and vars(own)==snapshot


def test_only_latest_statement_boundary_terms_alert_and_missing_terms_remain_visible(own):
    bid=bdata(own);items=attention_items(own,ctx=ctx(own));assert any(i['code']=='statement_boundary_missing' for i in items)
    bounds(own,bid,closing=110001);bounds(own,bid,'fixed-check')
    items=attention_items(own,ctx=ctx(own));rows=[i for i in items if i['code']=='statement_boundary']
    assert len(rows)==1 and rows[0]['target']=='fixed-check' and rows[0]['urgency']=='review'


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_credit_review_links_offer_non_cash_carry_workflow(own,kind):
    cdata(own,kind);items=attention_items(own,ctx=ctx(own))
    target='custom-x' if kind=='invoice' else 'materials'
    assert any(i['code']=='confirm_input' and i['target']==target and i['href']=='/credits' for i in items)


def test_unmaterialized_recurring_work_is_visible_without_creating_bills(own):
    create_recurring(own,'rent','Synthetic rent','USD',1000,own.as_of,None,'monthly',False,False,'Landlord','ops-source','Source checked.',review_revision(own),ctx=ctx(own))
    snapshot=copy.deepcopy(vars(own));items=attention_items(own,ctx=ctx(own))
    assert any(i['code']=='recurring_pending' for i in items) and vars(own)==snapshot
    confirm_recurring(own,'rent',own.as_of,False,review_revision(own),ctx=ctx(own))
    assert not any(i['code']=='recurring_pending' for i in attention_items(own,ctx=ctx(own)))


def test_http_slow_provider_does_not_block_review_and_duplicate_post_stays_pending(own,live_server,monkeypatch):
    base,db=live_server;save(db,own);a=GateAdapter()
    agent_web.mount(web)
    monkeypatch.setattr(agent_web,'live_adapter',lambda:a)
    fields=dict(csrf=web.CSRF_TOKEN,request_id='a'*32,business_id=own.business_id,question='Prepare a source-backed draft.',provider='live')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(_post,base,'/agent/ask',fields)
        try:
            assert a.enter.wait(2)
            start=time.monotonic();assert _get(base,'/review')[0]==200;assert time.monotonic()-start<1
            assert _post(base,'/agent/ask',fields)[0]==202
        finally:a.release.set()
        assert pending.result()[0]==200
    assert len(_load(db).actions)==1


@pytest.mark.parametrize('bad',['missing_id','invalid_id','business','extra','empty','large'])
def test_agent_form_rejects_invalid_request_without_history(live_server,bad):
    agent_web.mount(web)
    base,db=live_server;fields=dict(csrf=web.CSRF_TOKEN,request_id='a'*32,business_id='demo-agency',question='Compare plans.',provider='mock')
    if bad=='missing_id':del fields['request_id']
    if bad=='invalid_id':fields['request_id']='../../other'
    if bad=='business':fields['business_id']='other'
    if bad=='extra':fields['approve']='yes'
    if bad=='empty':fields['question']=''
    if bad=='large':fields['question']='q'*2001
    assert _post(base,'/agent/ask',fields)[0]==400 and agent.list_agent_runs(db)==[]


def test_daily_guide_and_task_session_are_read_only_and_accessible(own,live_server):
    base,db=live_server;save(db,own);before=copy.deepcopy(vars(_load(db)))
    for path in ['/review','/usability']:
        code,html=_get(base,path);assert code==200 and 'Skip to main content' in html and '<main id="main"' in html
        assert html.count('<h1>')==1 and '<html lang="en">' in html
    assert vars(_load(db))==before
