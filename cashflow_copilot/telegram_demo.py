"""Disposable browser practice: real deterministic tools, simulated transport.

Never accesses the live bridge/configuration, network, database or model. The
practice ledger is a fresh fixture, held only on the current workspace object.
"""
import copy
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import re
import time
from uuid import uuid4

from .authz import RequestContext
from .demo_examples import seed
from .errors import CashflowError
from . import agent_tasks as tasks,agent_workflows as workflows,telegram_copy as words,ui

LABEL='browser Telegram simulation; no live model or delivery'


class CardHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'b','code'}:self.parts.append('<'+tag+'>')
    def handle_endtag(self,tag):
        if tag in {'b','code'}:self.parts.append('</'+tag+'>')
    def handle_data(self,data):self.parts.append(escape(data))


def card_html(value):
    parser=CardHTML();parser.feed(str(value));return ''.join(parser.parts)


class Practice:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.created=clock();self.id=uuid4().hex;self.ledger=seed('atlas')
        self.messages=[];self.reviews={};self.seen={};self.alerts=False;self.alert_hash=''
        self.add('bot',words.card('Browser practice ready','SIMULATION · fresh Atlas practice data. No Telegram account, bot token or model is connected.',
            'Try /forecast, then prepare a DRAFT and inspect the exact review. Saved practice results appear beside this conversation. Your main workspace is unchanged.'))
    def add(self,role,text,review=''):
        self.messages.append(dict(role=role,text=text,review=review));self.messages=self.messages[-40:]
    def ctx(self,role='agent'):
        return RequestContext('telegram-demo-'+role,role,self.ledger.business_id)
    def tool(self,plan,request_id):
        from .agent import execute_tool
        name,args=tasks.tool_for(plan,'demo-'+request_id)
        candidate=copy.deepcopy(self.ledger)
        result=execute_tool(self.ctx(),candidate,name,args,provider_label=LABEL)['result']
        self.ledger=candidate;return result
    def review(self,kind,payload,text,button):
        if sum(r['status']=='pending' for r in self.reviews.values())>=20:raise CashflowError('Review or reject existing practice cards first.')
        token=uuid4().hex
        self.reviews[token]=dict(kind=kind,payload=payload,basis=workflows.revision(self.ledger),expires=self.clock()+900,status='pending',button=button)
        self.add('bot',text,token)
    def propose(self,plan,question):
        plan=tasks.validate_plan(self.ledger,plan,question,ctx=self.ctx())
        self.review('plan',plan,words.review(plan,tasks.title(plan['task']),self.ledger.as_of),
            'Create practice DRAFT' if plan['task']=='action_proposal' else 'Prepare pending snapshot')
    def command(self,text):
        if not isinstance(text,str) or not 1<=len(text)<=220:raise CashflowError('Use a supported practice command of at most 220 characters.')
        self.add('user',words.plain(text))
        if text in {'/forecast','/plans'}:
            task=text[1:];result=self.tool(dict(task=task,parameters={}),uuid4().hex)
            self.add('bot',words.forecast(result) if task=='forecast' else words.plans(result))
        elif text.startswith('/draft '):
            parts=text.split()
            if len(parts)!=5:raise CashflowError('Use /draft INVOICE_ID USD 100 YYYY-MM-DD.')
            _,target,currency,amount,day=parts
            self.propose(dict(task='action_proposal',parameters=dict(kind='request_early_payment',target_id=target,currency=currency,amount=amount,date=day)),text)
        elif text.startswith('/snapshot '):
            label=text[len('/snapshot '):].strip()
            if not 1<=len(label)<=64:raise CashflowError('Use a snapshot label of 1–64 characters.')
            self.propose(dict(task='proposal',parameters=dict(steps=[dict(operation='save_forecast',arguments=dict(label=label))])),text)
        elif text in {'/alerts on','/alerts off'}:
            self.alerts=text.endswith(' on')
            self.add('bot',words.card('Practice alerts '+('ON' if self.alerts else 'OFF'),'Alerts are generated only when you press Check practice alerts. No background schedule or Telegram send.'))
        elif text=='/status':
            self.add('bot',words.card('Practice status','Transport: simulated in this browser. Model: none. Data: disposable Atlas fixture.',
                'Practice DRAFTs: '+str(len(self.ledger.actions))+' · snapshots: '+str(len(self.ledger.forecast_snapshots))+'. No real payment, receipt or counterparty acceptance.'))
        else:self.add('bot',words.card('Supported-command demo','Use /forecast, /plans, /draft, /snapshot, /status or /alerts on|off.',
            'This simulator does not translate free text with AI. Approval/payment/consent commands cannot bypass the separate exact owner review. Real private model translation is a separate configured feature.'))
    def decide(self,token,decision,request_id):
        row=self.reviews.get(token)
        if row is None or decision not in {'yes','no'}:raise CashflowError('Practice review is unavailable.')
        if row['status']!='pending':
            self.add('bot',words.card('Already reviewed','No duplicate operation was run.'));return
        if row['expires']<=self.clock():row['status']='expired';raise CashflowError('This practice review expired. Prepare fresh terms.')
        if row['basis']!=workflows.revision(self.ledger):row['status']='stale';raise CashflowError('Practice data changed. This old button applied nothing; prepare a fresh request.')
        if decision=='no':
            if row['kind']=='snapshot':
                candidate=copy.deepcopy(self.ledger);payload=row['payload']
                workflows.decide(candidate,payload['id'],payload['review_token'],'reject',ctx=self.ctx('owner'));self.ledger=candidate
            row['status']='rejected';self.add('bot',words.card('Practice request rejected','No proposed financial operation was applied.'));return
        if row['kind']=='plan':
            plan=row['payload'];result=self.tool(plan,request_id);row['status']='completed'
            if plan['task']=='action_proposal':
                self.add('bot',words.card('Practice DRAFT ready','Action: '+words.code(result['action_id'])+'\nState: DRAFT only.',
                    'Inspect Practice results on this page. No owner approval, consent, receipt, message or payment was created.'))
            else:
                job=self.ledger.agent_workflows[result['id']]
                self.add('bot',words.card('Practice workflow prepared','Status: pending owner approval. No snapshot has been saved yet.'))
                self.review('snapshot',dict(id=job['id'],review_token=job['review_token']),words.snapshot_review(self.ledger,job),'Approve & save practice snapshot')
        else:
            payload=row['payload'];candidate=copy.deepcopy(self.ledger)
            job=candidate.agent_workflows.get(payload['id'])
            if not job or any(s['operation']!='save_forecast' for s in job['steps']):raise CashflowError('Only exact forecast snapshots can be approved here.')
            result=workflows.decide(candidate,payload['id'],payload['review_token'],'approve',ctx=self.ctx('owner'))
            self.ledger=candidate;row['status']='completed'
            self.add('bot',words.card('Practice snapshot '+result['status'],'A separate human button approved this exact synthetic snapshot.',
                'Current cash is unchanged. No payment, receipt or counterparty consent was recorded. This is not a Telegram delivery receipt.'))
    def notify(self):
        if not self.alerts:
            self.add('bot',words.card('Practice alerts are OFF','Use /alerts on before checking. Nothing was sent.'));return
        result=self.tool(dict(task='forecast',parameters={}),uuid4().hex)
        digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
        if digest==self.alert_hash:
            self.add('bot',words.card('No new practice alert','The computed forecast is unchanged. No duplicate forecast notification was generated.'));return
        self.alert_hash=digest
        self.add('bot',words.card('Simulated forecast notification','Generated from the practice ledger. Delivery is simulated; no Telegram message was sent.'))
        self.add('bot',words.forecast(result))
    def perform(self,request_id,action,command='',token='',decision=''):
        if not re.fullmatch('[a-f0-9]{32}',request_id):raise CashflowError('Reload the current practice form.')
        identity=(action,command,token,decision)
        if request_id in self.seen:
            if self.seen[request_id]!=identity:raise CashflowError('Practice request identity was reused with different terms.')
            return
        if len(self.seen)>=100:raise CashflowError('Practice limit reached. Restart this disposable lesson to continue.')
        self.seen[request_id]=identity
        try:
            if action=='send':self.command(command.strip())
            elif action=='review':self.decide(token,decision,request_id)
            elif action=='alerts':self.notify()
            elif action=='delay':
                self.ledger.apply_event('evt-delay-001',ctx=self.ctx('owner'))
                self.add('bot',words.card('Synthetic delay event applied','Only the practice invoice timing changed. Old reviews may now be stale. Press Check practice alerts to recalculate; main workspace data is unchanged.'))
            else:raise CashflowError('Use a displayed practice control.')
        except CashflowError as error:self.add('bot',words.card('Practice request needs review',words.text(str(error)),'No successful operation is implied. Inspect Practice results.'))


def mount(web):
    def safe(fn):
        def guarded(handler,req):
            try:return fn(handler,req)
            except CashflowError as error:
                return handler._send_html(400,ui.app_page('Practice request refused','<h1>Practice request refused</h1><p>'+web.esc(str(error))+'</p><p><a href="/telegram/demo">Open current practice lesson</a></p>','/telegram/demo','Browser practice simulation — no live model or Telegram delivery.'))
        return guarded
    def lesson(handler):
        scope=getattr(web._WORKSPACE,'state',None) or handler.server
        current=getattr(scope,'telegram_demo',None)
        if current is None or time.monotonic()-current.created>=1800:
            current=Practice();scope.telegram_demo=current
        return current,scope
    def controls(state,action,extra,button):
        hidden=dict(lesson=state.id,request_id=uuid4().hex,action=action,**extra)
        return "<form method='post' action='/telegram/demo/action'>"+web.csrf_field()+''.join("<input type='hidden' name='"+web.esc(k)+"' value='"+web.esc(v)+"'>" for k,v in hidden.items())+button+'</form>'
    @web.route('GET','/telegram/demo')
    @safe
    def page(handler,req):
        if req.query:raise CashflowError('Open the practice page without query parameters.')
        with web.LOCK:
            state,_=lesson(handler)
            body='<h1>Telegram interactive demo</h1><p><b>SIMULATED TRANSPORT · NO LIVE AI</b> · No Telegram account or token required.</p><section class="card"><h2>Try the private-chat workflow</h2><p>This disposable Atlas practice ledger is separate from your main workspace. Financial replies and changes use the existing deterministic tools. Chat delivery and user roles are simulated; your review button is an explicit human demo decision.</p><p>Try: forecast → DRAFT request → exact review → snapshot request → separate owner approval. For alerts: turn alerts on → check → apply the synthetic delay → check again.</p><p>Memory only: the lesson expires after 30 minutes, server restart or loss of the visitor session. Practice results are not saved into the main ledger. <a href="/telegram">Real private bot setup (operator access required)</a></p></section>'
            body+='<div class="tg-demo-grid" id="practice-chat"><section class="card"><div class="eyebrow">BROWSER SIMULATION</div><h2>Fluxora practice chat</h2><div class="tg-demo-chat" data-telegram-demo-log role="log" aria-label="Simulated Telegram conversation">'
            for message in state.messages:
                body+='<article class="tg-demo-message '+message['role']+'"><small>'+('Demo user' if message['role']=='user' else 'Simulated Fluxora reply')+'</small><div>'+card_html(message['text'])+'</div>'
                row=state.reviews.get(message['review'])
                if row:
                    body+='<p>Review: '+web.esc(row['status'])+' · expires after 15 minutes; changed practice data invalidates it.</p>'
                    if row['status']=='pending':
                        for decision,label in [('yes',row['button']),('no','Reject practice request')]:
                            body+=controls(state,'review',dict(token=message['review'],decision=decision),'<button>'+web.esc(label)+'</button>')
                body+='</article>'
            body+='</div><h3>Supported demo command</h3>'
            body+=controls(state,'send',{},'<label>Command<input name="command" maxlength="220" required placeholder="/forecast"></label><button>Send in simulation</button>')
            body+='<div class="tg-demo-shortcuts">'
            for command,label in [('/forecast','View forecast'),('/plans','Compare plans'),('/draft inv-client-001 USD 100 2026-10-22','Prepare sample DRAFT'),('/snapshot Practice review','Request snapshot'),('/alerts on','Enable practice alerts'),('/status','Practice status')]:
                body+=controls(state,'send',dict(command=command),'<button>'+label+'</button>')
            body+='</div></section><aside><section class="card"><h2>Practice results</h2><p>As of '+str(state.ledger.as_of)+' · Atlas fixture · integer minor units · currencies separate.</p><p>DRAFT actions: <b>'+str(len(state.ledger.actions))+'</b> · saved snapshots: <b>'+str(len(state.ledger.forecast_snapshots))+'</b></p>'
            body+='<ul>'+''.join('<li>'+web.esc(a.id)+' · '+web.esc(a.state)+'</li>' for a in state.ledger.actions.values())+''.join('<li>Workflow '+web.esc(j['id'])+' · '+web.esc(j['status'])+'</li>' for j in state.ledger.agent_workflows.values())+'</ul><p>No observed receipts, counterparty acceptance or real money movement is created by this lesson.</p></section>'
            body+='<section class="card"><h2>Try changed-data alerts</h2>'
            body+=controls(state,'alerts',{},'<button>Check practice alerts</button>')+controls(state,'delay',{},'<button>Apply synthetic customer delay</button>')
            body+='<p>These controls affect only this lesson. Notifications run on demand; no schedule or Telegram delivery is simulated as a real send.</p></section>'
            body+='<details class="card"><summary>Practice evidence</summary><ul>'+''.join('<li><code>'+web.esc(e.id)+'</code>: '+web.esc(e.summary)+'</li>' for e in state.ledger.evidence.values())+'</ul></details>'
            body+='<section class="card"><h2>Restart practice</h2>'+controls(state,'restart',{},'<label><input type="checkbox" name="confirmed" value="yes" required> Discard only this lesson and its review buttons; main workspace and real bot stay unchanged.</label><button>Restart disposable lesson</button>')+'</section></aside></div>'
            return handler._send_html(200,ui.app_page('Telegram interactive demo',body,'/telegram/demo','Browser practice simulation — no live model or Telegram delivery.'))
    @web.route('POST','/telegram/demo/action')
    @safe
    def action(handler,req):
        allowed={'csrf','lesson','request_id','action','command','token','decision','confirmed'}
        if req.query or req.files or set(req.form)-allowed or any(len(v)!=1 for v in req.form.values()) or len(req.body)>4096:raise CashflowError('Use the current practice form.')
        with web.LOCK:
            state,scope=lesson(handler)
            if req.field('lesson')!=state.id:raise CashflowError('This practice lesson expired or restarted. Open its current page.')
            if req.field('action')=='restart':
                if req.field('confirmed')!='yes':raise CashflowError('Confirm discarding only the disposable lesson.')
                scope.telegram_demo=Practice()
            else:state.perform(req.field('request_id'),req.field('action'),req.field('command'),req.field('token'),req.field('decision'))
        return handler._redirect('/telegram/demo#practice-chat')
