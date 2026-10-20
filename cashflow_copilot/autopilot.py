"""Opt-in, persisted deterministic monitoring and DRAFT preparation.

No model, network, approval, observation or payment capability. Each accepted
policy is business/workspace scoped. Effects and a recovery journal commit in
one ledger transaction; the separate scheduling index can recover that result.
"""
import copy,json,re,sqlite3,threading,time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import web
from .authz import OP_READ,RequestContext,require_access
from .errors import CashflowError
from .planning import _hash
from .store import SQLiteStore
from .visitors import VisitorError,Workspace

MAX_DRAFTS=8
MAX_JOBS_DAY=48
SCHEMA='''
CREATE TABLE IF NOT EXISTS policies (
 workspace TEXT PRIMARY KEY,business TEXT NOT NULL,revision TEXT NOT NULL,
 policy TEXT NOT NULL,next_check REAL NOT NULL,last_basis TEXT NOT NULL,
 last_status TEXT NOT NULL,last_digest REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY,workspace TEXT NOT NULL,business TEXT NOT NULL,
 revision TEXT NOT NULL,basis TEXT NOT NULL,kind TEXT NOT NULL,
 status TEXT NOT NULL,created REAL NOT NULL,claim TEXT NOT NULL,
 lease_until REAL NOT NULL,result TEXT NOT NULL,notified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_workspace ON jobs(workspace,created);
CREATE TABLE IF NOT EXISTS job_attempts (id TEXT PRIMARY KEY,workspace TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS policy_progress (workspace TEXT PRIMARY KEY,last_job TEXT NOT NULL);
'''
JOURNAL='''CREATE TABLE IF NOT EXISTS autopilot_runs (
 id TEXT PRIMARY KEY,business TEXT NOT NULL,result TEXT NOT NULL);'''


def basis(ledger):
    # Our own drafts, audit reads and saved report metadata never retrigger
    # financial preparation. Review/status changes have a separate fingerprint.
    excluded={'actions','audit','agent_workflows','forecast_snapshots','demo_checks','scenarios'}
    data={k:v for k,v in vars(ledger).items() if k not in excluded}
    data['owner_operations']={k:v for k,v in ledger.owner_operations.items() if v.get('kind')!='forecast_snapshot_saved'}
    return _hash(data)


def status_basis(ledger):
    return _hash(dict(actions=[(a.id,a.state,a.version) for a in ledger.actions.values()],
        workflows=[(j['id'],j['status']) for j in ledger.agent_workflows.values()]))


def defaults(ledger):
    currencies=sorted({a.currency for a in ledger.accounts.values()})
    return dict(enabled=False,interval_seconds=300,digest_seconds=86400,
        strategy='off',caps={c:0 for c in currencies},snapshot=True,notify=False)


def validate(policy,ledger):
    expected={'enabled','interval_seconds','digest_seconds','strategy','caps','snapshot','notify'}
    if not isinstance(policy,dict) or set(policy)!=expected:raise CashflowError('Use the current Autopilot policy form.')
    for key in ['enabled','snapshot','notify']:
        if type(policy[key]) is not bool:raise CashflowError('Autopilot switches must be boolean.')
    for key,low,high in [('interval_seconds',30,3600),('digest_seconds',3600,604800)]:
        if type(policy[key]) is not int or not low<=policy[key]<=high:raise CashflowError('Autopilot timing is outside its supported range.')
    if policy['strategy'] not in {'off','auto-collections','auto-deferrals','auto-combined'}:raise CashflowError('Choose a listed deterministic strategy.')
    currencies={a.currency for a in ledger.accounts.values()}
    if not isinstance(policy['caps'],dict) or set(policy['caps'])!=currencies:raise CashflowError('Set a separate DRAFT limit for each recorded account currency.')
    if any(type(v) is not int or not 0<=v<=10**14 for v in policy['caps'].values()):raise CashflowError('DRAFT limits must be nonnegative integer minor units.')
    return copy.deepcopy(policy)


def analyze(ledger,policy,ident,kind,deadline):
    """Private candidate only; all figures/choices come from existing tools."""
    from .adaptive import generate_proposals
    from .agent import execute_tool
    from .forecast import forecast
    from . import agent_workflows,planning
    def check():
        if time.monotonic()>=deadline:raise TimeoutError()
    check();ctx=RequestContext('autopilot','agent',ledger.business_id)
    fc=forecast(ledger,ledger.as_of,ledger.horizon_days)
    result=dict(as_of=str(ledger.as_of),trigger=kind,provider='local deterministic Autopilot; no model call',
        currencies={c:dict(minimum_minor=v.min_balance_minor,closing_minor=v.closing_minor,
            shortfall_minor=v.worst_shortfall_minor,first_breach=str(v.first_breach_date) if v.first_breach_date else None)
            for c,v in fc.currencies.items()},overdue=[],missing_facts=[],plans=[],draft_ids=[],workflow_ids=[],notes=[])
    for rec in ledger.receivables.values():
        if rec.remaining_minor and rec.due_date is not None and rec.due_date<ledger.as_of:
            result['overdue'].append(dict(id=rec.id,currency=rec.currency,amount_minor=rec.remaining_minor,due=str(rec.due_date),evidence_id=rec.evidence_id))
    facts=execute_tool(ctx,ledger,'missing_facts',{},provider_label=result['provider'])['result']
    result['missing_facts']=facts.get('clarifications',[])
    check();proposals=generate_proposals(ledger,ctx=ctx)
    result['plans']=[dict(id=p.id,financially_sufficient=p.financially_sufficient,blockers=p.blockers,evidence_ids=p.evidence_ids,
        pending_permissions=p.pending_permissions,notes=p.notes) for p in proposals]
    result['no_feasible_plan']=not any(p.financially_sufficient for p in proposals)
    check()
    if kind=='financial_change' and policy['strategy']!='off' and any(c.worst_shortfall_minor>0 for c in fc.currencies.values()):
        proposal=next(p for p in proposals if p.id==policy['strategy'])
        if proposal.blockers:result['notes'].append('Missing/source facts block automatic DRAFT preparation. Review Missing facts.')
        elif not proposal.financially_sufficient:result['notes'].append('No sufficient plan in the selected bounded strategy. No speculative DRAFT was prepared.')
        elif not proposal.drafts:result['notes'].append('No new DRAFT terms are needed.')
        elif len(proposal.drafts)>MAX_DRAFTS:result['notes'].append('The plan exceeds the eight-DRAFT batch limit; use detailed Web review.')
        elif any(s['amount_minor']>policy['caps'].get(s['currency'],0) for s in proposal.drafts):
            result['notes'].append('The selected plan exceeds a currency-specific DRAFT limit. No partial plan was prepared.')
        else:
            # Existing domain preview refuses overlaps, missing evidence,
            # protected payroll, bad source facts, consent and currency misuse.
            try:
                preview=planning.plan_draft_preview(ledger,proposal.id,ctx=ctx);check()
                result['draft_ids']=planning.create_plan_drafts(ledger,proposal.id,preview['revision'],ctx=ctx)['action_ids']
            except CashflowError:
                result['notes'].append('Existing source/overlap/permission checks require detailed plan review; no new plan batch was prepared.')
    check()
    if kind=='financial_change' and policy['snapshot']:
        label='Autopilot review '+str(ledger.as_of)
        steps=[dict(operation='save_forecast',arguments=dict(label=label))]
        existing=[j for j in ledger.agent_workflows.values() if j['status']=='pending_approval' and j['steps']==steps and j['basis']==agent_workflows.revision(ledger)]
        if existing:result['workflow_ids']=[existing[0]['id']]
        else:
            try:
                job=agent_workflows.prepare(ledger,'autopilot-'+ident[:32],label,steps,ctx=ctx)
                result['workflow_ids']=[job['id']]
            except CashflowError:result['notes'].append('The pending-review queue is full or this snapshot needs detailed review.')
    result['actions']=[dict(id=a.id,state=a.state) for a in ledger.actions.values()][:100]
    result['workflows']=[dict(id=j['id'],status=j['status']) for j in ledger.agent_workflows.values()][:100]
    check();return result


class Service:
    def __init__(self,server,*,clock=time.time,analyzer=analyze):
        self.server=server;self.clock=clock;self.analyzer=analyzer;self.stop=threading.Event();self.thread=None
        self.processing=threading.Lock()
        database=getattr(server,'workspace_database',None) or server.config.database
        self.database=str(Path(database).resolve());self.path=Path(self.database).parent/'autopilot-control.sqlite'
        with self.db() as db:db.executescript(SCHEMA)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def identity(self,database):
        visitors=getattr(self.server,'visitors',None)
        if visitors:return visitors.workspace_digest(database)
        if str(Path(database).resolve())!=self.database:raise CashflowError('Use the active Autopilot workspace.')
        return _hash(self.database)

    @contextmanager
    def workspace(self,key):
        visitors=getattr(self.server,'visitors',None)
        if visitors:
            with visitors.lease_digest(key) as state:
                with web.workspace_scope(state):yield
        else:
            if key!=self.identity(self.database):raise CashflowError('Unknown Autopilot workspace.')
            with web.workspace_scope(Workspace(self.database,web.CSRF_TOKEN)):yield

    def policy(self,database,ledger,*,ctx):
        require_access(ctx,ledger,OP_READ);key=self.identity(database)
        with self.db() as db:row=db.execute('SELECT * FROM policies WHERE workspace=?',(key,)).fetchone()
        if row and row['business']!=ledger.business_id:raise CashflowError('Autopilot policy belongs to a different business; stop it before changing business.')
        return dict(row) if row else dict(workspace=key,business=ledger.business_id,revision='',policy=json.dumps(defaults(ledger)),next_check=0,last_basis='',last_status='',last_digest=0)

    def save(self,database,ledger,policy,*,ctx,expected):
        require_access(ctx,ledger);policy=validate(policy,ledger);key=self.identity(database)
        if policy['notify']:
            bridge=getattr(self.server,'telegram',None)
            if not bridge or not self.linked(database):raise CashflowError('Link your own private Telegram workspace before enabling Autopilot delivery.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM policies WHERE workspace=?',(key,)).fetchone()
            if not row and db.execute('SELECT count(*) FROM policies').fetchone()[0]>=256:raise CashflowError('Autopilot workspace capacity reached.')
            if (row['revision'] if row else '')!=expected:raise CashflowError('Autopilot settings changed. Reload the policy before saving.')
            if row and row['business']!=ledger.business_id:raise CashflowError('The Autopilot business changed.')
            previous=json.loads(row['policy']) if row else None
            replan=previous is None or any(previous[k]!=policy[k] for k in ['strategy','caps','snapshot'])
            db.execute('INSERT OR REPLACE INTO policies VALUES (?,?,?,?,?,?,?,?)',
                (key,ledger.business_id,uuid4().hex,json.dumps(policy),self.clock(),row['last_basis'] if row and not replan else '',row['last_status'] if row else '',row['last_digest'] if row else 0))
            db.execute("UPDATE jobs SET status='cancelled' WHERE workspace=? AND status IN ('queued','running')",(key,))
        bridge=getattr(self.server,'telegram',None)
        if bridge and (not policy['enabled'] or not policy['notify']):
            with bridge.db() as db:db.execute("UPDATE deliveries SET status='cancelled' WHERE status='queued' AND id IN (SELECT delivery_id FROM delivery_keys WHERE key LIKE ?)",('ap:'+key+':%',))
        return policy

    def linked(self,database):
        bridge=getattr(self.server,'telegram',None)
        if bridge is None:return False
        with bridge.db() as db:return bool(db.execute('SELECT 1 FROM binding WHERE id=1 AND digest=?',(self.identity(database),)).fetchone())

    def history(self,database,ledger,*,ctx):
        require_access(ctx,ledger,OP_READ);key=self.identity(database)
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM jobs WHERE workspace=? AND business=? ORDER BY created DESC,rowid DESC LIMIT 30',(key,ledger.business_id))]

    def enqueue(self,key,*,force=False):
        with self.workspace(key):
            ledger=web.read_state(lambda l:l);current=basis(ledger);states=status_basis(ledger);now=self.clock()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM policies WHERE workspace=?',(key,)).fetchone()
                if not row or row['business']!=ledger.business_id:return None
                policy=json.loads(row['policy'])
                if not policy['enabled'] or (not force and now<row['next_check']):return None
                if force and row['next_check']>now+policy['interval_seconds']-30:return None
                db.execute('UPDATE policies SET next_check=? WHERE workspace=?',(now+policy['interval_seconds'],key))
                active=db.execute("SELECT id FROM jobs WHERE workspace=? AND status IN ('queued','running') ORDER BY created LIMIT 1",(key,)).fetchone()
                if active:return active[0]
                kind='financial_change' if current!=row['last_basis'] else 'follow_up' if states!=row['last_status'] else 'digest' if now-row['last_digest']>=policy['digest_seconds'] else ''
                if not kind:return None
                db.execute('DELETE FROM job_attempts WHERE created<?',(now-86400,))
                if db.execute('SELECT count(*) FROM job_attempts WHERE workspace=? AND created>=?',(key,now-86400)).fetchone()[0]>=MAX_JOBS_DAY:return None
                db.execute("DELETE FROM jobs WHERE workspace=? AND status NOT IN ('queued','running') AND rowid NOT IN (SELECT rowid FROM jobs WHERE workspace=? ORDER BY created DESC,rowid DESC LIMIT 30)",(key,key))
                if db.execute('SELECT count(*) FROM jobs').fetchone()[0]>=1024:return None
                slot=int(now//policy['digest_seconds']) if kind=='digest' else states if kind=='follow_up' else ''
                progress=db.execute('SELECT last_job FROM policy_progress WHERE workspace=?',(key,)).fetchone()
                ident=_hash([key,row['revision'],current,kind,slot,progress[0] if progress else ''])[:40]
                inserted=db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,0)',(ident,key,ledger.business_id,row['revision'],current,kind,'queued',now,'',0,'{}')).rowcount
                if inserted:db.execute('INSERT INTO job_attempts VALUES (?,?,?)',(ident,key,now))
                return ident

    def execute(self,ident):
        from .agent_requests import _draft_only
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock()
            db.execute("UPDATE jobs SET status='queued',claim='' WHERE id=? AND status='running' AND lease_until<=?",(ident,now))
            job=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
            if not job or job['status']!='queued':return
            claim=uuid4().hex;db.execute("UPDATE jobs SET status='running',claim=?,lease_until=? WHERE id=?",(claim,now+30,ident))
            job=dict(job)
        deadline=time.monotonic()+10;committed=False
        def finish(status,result=None):
            with self.db() as db:
                if committed:
                    db.execute("UPDATE jobs SET lease_until=0 WHERE id=? AND claim=? AND status='running'",(ident,claim))
                else:db.execute('UPDATE jobs SET status=?,result=? WHERE id=? AND claim=? AND status=\'running\'',(status,json.dumps(result or {}),ident,claim))
        try:
            with self.workspace(job['workspace']):
                with web.LOCK:
                    store=SQLiteStore(web.database_path())
                    try:
                        before=store.load_ledger();version=store.state_version();store.conn.executescript(JOURNAL)
                        saved=store.conn.execute('SELECT business,result FROM autopilot_runs WHERE id=?',(ident,)).fetchone()
                    finally:store.close()
                if before.business_id!=job['business']:raise CashflowError('Autopilot business changed.')
                if saved:
                    if saved[0]!=job['business']:raise CashflowError('Autopilot recovery scope changed.')
                    committed=True;result=json.loads(saved[1])
                    with self.db() as db:
                        db.execute('BEGIN IMMEDIATE');active=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
                        if not active or active['status']!='running' or active['claim']!=claim:return
                        row=db.execute('SELECT revision FROM policies WHERE workspace=?',(job['workspace'],)).fetchone()
                        db.execute("UPDATE jobs SET status='completed',result=? WHERE id=?",(saved[1],ident))
                        if row and row[0]==job['revision']:
                            db.execute('UPDATE policies SET last_basis=?,last_status=?,last_digest=? WHERE workspace=?',
                                (job['basis'],result.get('state_after',''),self.clock(),job['workspace']))
                            db.execute('INSERT OR REPLACE INTO policy_progress VALUES (?,?)',(job['workspace'],ident))
                    return
                with self.db() as db:row=db.execute('SELECT * FROM policies WHERE workspace=?',(job['workspace'],)).fetchone()
                if not row or row['revision']!=job['revision'] or not json.loads(row['policy'])['enabled']:finish('cancelled');return
                if before.business_id!=job['business'] or basis(before)!=job['basis']:finish('stale');return
                work=copy.deepcopy(before);policy=json.loads(row['policy'])
                result=self.analyzer(work,policy,ident,job['kind'],deadline)
                result['state_after']=status_basis(work)
                if len(json.dumps(result).encode())>64000:raise CashflowError('Autopilot result exceeds its bound.')
                if not _draft_only(before,work):raise CashflowError('Autopilot exceeded its read/DRAFT boundary.')
                with web.LOCK,self.db() as control:
                    control.execute('BEGIN IMMEDIATE')
                    active=control.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
                    current_policy=control.execute('SELECT * FROM policies WHERE workspace=?',(job['workspace'],)).fetchone()
                    if self.stop.is_set() or not active or active['status']!='running' or active['claim']!=claim or not current_policy or current_policy['revision']!=job['revision'] or not json.loads(current_policy['policy'])['enabled']:
                        return
                    if time.monotonic()>=deadline or self.clock()>=active['lease_until']:raise TimeoutError()
                    store=SQLiteStore(web.database_path())
                    try:
                        store.conn.execute('BEGIN IMMEDIATE');current=store.load_ledger()
                        if vars(current)!=vars(before) or store.state_version()!=version:
                            control.execute("UPDATE jobs SET status='stale' WHERE id=?",(ident,));return
                        store.save_ledger(work,commit=False)
                        store.conn.execute('INSERT INTO autopilot_runs VALUES (?,?,?)',(ident,job['business'],json.dumps(result)))
                        if time.monotonic()>=deadline:raise TimeoutError()
                        store.conn.commit()
                        committed=True
                    finally:store.close()
                    control.execute("UPDATE jobs SET status='completed',result=? WHERE id=?",(json.dumps(result),ident))
                    control.execute('UPDATE policies SET last_basis=?,last_status=?,last_digest=? WHERE workspace=?',
                        (basis(work),status_basis(work),self.clock(),job['workspace']))
                    control.execute('INSERT OR REPLACE INTO policy_progress VALUES (?,?)',(job['workspace'],ident))
        except TimeoutError:finish('timeout',{'notice':'Deadline expired; no candidate DRAFTs were saved.'})
        except (CashflowError,VisitorError,OSError,sqlite3.Error):finish('blocked',{'notice':'Source, workspace, capacity or boundary checks stopped this job. Review current facts.'})
        except Exception:finish('failed',{'notice':'Job stopped safely. No provider or internal exception body is exposed.'})

    def tick(self,key=None,*,force=False):
        if not self.processing.acquire(False):return
        try:
            visitors=getattr(self.server,'visitors',None)
            if visitors is not None and not getattr(visitors,'durable',False) and getattr(visitors,'registry',None):
                with sqlite3.connect(visitors.registry) as registry:
                    live={r[0] for r in registry.execute('SELECT digest FROM visitors WHERE expires>?',(visitors.clock(),))}
                with self.db() as db:
                    for r in db.execute('SELECT workspace FROM policies').fetchall():
                        if r[0] not in live:
                            db.execute('DELETE FROM policies WHERE workspace=?',(r[0],));db.execute('DELETE FROM jobs WHERE workspace=?',(r[0],));db.execute('DELETE FROM job_attempts WHERE workspace=?',(r[0],));db.execute('DELETE FROM policy_progress WHERE workspace=?',(r[0],))
            with self.db() as db:
                waiting={r[0] for r in db.execute("SELECT DISTINCT workspace FROM jobs WHERE status='queued' OR (status='running' AND lease_until<=?)",(self.clock(),))}
                keys=[key] if key else [r['workspace'] for r in db.execute('SELECT * FROM policies ORDER BY next_check') if json.loads(r['policy'])['enabled'] and (r['next_check']<=self.clock() or r['workspace'] in waiting)][:4]
            for current in keys:
                if self.stop.is_set():break
                try:self.enqueue(current,force=force)
                except (VisitorError,CashflowError):
                    with self.db() as db:db.execute("UPDATE policies SET next_check=? WHERE workspace=?",(self.clock()+3600,current))
                with self.db() as db:
                    jobs=[r[0] for r in db.execute("SELECT id FROM jobs WHERE workspace=? AND (status='queued' OR (status='running' AND lease_until<=?)) ORDER BY created LIMIT 2",(current,self.clock()))]
                for ident in jobs:
                    if not self.stop.is_set():self.execute(ident)
        finally:self.processing.release()

    def start(self):
        def run():
            while not self.stop.is_set():
                try:self.tick()
                except Exception:pass # safe state remains in persisted job history
                self.stop.wait(5)
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=run,name='fluxora-autopilot',daemon=False);self.thread.start()

    def notify(self,bridge):
        """Queue once to the currently paired owner; flush owns real transport.

        No public visitor can select another workspace, recipient or endpoint.
        All data and reviews are escaped server cards; unknown sends stay unknown.
        """
        # Disabled/unlinked Autopilot must not compete with ordinary bot stop
        # or configuration operations for the bridge's busy-task lock.
        with bridge.db() as db:binding=db.execute('SELECT digest FROM binding WHERE id=1').fetchone()
        if not binding:return
        with self.db() as db:row=db.execute('SELECT policy FROM policies WHERE workspace=?',(binding[0],)).fetchone()
        if not row:return
        policy=json.loads(row[0])
        if not policy['enabled'] or not policy['notify']:return
        from . import telegram_copy as words
        from .money import format_amount
        from . import agent_workflows
        try:
            with bridge.lock,bridge.workspace() as binding:
                key=self.identity(web.database_path());ledger=web.read_state(lambda l:l)
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT * FROM policies WHERE workspace=?',(key,)).fetchone()
                    if not row or row['business']!=ledger.business_id:return
                    policy=json.loads(row['policy'])
                    if not policy['enabled'] or not policy['notify']:return
                    job=db.execute("SELECT * FROM jobs WHERE workspace=? AND business=? AND revision=? AND notified=0 AND status IN ('completed','timeout','failed','blocked','stale') ORDER BY created,rowid LIMIT 1",(key,ledger.business_id,row['revision'])).fetchone()
                    if not job:return
                    result=json.loads(job['result']);prefix='ap:'+key+':'+binding['generation']+':'+job['id']
                    blocks=['Saved run: '+words.code(job['kind'])+'\nStatus: '+words.text(job['status'])+'\nSnapshot: '+words.code(result.get('as_of',str(ledger.as_of)))]
                    for currency,value in result.get('currencies',{}).items():
                        blocks.append(words.bold(currency)+'\nProjected minimum: '+words.text(format_amount(value['minimum_minor'],currency))+'\nGap to floor: '+words.text(format_amount(value['shortfall_minor'],currency))+'\nFirst breach: '+words.code(value['first_breach'] or 'none under assumptions'))
                    if result.get('no_feasible_plan'):blocks.append('No sufficient plan within the three bounded strategies. No consent, receipt or funding was invented.')
                    blocks.append('Prepared DRAFTs: '+str(len(result.get('draft_ids',[])))+'\nPending snapshot reviews: '+str(len(result.get('workflow_ids',[])))+'\nNo approval, sending or payment was performed by Autopilot.')
                    blocks.append('Inspect current terms in Web → Autopilot. Projections are conditional. /autopilot off stops future preparations and queued Autopilot updates.')
                    try:bridge.queue(words.card('Autopilot update',*blocks),key=prefix+':summary')
                    except CashflowError:
                        bridge.queue(words.card('Autopilot run needs Web review','Status: '+words.text(job['status']),
                            'The full multi-currency result does not fit this card. Open Web → Autopilot for all recorded figures and evidence. No result has been shortened into a financial conclusion.'),key=prefix+':summary')
                    if job['status']=='completed':
                        for action in [a for a in ledger.actions.values() if a.actor=='autopilot' and a.state=='draft'][:MAX_DRAFTS]:
                            bridge._action_card(binding,ledger,action,key=prefix+':action:'+action.id+':'+agent_workflows.revision(ledger))
                        for workflow in [j for j in ledger.agent_workflows.values() if j['prepared_by']=='autopilot' and j['status']=='pending_approval'][:1]:
                            if workflow['basis']!=agent_workflows.revision(ledger):
                                bridge.queue('A pending Autopilot snapshot is stale after recorded changes. Open Web to inspect it and prepare a fresh snapshot when needed.',key=prefix+':workflow-stale')
                                continue
                            try:bridge._workflow_card(binding,ledger,workflow,key=prefix+':workflow:'+workflow['id'])
                            except CashflowError:bridge.queue('A pending Autopilot snapshot needs detailed Web review.',key=prefix+':workflow-web')
                    db.execute('UPDATE jobs SET notified=1 WHERE id=?',(job['id'],))
        except (CashflowError,VisitorError,OSError,sqlite3.Error):return

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(20)
