"""Private owner Telegram control plane; opaque, expiring, exact review tokens.

Runs inside the hosted process and reuses its visitor leases and ledger lock.
No public webhook or client-selected filesystem path. Transport is replaceable
with a local stub; no Telegram/model calls occur unless explicitly configured.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from uuid import uuid4

from . import agent_tasks as tasks,agent_workflows as workflows,web
from .authz import RequestContext
from .errors import CashflowError
from .telegram_api import TelegramAPI,TelegramError
from .visitors import VisitorError
from . import telegram_copy as wording


class TelegramNotice(CashflowError):
    """Known server-owned reason; never display a provider/domain exception."""
    def __init__(self,reason):self.reason=reason;super().__init__(reason)

SCHEMA='''
CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL,
 generation TEXT NOT NULL, alerts INTEGER NOT NULL, alert_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pairs (hash TEXT PRIMARY KEY,digest TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (token TEXT PRIMARY KEY,generation TEXT NOT NULL,digest TEXT NOT NULL,
 business TEXT NOT NULL,revision TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


@dataclass(frozen=True)
class TelegramConfig:
    owner_id:int
    bot_name:str=''
    use_model:bool=False
    def __post_init__(self):
        if type(self.owner_id) is not int or self.owner_id<=0:raise ValueError('Set the private Telegram owner numeric user ID.')
        if self.bot_name and not re.fullmatch('[A-Za-z0-9_]{5,32}',self.bot_name):raise ValueError('Invalid public bot username.')


def configured():
    mode=os.environ.get('FLUXORA_TELEGRAM_ENABLED','0')
    if mode not in {'0','1'}:raise ValueError('Telegram enabled flag must be 0 or 1.')
    if mode=='0':return None
    owner=os.environ.get('FLUXORA_TELEGRAM_OWNER_ID','')
    if not re.fullmatch('[1-9][0-9]{0,15}',owner):raise ValueError('Set the private Telegram owner numeric user ID.')
    model=os.environ.get('FLUXORA_TELEGRAM_USE_MODEL','0')
    if model not in {'0','1'}:raise ValueError('Telegram model flag must be 0 or 1.')
    api=TelegramAPI(os.environ.get('FLUXORA_TELEGRAM_BOT_TOKEN',''))
    return TelegramConfig(int(owner),os.environ.get('FLUXORA_TELEGRAM_BOT_NAME',''),model=='1'),api


class TelegramBridge:
    def __init__(self,server,config,api,*,adapter=None,clock=time.time):
        if server.visitors is None:raise ValueError('Telegram currently requires hosted visitor workspaces.')
        self.server=server;self.config=config;self.api=api;self.adapter=adapter;self.clock=clock
        database=getattr(server,'workspace_database',None) or server.config.database
        self.path=Path(database).resolve().parent/'telegram-control.sqlite'
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None
        self.connection='not checked';self.last_error='';self.next_alert=0
        with self.db() as db:
            db.executescript(SCHEMA)
            owner=db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()
            if owner and owner[0]!=str(config.owner_id):
                db.execute('DELETE FROM binding');db.execute('DELETE FROM pairs')
                db.execute("UPDATE reviews SET status='revoked' WHERE status='pending'")
                db.execute("UPDATE deliveries SET status='cancelled' WHERE status='queued'")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('owner',?)",(str(config.owner_id),))
            # Ambiguous sends are never repeated automatically after restart.
            db.execute("UPDATE deliveries SET status='unknown' WHERE status='sending'")
            db.execute("UPDATE updates SET status='interrupted' WHERE status='running'")
            db.execute("UPDATE reviews SET status='interrupted' WHERE status='running'")

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def pairing(self,database):
        digest=self.server.visitors.workspace_digest(database)
        # Only an already selected visitor workspace can issue a pairing code.
        if self.server.visitors.database_for_digest(digest)!=Path(database).resolve():raise CashflowError('Use the selected workspace.')
        code=secrets.token_hex(16);now=self.clock()
        with self.lock,self.db() as db:
            db.execute('DELETE FROM pairs WHERE expires<=? OR digest=?',(now,digest))
            if db.execute('SELECT count(*) FROM pairs').fetchone()[0]>=64:raise CashflowError('Pairing capacity reached.')
            db.execute('INSERT INTO pairs VALUES (?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),digest,now+300))
        return code

    def state(self,database):
        digest=self.server.visitors.workspace_digest(database)
        with self.lock,self.db() as db:
            row=db.execute('SELECT * FROM binding WHERE id=1 AND digest=?',(digest,)).fetchone()
            uncertain=db.execute("SELECT count(*) FROM deliveries WHERE status='unknown'").fetchone()[0] if row else 0
        return dict(linked=bool(row),alerts=bool(row['alerts']) if row else False,uncertain_deliveries=uncertain,
            connection=self.connection,model='private live model' if self.adapter else 'local commands only; no live model')

    def disconnect(self,database):
        digest=self.server.visitors.workspace_digest(database)
        with self.lock,self.db() as db:
            row=db.execute('SELECT * FROM binding WHERE id=1 AND digest=?',(digest,)).fetchone()
            if row:
                db.execute('DELETE FROM binding WHERE id=1')
                db.execute("UPDATE reviews SET status='revoked' WHERE generation=? AND status='pending'",(row['generation'],))
                db.execute("UPDATE deliveries SET status='cancelled' WHERE status='queued'")
            db.execute('DELETE FROM pairs WHERE digest=?',(digest,))

    @contextmanager
    def workspace(self):
        with self.db() as db:row=db.execute('SELECT * FROM binding WHERE id=1').fetchone()
        if row is None:raise TelegramNotice('not_linked')
        try:
            with self.server.visitors.lease_digest(row['digest']) as state:
                with web.workspace_scope(state):yield dict(row)
        except VisitorError as error:
            if error.code==409:
                self.disconnect(str(self.server.visitors.database_for_digest(row['digest'])))
                raise TelegramNotice('workspace_expired') from None
            raise CashflowError('The linked workspace is temporarily unavailable; review in Web.') from None

    def _payload(self,text,buttons=None):
        if not isinstance(text,str):raise TelegramNotice('oversize')
        rendered=text if isinstance(text,wording.Card) else wording.plain(text)
        # Conservative markup-inclusive UTF-16 limit: never truncate exact terms.
        if len(rendered.encode('utf-16-le'))//2>3800:raise TelegramNotice('oversize')
        payload={'chat_id':self.config.owner_id,'text':str(rendered),'parse_mode':'HTML','link_preview_options':{'is_disabled':True}}
        if buttons:payload['reply_markup']={'inline_keyboard':buttons}
        return payload

    def queue(self,text,buttons=None):
        payload=self._payload(text,buttons)
        with self.db() as db:
            if db.execute("SELECT count(*) FROM deliveries WHERE status='queued'").fetchone()[0]>=32:raise CashflowError('Telegram delivery queue full.')
            db.execute('INSERT INTO deliveries(payload,status) VALUES (?,?)',(json.dumps(payload),'queued'))

    def flush(self):
        with self.lock,self.db() as db:
            row=db.execute("SELECT * FROM deliveries WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:db.execute("UPDATE deliveries SET status='sending' WHERE id=?",(row['id'],))
        if not row:return
        try:self.api.call('sendMessage',json.loads(row['payload']));status='sent'
        except TelegramError:status='unknown';self.last_error='Delivery uncertain; use /status or Web. No automatic send retry.'
        with self.db() as db:db.execute('UPDATE deliveries SET status=? WHERE id=?',(status,row['id']))

    def _load(self):
        return web.read_state(lambda ledger:ledger)

    def _review(self,binding,ledger,kind,payload,text,confirm):
        token=secrets.token_hex(12)
        buttons=[[{'text':confirm,'callback_data':'yes:'+token},{'text':'Reject','callback_data':'no:'+token}]]
        self._payload(text,buttons)  # Refuse oversized terms before storing a review.
        with self.db() as db:
            db.execute('DELETE FROM reviews WHERE expires<?',(self.clock()-86400,))
            if db.execute("SELECT count(*) FROM reviews WHERE status='pending' AND expires>?",(self.clock(),)).fetchone()[0]>=40:
                raise CashflowError('Review capacity reached. Reject or let old reviews expire.')
            db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)',(token,binding['generation'],binding['digest'],ledger.business_id,
                workflows.revision(ledger),kind,json.dumps(payload),self.clock()+900,'pending'))
        self.queue(text,buttons)

    def _plan(self,binding,ledger,plan,question):
        plan=tasks.validate_plan(ledger,plan,question,ctx=RequestContext('telegram-reader','agent',ledger.business_id))
        text=wording.review(plan,tasks.title(plan['task']),ledger.as_of)
        self._review(binding,ledger,'task',{'plan':plan,'question':question,'request_id':uuid4().hex},text,
            'Create DRAFT' if plan['task']=='action_proposal' else 'Prepare for approval' if plan['task']=='proposal' else 'Run reviewed report')

    def _report(self,ledger,task):
        from .agent import execute_tool
        name,args=tasks.tool_for({'task':task,'parameters':{}})
        reply=execute_tool(RequestContext('telegram-report','agent',ledger.business_id),ledger,name,args,provider_label='local deterministic report; no model')
        text=wording.forecast(reply['result']) if task=='forecast' else wording.plans(reply['result'])
        try:self._payload(text)
        except TelegramNotice:
            text=wording.card('Report ready in Web','The complete report exceeds the Telegram message limit. Open '+('Forecast' if task=='forecast' else 'Plan Comparison')+' in your linked browser for all currencies, assumptions and source references. No report values were shortened into a financial conclusion.')
        self.queue(text)

    def _workflow_card(self,binding,ledger,job):
        if (any(s['operation']!='save_forecast' for s in job['steps']) or job['requires_observed_confirmation'] or job['requires_counterparty_confirmation']):
            self.queue('Pending workflow '+job['id']+' needs detailed Web review. Telegram approval is available only for forecast snapshots; observations/consent need separate source-backed confirmation.');return
        text=wording.snapshot_review(ledger,job)
        self._review(binding,ledger,'workflow',{'id':job['id'],'review_token':job['review_token']},text,'Approve & save snapshot')

    def _message(self,message):
        text=message.get('text','')
        if not isinstance(text,str) or not 1<=len(text)<=2000 or message.get('forward_origin') or message.get('via_bot'):
            self.queue('Use a new private text command of at most 2,000 characters. Forwarded messages and attachments are not instructions.');return
        if text.startswith('/link '):
            code=text[6:].strip()
            if not re.fullmatch('[a-f0-9]{32}',code):raise TelegramNotice('pair_expired')
            with self.db() as db:pair=db.execute('SELECT * FROM pairs WHERE hash=? AND expires>?',(hashlib.sha256(code.encode()).hexdigest(),self.clock())).fetchone()
            if pair is None:raise TelegramNotice('pair_expired')
            with self.server.visitors.lease_digest(pair['digest']):
                with self.db() as db:
                    db.execute('DELETE FROM pairs WHERE hash=?',(pair['hash'],))
                    db.execute("UPDATE reviews SET status='revoked' WHERE status='pending'")
                    db.execute("UPDATE deliveries SET status='cancelled' WHERE status='queued'")
                    db.execute('INSERT OR REPLACE INTO binding VALUES (1,?,?,0,?)',(pair['digest'],uuid4().hex,''))
            lifetime=('Local data stays on this computer until explicitly reset, restored or removed. Keep this app running for the bot to reply.' if getattr(self.server.visitors,'durable',False) else 'The workspace expires 24 hours after browser creation; linking does not extend it.')
            self.queue(wording.card('Workspace linked','Connected to the browser workspace whose one-time code you entered. Only your configured private account can control this bot.',
                'Next: /forecast for your cash position, or /help for all commands.',
                'Alerts are OFF. Use /alerts on to opt in. '+lifetime));return
        if text in {'/start','/help'}:
            self.queue(wording.help_card());return
        with self.workspace() as binding:
            ledger=self._load()
            if text=='/unlink':self.disconnect(web.database_path());self.queue('Telegram disconnected; pending buttons revoked.');return
            if text=='/status':
                model=self.adapter.status().state if self.adapter else 'not configured'
                self.queue(wording.card('Workspace status',wording.fields(dict(business=ledger.business_id,as_of=str(ledger.as_of),private_model=wording.model_state(model),alerts='on' if binding['alerts'] else 'off')),
                    'Connected to your linked browser workspace. Use /forecast to see the deterministic cash-flow report. Telegram operations never move real money.'));return
            if text in {'/alerts on','/alerts off'}:
                with self.db() as db:db.execute('UPDATE binding SET alerts=?,alert_hash=? WHERE id=1',(int(text.endswith(' on')),''))
                self.queue('Financial-change alerts '+('enabled. Updates leave this workspace via Telegram.' if text.endswith(' on') else 'disabled.'));return
            if text in {'/forecast','/plans'}:self._report(ledger,text[1:]);return
            if text.startswith('/draft '):
                fields=text.split()
                if len(fields)!=5:raise TelegramNotice('draft_format')
                _,target,ccy,amount,day=fields
                plan={'task':'action_proposal','parameters':dict(kind='request_early_payment',target_id=target,currency=ccy,amount=amount,date=day)}
                self._plan(binding,ledger,plan,text);return
            if text.startswith('/snapshot '):
                label=text[len('/snapshot '):].strip()
                plan={'task':'proposal','parameters':{'steps':[{'operation':'save_forecast','arguments':{'label':label}}]}}
                self._plan(binding,ledger,plan,text);return
            if text in {'/verify','/qualify'}:
                if not self.adapter:raise TelegramNotice('model_missing')
                self._review(binding,ledger,'model_check',{'operation':text[1:]},
                    'Private model '+text[1:]+' check. Sends synthetic check prompts to the configured provider; may incur charges. No ledger sent or financial effect. Expires in 15 minutes.',
                    'Run potentially billed check');return
            if text.startswith('/'):raise TelegramNotice('unknown_command')
            if not self.adapter:raise TelegramNotice('model_missing')
            from . import task_qualification as qualification
            if qualification.status(self.qualification_path(),self.adapter)!='qualified':raise TelegramNotice('qualification')
            result=tasks.translate(ledger,text,self.adapter,ctx=RequestContext('telegram-agent','agent',ledger.business_id),deadline=time.monotonic()+30)
            if result['status']!='review':self.queue(tasks.REASONS[result['reason']]);return
            self._plan(binding,ledger,result['plan'],text)

    def qualification_path(self):return self.path.parent/'telegram-model-qualification.json'

    def _callback(self,query):
        data=query.get('data','')
        if not isinstance(data,str) or not re.fullmatch('(yes|no):[a-f0-9]{24}',data):raise CashflowError('Invalid review button.')
        decision,token=data.split(':')
        with self.workspace() as binding:
            with self.db() as db:row=db.execute('SELECT * FROM reviews WHERE token=?',(token,)).fetchone()
            if row is None or row['generation']!=binding['generation'] or row['digest']!=binding['digest']:raise TelegramNotice('old_link')
            if row['status']!='pending':self.queue('This review is already '+row['status']+'. No duplicate operation was run.');return
            if row['expires']<=self.clock():raise TelegramNotice('expired_review')
            if decision=='no':
                if row['kind']=='workflow':
                    payload=json.loads(row['payload'])
                    web.mutate_state(lambda ledger:workflows.decide(ledger,payload['id'],payload['review_token'],'reject',
                        ctx=RequestContext('telegram-owner','owner',ledger.business_id)))
                with self.db() as db:db.execute("UPDATE reviews SET status='rejected' WHERE token=?",(token,))
                self.queue('Review rejected. No financial operation was applied.');return
            ledger=self._load()
            if row['business']!=ledger.business_id or row['revision']!=workflows.revision(ledger):raise TelegramNotice('changed_review')
            payload=json.loads(row['payload'])
            with self.db() as db:db.execute("UPDATE reviews SET status='running' WHERE token=? AND status='pending'",(token,))
            try:
                if row['kind']=='model_check':
                    if not self.adapter:raise CashflowError('Private model unavailable.')
                    from . import task_qualification as qualification
                    if payload['operation']=='verify':state=self.adapter.verify().state
                    else:state=qualification.qualify(self.adapter,self.qualification_path(),total_budget=250,case_budget=25)['status']
                    self.queue('Private model check: '+state+'. This is a bounded check, not a guarantee for arbitrary instructions.')
                elif row['kind']=='task':
                    from .agent_requests import run_request
                    plan=tasks.validate_plan(ledger,payload['plan'],payload['question'],ctx=RequestContext('telegram-agent','agent',ledger.business_id))
                    adapter=tasks.TaskExecutionAdapter(plan,payload['request_id'])
                    from .agent_disclosure import disclosure_revision
                    outcome=run_request(web.database_path(),payload['request_id'],payload['question'],'guided',adapter,
                        ctx=RequestContext('telegram-owner','owner',ledger.business_id),lock=web.LOCK,data_mode='minimized',expected_revision=disclosure_revision(ledger))
                    self.queue(wording.completion(plan,outcome))
                    if plan['task']=='proposal' and outcome.status=='completed':
                        current=self._load();job=current.agent_workflows.get('guided-'+payload['request_id'])
                        if job:self._workflow_card(binding,current,job)
                elif row['kind']=='workflow':
                    def apply(current):
                        job=current.agent_workflows.get(payload['id'])
                        if not job or any(s['operation']!='save_forecast' for s in job['steps']):raise CashflowError('This operation requires Web review.')
                        return workflows.decide(current,payload['id'],payload['review_token'],'approve',ctx=RequestContext('telegram-owner','owner',current.business_id))
                    result=web.mutate_state(apply);self.queue(wording.card('Snapshot saved' if result['status']=='executed' else 'Snapshot workflow update',
                        'Workflow: '+wording.code(result['id'])+'\nStatus: '+wording.text(result['status']),
                        'Next: open saved forecasts in your linked browser. Current recorded cash is unchanged. No payment, receipt or counterparty consent was recorded.'))
                else:raise CashflowError('Unsupported review.')
                status='completed'
            except Exception:
                status='failed'
                with self.db() as db:db.execute('UPDATE reviews SET status=? WHERE token=?',(status,token))
                raise
            with self.db() as db:db.execute('UPDATE reviews SET status=? WHERE token=?',(status,token))

    def handle(self,update):
        if not isinstance(update,dict) or type(update.get('update_id')) is not int:return
        message=update.get('message');query=update.get('callback_query')
        if (not isinstance(message,dict) and not isinstance(query,dict)) or ('edited_message' in update):return
        source=query.get('message',{}) if isinstance(query,dict) else message
        sender=query.get('from',{}) if isinstance(query,dict) else (message or {}).get('from',{})
        chat=source.get('chat',{}) if isinstance(source,dict) else {}
        if (not isinstance(source,dict) or not isinstance(sender,dict) or not isinstance(chat,dict) or sender.get('is_bot') is not False
            or type(sender.get('id')) is not int or sender['id']!=self.config.owner_id
            or chat.get('type')!='private' or type(chat.get('id')) is not int or chat['id']!=self.config.owner_id):return
        if not query and (type(message.get('date')) is not int or not self.clock()-900<=message['date']<=self.clock()+60):return
        with self.lock:
            if self.stop.is_set():return
            with self.db() as db:
                if db.execute('SELECT 1 FROM updates WHERE id=?',(update['update_id'],)).fetchone():return
                db.execute('INSERT INTO updates VALUES (?,?)',(update['update_id'],'running'))
            try:
                if query:self._callback(query)
                else:self._message(message)
                status='completed'
            except TelegramNotice as error:
                self.queue(wording.notice(error.reason));status='refused'
            except (CashflowError,VisitorError):
                self.queue(wording.card('Request needs review','The request could not pass local checks. Check record IDs, currency, amount, date, source requirements and the current link.',
                    'Open Web history to inspect the saved status before preparing a fresh request. Use /help for command formats. No automatic retry.'));status='refused'
            except Exception:
                self.queue('Request stopped safely. Check Web history before trying a fresh request; no automatic retry.');status='failed'
            with self.db() as db:db.execute('UPDATE updates SET status=? WHERE id=?',(status,update['update_id']))
        if query:
            try:self.api.call('answerCallbackQuery',{'callback_query_id':query['id'],'text':'Reviewed. See the bot reply.'})
            except TelegramError:pass

    def notifications(self):
        with self.lock:
            try:
                with self.workspace() as binding:
                    if not binding['alerts']:return
                    ledger=self._load()
                    from .forecast import forecast
                    from .money import format_amount
                    current=forecast(ledger,ledger.as_of,ledger.horizon_days)
                    facts={ccy:{'shortfall':c.worst_shortfall_minor,'date':str(c.first_breach_date)} for ccy,c in current.currencies.items()}
                    pending=sorted(j['id'] for j in ledger.agent_workflows.values() if j['status']=='pending_approval')
                    action_states=sorted((a.id,a.state) for a in ledger.actions.values())
                    workflow_states=sorted((j['id'],j['status']) for j in ledger.agent_workflows.values())
                    fingerprint=hashlib.sha256(json.dumps([facts,pending,action_states,workflow_states],sort_keys=True).encode()).hexdigest()
                    if fingerprint==binding['alert_hash']:return
                    lines=['As of '+wording.code(ledger.as_of)]
                    for ccy,c in current.currencies.items():lines.append(wording.bold(ccy)+'\nWorst projected gap to floor: '+wording.text(format_amount(c.worst_shortfall_minor,ccy))+'\nFirst floor breach: '+wording.text(c.first_breach_date or 'none under assumptions'))
                    counts={state:sum(s==state for _,s in action_states) for state in sorted({s for _,s in action_states})}
                    lines.append('Pending workflows: '+str(len(pending))+'\nActions: '+(', '.join(wording.text(k.replace('_',' '))+': '+str(v) for k,v in counts.items()) or 'none')+'\nExecuted local workflows: '+str(sum(s=='executed' for _,s in workflow_states)))
                    lines.append('Next: /forecast for details, or review pending work in Web. Expected collections remain conditional; no payment or consent is inferred. /alerts off stops these updates.')
                    self.queue(wording.card('Financial update',*lines))
                    # Notify about exact new Web-created snapshot approvals as
                    # well as tasks initiated in Telegram. Never reissue a
                    # failed/expired review automatically.
                    with self.db() as db:
                        reviewed={json.loads(r[0]).get('id') for r in db.execute(
                            "SELECT payload FROM reviews WHERE generation=? AND kind='workflow'",(binding['generation'],))}
                    for ident in pending:
                        if ident not in reviewed:
                            try:self._workflow_card(binding,ledger,ledger.agent_workflows[ident])
                            except CashflowError:self.queue('Pending workflow '+ident+' needs a detailed Web review; its full terms do not fit a Telegram approval card.')
                    with self.db() as db:db.execute('UPDATE binding SET alert_hash=? WHERE id=1',(fingerprint,))
            except (CashflowError,VisitorError):return

    def poll_once(self):
        with self.db() as db:
            db.execute('DELETE FROM pairs WHERE expires<=?',(self.clock(),))
            db.execute("UPDATE reviews SET status='expired' WHERE status='pending' AND expires<=?",(self.clock(),))
            db.execute('DELETE FROM reviews WHERE expires<?',(self.clock()-7*86400,))
            db.execute("DELETE FROM deliveries WHERE status IN ('sent','unknown','cancelled') AND id < (SELECT coalesce(max(id),0)-5000 FROM deliveries)")
            db.execute("DELETE FROM updates WHERE id < (SELECT coalesce(max(id),0)-20000 FROM updates)")
            row=db.execute("SELECT value FROM settings WHERE key='offset'").fetchone();offset=int(row[0]) if row else 0
        updates=self.api.call('getUpdates',{'offset':offset,'timeout':10,'limit':10,'allowed_updates':['message','callback_query']})
        if not isinstance(updates,list):raise TelegramError('Invalid Telegram update list.')
        for update in updates:
            if self.stop.is_set():return
            self.handle(update)
            if self.stop.is_set():return
            if isinstance(update,dict) and type(update.get('update_id')) is int:
                offset=max(offset,update['update_id']+1)
                with self.db() as db:db.execute("INSERT OR REPLACE INTO settings VALUES ('offset',?)",(str(offset),))
        if self.stop.is_set():return
        if time.monotonic()>=self.next_alert:
            self.notifications();self.next_alert=time.monotonic()+30
        for _ in range(8):
            if self.stop.is_set():break
            self.flush()

    def start(self):
        def run():
            if self.stop.is_set():return
            try:
                me=self.api.call('getMe',{})
                if not isinstance(me,dict) or me.get('is_bot') is not True:raise TelegramError('Invalid bot configuration.')
                if self.config.bot_name and me.get('username','').casefold()!=self.config.bot_name.casefold():raise TelegramError('Bot username mismatch.')
                self.connection='connected'
            except TelegramError:self.connection='unavailable';return
            while not self.stop.is_set():
                try:self.poll_once()
                except Exception:
                    self.last_error='Telegram unavailable; polling will back off. Outgoing sends are not automatically retried.'
                    self.stop.wait(15)
                else:self.stop.wait(.5)
        self.thread=threading.Thread(target=run,name='fluxora-telegram',daemon=False);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(280)
