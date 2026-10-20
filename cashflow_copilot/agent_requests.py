"""Synchronous snapshot runs with durable request identity and atomic completion.

No background worker. Provider I/O runs outside the ledger lock. Only complete,
unchanged snapshots may commit new DRAFTs together with their run history.
"""
import copy
from dataclasses import dataclass
import json,re,threading,time
from pathlib import Path
from uuid import uuid4

from . import agent
from .authz import require_access,OP_READ
from .errors import CashflowError,IdempotencyConflict
from .store import SQLiteStore

REQUEST_SCHEMA="""
CREATE TABLE IF NOT EXISTS agent_requests (
 business_id TEXT NOT NULL, request_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
 question TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,
 instance_id TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
 PRIMARY KEY (business_id,request_id));
CREATE TABLE IF NOT EXISTS agent_request_policies (
 business_id TEXT NOT NULL, request_id TEXT NOT NULL, data_mode TEXT NOT NULL,
 PRIMARY KEY (business_id,request_id));
CREATE TABLE IF NOT EXISTS guided_request_tasks (
 business_id TEXT NOT NULL, request_id TEXT NOT NULL, plan_json TEXT NOT NULL,
 PRIMARY KEY (business_id,request_id));
"""
INSTANCE_ID=uuid4().hex
_ACTIVE=set()
_ACTIVE_LOCK=threading.RLock()
MAX_ACTIVE_RUNS=2


@dataclass
class RequestOutcome:
    request_id:str
    status:str
    result:object=None
    replayed:bool=False
    note:str=''


def _validate(request_id,question=None,provider=None):
    if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):
        raise CashflowError('Reload Ask Agent to obtain a valid request identity.')
    if question is not None and (not isinstance(question,str) or not question.strip() or len(question)>2000):
        raise CashflowError('Ask a question containing 1–2000 characters.')
    if provider is not None and provider not in {'mock','live','guided'}:raise CashflowError('Choose a supported provider or reviewed local task.')


def _row(store,business,ident):
    store.conn.executescript(REQUEST_SCHEMA+agent.AGENT_RUNS_SCHEMA)
    result=store.conn.execute('SELECT request_id,run_id,question,provider,status,instance_id,created_at FROM agent_requests WHERE business_id=? AND request_id=?',(business,ident)).fetchone()
    if not result:return None
    row=dict(zip(('request_id','run_id','question','provider','status','instance_id','created_at'),result))
    policy=store.conn.execute('SELECT data_mode FROM agent_request_policies WHERE business_id=? AND request_id=?',(business,ident)).fetchone()
    row['data_mode']=policy[0] if policy else 'full_metadata'
    return row


def _write_run(conn,result):
    conn.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?)',(result.run_id,result.business_id,result.question,result.provider_label,
        json.dumps(result.tool_calls),result.answer[:agent.RESPONSE_CAP],result.created_at,result.finished_at))
    agent.write_run_metadata(conn,vars(result))


def _existing(store,row,business,active_key):
    status=row['status']
    with _ACTIVE_LOCK:alive=active_key in _ACTIVE
    if status=='running' and (row['instance_id']!=INSTANCE_ID or not alive):
        store.conn.execute("UPDATE agent_requests SET status='interrupted',finished_at=? WHERE business_id=? AND request_id=?",
            (agent._utcnow_iso(),business,row['request_id']));store.conn.commit();status='interrupted'
    if status=='running':return RequestOutcome(row['request_id'],'running',replayed=True,
        note='This request is still running. Other pages remain available. Refresh this result; do not resubmit a new question just to retry.')
    r=store.conn.execute('SELECT run_id,business_id,question,provider_label,answer_summary,tool_calls_json,created_at,finished_at FROM agent_runs WHERE run_id=? AND business_id=?',(row['run_id'],business)).fetchone()
    result=agent.AgentRunResult(r[0],r[1],r[2],r[3],r[4],json.loads(r[5]),r[6],r[7],outcome=status) if r else None
    if result:result.disclosure,result.usage=agent.read_run_metadata(store.conn,result.run_id,business)
    return RequestOutcome(row['request_id'],status,result,replayed=True,note=(
        'Saved historical result. It is not recomputed against later ledger changes.' if status=='completed' else
        'This request ended without saving candidate drafts. It will not run again under the old identity. Review the current ledger and use a fresh question if needed.'))


def request_status(db,request_id,*,ctx,lock):
    _validate(request_id);active_key=(str(Path(db).resolve()),request_id)
    with lock:
        s=SQLiteStore(db)
        try:
            ledger=s.load_ledger();require_access(ctx,ledger,OP_READ)
            row=_row(s,ledger.business_id,request_id)
            if row is None:raise CashflowError('No agent request exists for this active business.')
            return _existing(s,row,ledger.business_id,active_key)
        finally:s.close()


def has_guided_request(db,request_id,question,*,ctx,lock):
    """Recognize a saved submission before another translation, including restart.

    A changed focus never re-executes an old identity; its original result is
    historical. A different question/provider must use a new identity.
    """
    _validate(request_id,question)
    with lock:
        store=SQLiteStore(db)
        try:
            ledger=store.load_ledger();require_access(ctx,ledger,OP_READ)
            row=_row(store,ledger.business_id,request_id)
            if row is None:return False
            if row['question']!=question or row['provider']!='guided' or row['data_mode']!='minimized':
                raise IdempotencyConflict('This request identity belongs to another submission. Reload the task form.')
            return True
        finally:store.close()


def _draft_only(before,after):
    if {k:v for k,v in vars(before).items() if k not in {'actions','audit','agent_workflows'}}!={k:v for k,v in vars(after).items() if k not in {'actions','audit','agent_workflows'}}:
        return False
    from .agent_workflows import valid_pending
    if any(after.agent_workflows.get(k)!=v for k,v in before.agent_workflows.items()):return False
    if any(not valid_pending(v,before.business_id) for k,v in after.agent_workflows.items() if k not in before.agent_workflows):return False
    if any(ident not in after.actions or after.actions[ident]!=original for ident,original in before.actions.items()):return False
    return all(a.state=='draft' and a.approved_version is None and a.counterparty_response is None
        and a.business_id==before.business_id for ident,a in after.actions.items() if ident not in before.actions)


def run_request(db,request_id,question,provider,adapter,*,ctx,lock,time_budget_s=agent.TIME_BUDGET_S,data_mode='full_metadata',expected_revision=None):
    from .agent_disclosure import validate_mode,disclosure_revision
    validate_mode(data_mode)
    _validate(request_id,question,provider);active_key=(str(Path(db).resolve()),request_id)
    plan_json=None
    if provider=='guided':
        from .agent_tasks import TaskExecutionAdapter
        if not isinstance(adapter,TaskExecutionAdapter):raise CashflowError('Guided execution requires a reviewed local task.')
        plan_json=json.dumps(adapter.plan,sort_keys=True,separators=(',',':'))
        if adapter.skill is not None:plan_json=json.dumps(dict(plan=adapter.plan,skill=adapter.skill),sort_keys=True,separators=(',',':'))
    with lock:
        s=SQLiteStore(db)
        try:
            before=s.load_ledger();require_access(ctx,before)
            row=_row(s,before.business_id,request_id)
            if row:
                if row['question']!=question or row['provider']!=provider or row['data_mode']!=data_mode:raise IdempotencyConflict('Request identity already belongs to a different question/provider/data selection. Reload the form for a fresh question.')
                if plan_json is not None:
                    saved=s.conn.execute('SELECT plan_json FROM guided_request_tasks WHERE business_id=? AND request_id=?',(before.business_id,request_id)).fetchone()
                    if not saved or saved[0]!=plan_json:raise IdempotencyConflict('This identity belongs to a different reviewed task. Use a fresh task form.')
                return _existing(s,row,before.business_id,active_key)
            if expected_revision is not None and disclosure_revision(before)!=expected_revision:
                raise CashflowError('Ledger changed since the data preview. Review a fresh preview before sending.')
            with _ACTIVE_LOCK:
                if len(_ACTIVE)>=MAX_ACTIVE_RUNS:raise CashflowError('Two agent requests are already running. Wait for a result; ledger review remains available.')
                _ACTIVE.add(active_key)
            run_id=uuid4().hex;created=agent._utcnow_iso();version=s.state_version()
            try:
                s.conn.execute('INSERT INTO agent_requests VALUES (?,?,?,?,?,?,?,?,?)',
                    (before.business_id,request_id,run_id,question,provider,'running',INSTANCE_ID,created,None))
                s.conn.execute('INSERT INTO agent_request_policies VALUES (?,?,?)',(before.business_id,request_id,data_mode))
                if plan_json is not None:
                    s.conn.execute('INSERT INTO guided_request_tasks VALUES (?,?,?)',(before.business_id,request_id,plan_json))
                s.conn.commit()
            except BaseException:
                with _ACTIVE_LOCK:_ACTIVE.discard(active_key)
                raise
        finally:s.close()
    work=copy.deepcopy(before);deadline=time.monotonic()+time_budget_s
    try:
        try:
            result=agent.run_agent_question(work,question,adapter,run_id=run_id,time_budget_s=time_budget_s,data_mode=data_mode)
        except Exception as exc:
            # Never return provider/transport/tool exception bodies or candidate drafts.
            result=agent.AgentRunResult(run_id,before.business_id,question,adapter.provider_label,
                'This run failed or attempted a disallowed operation. No candidate drafts were saved. Review the current ledger before a fresh question.',[],created,agent._utcnow_iso(),outcome='failed',
                disclosure=getattr(exc,'agent_disclosure',{}),usage=getattr(exc,'agent_usage',{}))
        status='completed' if result.outcome=='completed' else 'failed'
        if status != 'completed':
            from .agent_recovery import failure_category
            result.disclosure['failure_category'] = failure_category(result, adapter)
        with lock:
            s=SQLiteStore(db)
            try:
                current=s.load_ledger();require_access(ctx,current)
                row=_row(s,before.business_id,request_id)
                if row is None or row['status']!='running' or row['run_id']!=run_id:
                    raise CashflowError('Request history changed during the run; no candidate drafts were saved.')
                if s.state_version()!=version or vars(current)!=vars(before):
                    status='stale'
                    replacement=agent.AgentRunResult(run_id,before.business_id,question,adapter.provider_label,
                        'The ledger changed while the model was running. Candidate results and drafts were discarded. Review current facts and ask again with a fresh request.',[],created,agent._utcnow_iso(),outcome='stale')
                    replacement.disclosure,replacement.usage=result.disclosure,result.usage;result=replacement
                    result.disclosure['failure_category']='stale'
                elif not _draft_only(before,work):
                    status='failed'
                    replacement=agent.AgentRunResult(run_id,before.business_id,question,adapter.provider_label,
                        'The run exceeded its read/DRAFT boundary. No candidate drafts or financial changes were saved.',[],created,agent._utcnow_iso(),outcome='failed')
                    replacement.disclosure,replacement.usage=result.disclosure,result.usage;result=replacement
                    result.disclosure['failure_category']='failed'
                elif time.monotonic()>=deadline:
                    status='failed'
                    replacement=agent.AgentRunResult(run_id,before.business_id,question,adapter.provider_label,
                        'The request deadline expired before saving its result. No candidate drafts were saved. Review current facts before a fresh question.',[],created,agent._utcnow_iso(),outcome='timeout')
                    replacement.disclosure,replacement.usage=result.disclosure,result.usage;result=replacement
                    result.disclosure['failure_category']='timeout'
                elif status!='completed':
                    result.tool_calls=[]
                    result.answer='The run did not complete within its provider/tool/deadline limits. No candidate drafts were saved. Review the current ledger before a fresh question.'
                try:
                    s.conn.execute('BEGIN IMMEDIATE')
                    if status=='completed':s.save_ledger(work,commit=False)
                    _write_run(s.conn,result)
                    s.conn.execute('UPDATE agent_requests SET status=?,finished_at=? WHERE business_id=? AND request_id=?',
                        (status,result.finished_at,before.business_id,request_id))
                    s.conn.commit()
                except BaseException:
                    s.conn.rollback();raise
            finally:s.close()
        return RequestOutcome(request_id,status,result,note=(
            'Run history and any new DRAFTs were saved together. This is a snapshot result; later ledger changes require a fresh question.' if status=='completed' else
            'No candidate drafts were saved. The old request will not run again.'))
    finally:
        with _ACTIVE_LOCK:_ACTIVE.discard(active_key)
