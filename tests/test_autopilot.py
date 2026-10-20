"""Real SQLite jobs, deterministic tools and local/stub Telegram. No live calls."""
import copy,json,sqlite3,threading,time
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from cashflow_copilot import autopilot,local_app,web,agent_workflows
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.store import SQLiteStore
from tests.test_web import _load


@pytest.fixture
def ap(tmp_path):
    app=local_app.create_server(tmp_path/'state.sqlite',0);now=[time.time()]
    engine=app.autopilot;engine.clock=lambda:now[0];db=app.workspace_database
    ledger=_load(db);ctx=RequestContext('test-owner','owner',ledger.business_id)
    ledger.apply_event('evt-delay-001',ctx=ctx)
    store=SQLiteStore(db);store.save_ledger(ledger);store.close()
    policy=autopilot.defaults(ledger);policy.update(enabled=True,strategy='auto-combined',caps={c:10**9 for c in policy['caps']})
    yield app,engine,db,ctx,policy,now
    app.server_close()


def configure(ap,**changes):
    app,engine,db,ctx,policy,now=ap;policy=copy.deepcopy(policy);policy.update(changes)
    ledger=_load(db);row=engine.policy(db,ledger,ctx=ctx)
    engine.save(db,ledger,policy,ctx=ctx,expected=row['revision']);return engine.identity(db)


def jobs(ap):
    _,engine,db,ctx,_,_=ap
    return engine.history(db,_load(db),ctx=ctx)


def test_enabled_policy_automatically_analyzes_and_saves_only_draft_pending_work(ap):
    app,engine,db,ctx,policy,now=ap;before=_load(db);key=configure(ap)
    engine.tick(key);done=jobs(ap)[0]
    assert done['status']=='completed',done
    result=json.loads(done['result']);after=_load(db)
    assert result['provider']=='local deterministic Autopilot; no model call'
    assert set(result['currencies'])=={'USD','EUR'} and result['workflow_ids']
    assert after.as_of==before.as_of and after.actual_cash('USD')==before.actual_cash('USD') and not after.receipts
    assert all(a.state=='draft' and a.approved_version is None and a.counterparty_response is None for a in after.actions.values())
    assert len(after.actions)==2
    assert all(j['status']=='pending_approval' and not j['approval'] and not j['execution'] for j in after.agent_workflows.values())
    assert not after.forecast_snapshots
    store=SQLiteStore(db)
    try:assert store.conn.execute('SELECT count(*) FROM autopilot_runs').fetchone()[0]==1
    finally:store.close()


def test_own_outputs_do_not_loop_and_periodic_digest_never_prepares_more_work(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);engine.tick(key)
    before=_load(db);now[0]+=301;engine.tick(key)
    assert len(jobs(ap))==1
    now[0]+=86401;engine.tick(key)
    assert jobs(ap)[0]['kind']=='digest' and len(jobs(ap))==2
    after=_load(db);assert after.actions==before.actions and after.agent_workflows==before.agent_workflows and after.as_of==before.as_of


def test_restart_replays_committed_result_without_recreating_drafts(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);engine.tick(key)
    ident=jobs(ap)[0]['id'];before=_load(db)
    with engine.db() as conn:conn.execute("UPDATE jobs SET status='running',lease_until=0,result='{}' WHERE id=?",(ident,))
    restarted=autopilot.Service(app,clock=lambda:now[0]);restarted.execute(ident)
    assert jobs(ap)[0]['status']=='completed'
    after=_load(db);assert vars(after)==vars(before)
    with restarted.db() as conn:assert conn.execute('SELECT last_basis FROM policies').fetchone()[0]==autopilot.basis(after)
    restarted.close()


def test_financial_values_returning_to_previous_snapshot_are_analyzed_again(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);engine.tick(key)
    before=_load(db);original=before.accounts['usd-main'].opening_minor
    for value in [original+1000,original]:
        l=_load(db);l.accounts['usd-main'].opening_minor=value;s=SQLiteStore(db);s.save_ledger(l);s.close()
        now[0]+=301;engine.tick(key)
    assert len(jobs(ap))==3 and all(j['status']=='completed' for j in jobs(ap))
    assert len(_load(db).actions)==len(before.actions)


def test_crash_window_after_effect_commit_recovers_without_reexecuting_analysis(ap,monkeypatch):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);original=engine.db;fail=[True]
    class Connection:
        def __init__(self,value):self.value=value
        def execute(self,sql,args=()):
            if "UPDATE jobs SET status='completed'" in sql and fail[0]:
                fail[0]=False;raise sqlite3.OperationalError('simulated control-index failure')
            return self.value.execute(sql,args)
        def __getattr__(self,key):return getattr(self.value,key)
    @contextmanager
    def broken():
        with original() as conn:yield Connection(conn)
    monkeypatch.setattr(engine,'db',broken);engine.tick(key)
    assert jobs(ap)[0]['status']=='running' and len(_load(db).actions)==2
    before=copy.deepcopy(vars(_load(db)));ident=jobs(ap)[0]['id']
    monkeypatch.setattr(engine,'db',original)
    engine.analyzer=lambda *a:pytest.fail('Journal recovery must not rerun analysis')
    engine.execute(ident)
    assert jobs(ap)[0]['status']=='completed' and vars(_load(db))==before


def test_expired_worker_claim_cannot_commit_after_another_worker_recovers_it(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);ident=engine.enqueue(key)
    original=engine.analyzer;entered=threading.Event();release=threading.Event()
    def waiting(*args):
        result=original(*args);entered.set();assert release.wait(5);return result
    engine.analyzer=waiting
    worker=threading.Thread(target=engine.execute,args=(ident,));worker.start()
    assert entered.wait(5)
    replacement=autopilot.Service(app,clock=lambda:now[0])
    replacement.execute(ident);assert jobs(ap)[0]['status']=='running' and not _load(db).actions
    now[0]+=31;replacement.execute(ident);assert jobs(ap)[0]['status']=='completed'
    committed=copy.deepcopy(vars(_load(db)));release.set();worker.join(5)
    assert not worker.is_alive() and vars(_load(db))==committed and len(_load(db).actions)==2
    replacement.close()


def test_rejected_or_approved_snapshot_is_a_followup_not_a_new_preparation(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);engine.tick(key)
    l=_load(db);job=next(iter(l.agent_workflows.values()))
    agent_workflows.decide(l,job['id'],job['review_token'],'reject',ctx=ctx)
    store=SQLiteStore(db);store.save_ledger(l);store.close()
    now[0]+=301;engine.tick(key)
    assert jobs(ap)[0]['kind']=='follow_up' and len(_load(db).agent_workflows)==1


def test_snapshot_approval_audit_metadata_never_triggers_another_snapshot_after_restart(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);engine.tick(key)
    ledger=_load(db);before=autopilot.basis(ledger);job=next(iter(ledger.agent_workflows.values()))
    agent_workflows.decide(ledger,job['id'],job['review_token'],'approve',ctx=ctx)
    assert ledger.owner_operations and autopilot.basis(ledger)==before
    store=SQLiteStore(db);store.save_ledger(ledger);store.close()
    restarted=autopilot.Service(app,clock=lambda:now[0]);now[0]+=301;restarted.tick(key)
    assert jobs(ap)[0]['kind']=='follow_up' and len(_load(db).agent_workflows)==1 and len(_load(db).forecast_snapshots)==1
    now[0]+=301;restarted.tick(key);assert len(jobs(ap))==2
    restarted.close()


def test_zero_currency_limit_keeps_full_plan_out_and_reports_reason(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap,caps={c:0 for c in policy['caps']})
    engine.tick(key);result=json.loads(jobs(ap)[0]['result'])
    assert jobs(ap)[0]['status']=='completed' and not _load(db).actions
    assert any('limit' in n or 'sufficient' in n or 'Missing' in n for n in result['notes']) or result['no_feasible_plan']


def test_explicit_strategy_or_limit_change_rechecks_same_facts_without_duplicate_work(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap,strategy='off',snapshot=False);engine.tick(key)
    assert not _load(db).actions
    configure(ap,strategy='auto-combined',snapshot=False);engine.tick(key)
    assert len(_load(db).actions)==2 and jobs(ap)[0]['kind']=='financial_change'
    now[0]+=301;configure(ap,strategy='auto-combined',snapshot=False);engine.tick(key)
    assert len(_load(db).actions)==2 and len(jobs(ap))==2


def test_no_feasible_plan_is_honest_and_missing_due_dates_are_not_invented(ap):
    app,engine,db,ctx,policy,now=ap;l=_load(db)
    for a in l.accounts.values():a.opening_minor=-10**9
    for r in l.receivables.values():r.due_date=None
    store=SQLiteStore(db);store.save_ledger(l);store.close()
    key=configure(ap);engine.tick(key);result=json.loads(jobs(ap)[0]['result'])
    assert result['no_feasible_plan'] and not result['overdue'] and not _load(db).actions


def test_policy_pause_during_analysis_discards_all_candidate_work(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);original=engine.analyzer
    def pause(*args):
        result=original(*args);ledger=_load(db);row=engine.policy(db,ledger,ctx=ctx)
        p=json.loads(row['policy']);p['enabled']=False;engine.save(db,ledger,p,ctx=ctx,expected=row['revision'])
        return result
    engine.analyzer=pause;engine.tick(key)
    assert jobs(ap)[0]['status']=='cancelled' and not _load(db).actions and not _load(db).agent_workflows


def test_late_candidate_is_rolled_back_before_any_draft_or_journal_commit(ap,monkeypatch):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);expired=[False];real=time.monotonic;original=engine.analyzer
    monkeypatch.setattr(autopilot,'time',SimpleNamespace(monotonic=lambda:real()+(11 if expired[0] else 0)))
    def late(*args):
        result=original(*args);expired[0]=True;return result
    engine.analyzer=late;engine.tick(key)
    assert jobs(ap)[0]['status']=='timeout' and not _load(db).actions and not _load(db).agent_workflows
    store=SQLiteStore(db)
    try:assert store.conn.execute('SELECT count(*) FROM autopilot_runs').fetchone()[0]==0
    finally:store.close()


def test_ledger_change_and_aba_reset_during_analysis_discard_candidate(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap);original=engine.analyzer
    def changed(*args):
        result=original(*args);store=SQLiteStore(db)
        try:store.save_ledger(store.load_ledger()) # same values, different state generation
        finally:store.close()
        return result
    engine.analyzer=changed;engine.tick(key)
    assert jobs(ap)[0]['status']=='stale' and not _load(db).actions and not _load(db).agent_workflows


def test_analyzer_cannot_approve_or_change_cash_and_raw_exceptions_are_private(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap)
    def hostile(ledger,*args):
        ledger.accounts['usd-main'].opening_minor+=1
        return dict(message='dummy-provider-key-DO-NOT-DISPLAY')
    engine.analyzer=hostile;before=_load(db);engine.tick(key)
    assert jobs(ap)[0]['status']=='blocked' and vars(_load(db))==vars(before)
    assert 'dummy-provider-key' not in json.dumps(jobs(ap))


@pytest.mark.parametrize('role',['agent','viewer'])
def test_only_owner_can_authorize_persistent_policy(ap,role):
    app,engine,db,ctx,policy,now=ap
    with pytest.raises(CashflowError):engine.save(db,_load(db),policy,ctx=RequestContext('test',role,ctx.business_id),expected='')
    assert not jobs(ap)


def test_business_scope_unknown_fields_and_stale_configuration_are_rejected(ap):
    app,engine,db,ctx,policy,now=ap
    with pytest.raises(CashflowError):engine.save(db,_load(db),policy,ctx=RequestContext('test','owner','foreign'),expected='')
    with pytest.raises(CashflowError):configure(ap,approve=True)
    with pytest.raises(CashflowError):configure(ap,caps={'USD':1.2,'EUR':1})
    configure(ap)
    with pytest.raises(CashflowError):engine.save(db,_load(db),policy,ctx=ctx,expected='')
    with pytest.raises(CashflowError):configure(ap,notify=True)


def test_disabled_policies_do_not_starve_active_workspaces_and_jobs_are_bounded(ap):
    app,engine,db,ctx,policy,now=ap;key=configure(ap)
    with engine.db() as conn:
        for i in range(5):conn.execute('INSERT INTO policies VALUES (?,?,?,?,?,?,?,?)',('disabled'+str(i),'other','r',json.dumps(dict(policy,enabled=False)),0,'','',0))
    engine.tick();assert jobs(ap)[0]['status']=='completed'
    now[0]+=301
    with engine.db() as conn:
        for i in range(autopilot.MAX_JOBS_DAY):conn.execute('INSERT INTO job_attempts VALUES (?,?,?)',('cap'+str(i),key,now[0]))
    l=_load(db);l.accounts['usd-main'].opening_minor+=1;s=SQLiteStore(db);s.save_ledger(l);s.close()
    assert engine.enqueue(key) is None
    # Restart and clearing displayed history cannot reset the rolling allowance.
    with engine.db() as conn:conn.execute('DELETE FROM jobs')
    restarted=autopilot.Service(app,clock=lambda:now[0]);assert restarted.enqueue(key,force=True) is None
    now[0]+=86401;assert restarted.enqueue(key) is not None
    restarted.close()


def test_real_background_thread_runs_saved_policy_and_stops_without_network(ap,monkeypatch):
    app,engine,db,ctx,policy,now=ap;key=configure(ap)
    from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
    monkeypatch.setattr(OpenAICompatibleAdapter,'chat',lambda *a,**k:pytest.fail('Autopilot must not call a model'))
    engine.start()
    end=time.monotonic()+5
    while time.monotonic()<end and not any(j['status']=='completed' for j in jobs(ap)):time.sleep(.02)
    engine.close();assert not engine.thread.is_alive() and jobs(ap)[0]['status']=='completed'
