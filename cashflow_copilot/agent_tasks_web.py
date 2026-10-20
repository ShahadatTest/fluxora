"""Guided task review, zero-model reports and explicit model qualification."""
import json
import time
from pathlib import Path
from uuid import uuid4

from . import agent_tasks as tasks,agent_skills as skills
from .agent_disclosure import disclosure_revision
from .errors import CashflowError


def _fields(req,allowed):
    if set(req.form)-set(allowed)-{'csrf'} or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current guided task form without replacing its fields.')


def mount(web):
    from . import agent_web
    def live_adapter():return agent_web.live_adapter()
    from . import task_qualification as qualification
    from .agent_task_reports import report_html
    check_running=[False]

    def page(handler,req):
        from .ui import REQUEST
        adapter=live_adapter();qualified=qualification.status(qualification.report_path(web.database_path()),adapter)
        checks_token='' if getattr(REQUEST,'public_ai',False) else web._stage(dict(kind='guided_qualification',created=time.monotonic(),configuration_id=id(adapter)))
        def build(ledger):
            from .ui import icon
            buttons=''.join(f"<a class='report-link report-shortcut' href='/agent/tasks/report/{task}'><span class='report-glyph'>{icon('chart' if task in {'forecast','weekly_cashflow','account_funding'} else 'file')}</span><span>{web.esc(tasks.title(task))}</span>{icon('arrow')}</a>" for task in tasks.QUICK_TASKS)
            disabled=' disabled' if qualified!='qualified' else ''
            requested=req.query.get('skill',['auto'])[0]
            if requested!='auto':skills.load(requested)
            skill_options="<option value='auto'>Automatic focus</option>"+''.join(
                "<option value='"+s['id']+"'"+(' selected' if s['id']==requested else '')+">"+web.esc(s['title'])+"</option>" for s in skills.catalog())
            body="<div class='copilot-home'><header class='feature-header copilot-intro'><div class='copilot-symbol'>"+icon('spark')+"</div><div class='eyebrow'>YOUR CASH-FLOW WORKSPACE</div><h1>Ask Fluxora</h1><p>A clearer view of your cash.<br>And a practical next step.</p></header>"
            options_open=' open' if requested!='auto' else ''
            body+="<section class='card copilot-composer'><form method='post' action='/agent/tasks/start' data-direct-task>"+web.csrf_field()+f"<input type='hidden' name='request_id' value='{uuid4().hex}'><input type='hidden' name='business_id' value='{web.esc(ledger.business_id)}'><label for='task-question'>Message Fluxora</label><textarea id='task-question' name='question' rows='4' maxlength='2000' placeholder='Show my cash forecast, compare options, or prepare a draft…' required></textarea><p class='composer-disclosure'>Send shares your message and static task definitions with {web.esc(adapter.status().base_url or 'the configured provider')}. Provider billing and retention apply; use synthetic data only. Reports and DRAFT/pending preparation only. Final approval stays separate.</p><div class='composer-footer'><span class='composer-review-note'>{icon('check')} Reports run now. New actions stay DRAFT.</span><button type='submit' class='primary'{disabled}>Send {icon('arrow')}</button></div><details class='task-options'{options_open}><summary>Options</summary><p>The agent chooses a task focus automatically. You can narrow its scope here or inspect the full preview before sending. Ledger contents and attachments are excluded from model context. Inspect the selected task and exact terms in the result; interpretation can be wrong.</p><label for='task-skill'>Task skill</label><select id='task-skill' name='skill_id'>"+skill_options+f"</select><button type='submit' formaction='/agent/tasks/preview'{disabled}>Preview before sending</button></details></form>"
            body+=f"<div class='composer-status'><span>Model: <b>{web.esc(adapter.status().model or 'Not configured')}</b></span><span>Task check: <b>{qualified}</b></span><a href='/agent/skills'>Browse 6 task skills</a><a href='/agent'>Connection settings</a></div></section>"
            if qualified!='qualified':body+="<div class='copilot-availability'>"+icon('file')+"<div><b>Start with a local report</b><p>Natural-language tasks need a qualified model configuration. The reports below work now, without a model.</p></div><a href='#local-reports'>View reports ↓</a></div>"
            body+="<div class='copilot-suggestions' aria-label='Example questions'><button type='button' data-task-example='Show the baseline cash forecast for every currency. Read only.'>"+icon('chart')+"<span><b>Understand my cash</b><small>See the baseline forecast</small></span>"+icon('arrow')+"</button><button type='button' data-task-example='Show the next thirteen weeks and the lowest balance in each week. Read only.'>"+icon('calendar')+"<span><b>Look further ahead</b><small>Explore the next thirteen weeks</small></span>"+icon('arrow')+"</button><button type='button' data-task-example='Compare the allowed plans. Read only; do not apply any plan.'>"+icon('layers')+"<span><b>Compare my options</b><small>Review allowed responses</small></span>"+icon('arrow')+"</button></div>"
            body+="<section class='copilot-reports' id='local-reports'><div class='section-head'><div><div class='eyebrow'>NO MODEL NEEDED</div><h2>Start with the numbers.</h2></div><span class='snapshot-tag'>Snapshot "+web.esc(str(ledger.as_of))+"</span></div><p>Local calculations from the recorded ledger, with evidence you can inspect.</p><div class='report-links'>"+buttons+'</div></section>'
            body+="<div class='copilot-bottom'><section class='card snapshot-card'><span class='panel-icon'>"+icon('save')+"</span><h2>Save a forecast for review</h2><p>Prepare a pending snapshot workflow. The owner approves saving it separately.</p>"
            body+=f"<form method='post' action='/agent/tasks/snapshot'>{web.csrf_field()}<input type='hidden' name='request_id' value='{uuid4().hex}'><input type='hidden' name='business_id' value='{web.esc(ledger.business_id)}'><label for='snapshot-label'>Snapshot label</label><input id='snapshot-label' name='label' maxlength='120' placeholder='For example: Monday cash review' required><button>Prepare snapshot review</button></form></section><section class='card review-card'><span class='panel-icon'>"+icon('check')+"</span><h2>Pick up where you left off.</h2><p>Inspect pending decisions and the work already recorded.</p><div class='review-destinations'><a href='/agent/workflows'>Owner approval queue "+icon('arrow')+"</a><a href='/actions'>Action history "+icon('arrow')+"</a><a href='/agent'>Advanced agent & request history "+icon('arrow')+"</a></div></section></div>"
            body+="<aside class='copilot-boundary'>"+icon('check')+"<div><b>Prepared by the agent. Decided by you.</b><p>The model reads supported reports and prepares DRAFT or pending work. It cannot approve, move money, send real messages, or invent consent or receipts. Advanced-agent prose remains unverified.</p><a href='/docs/guided-agent'>How guided tasks work →</a></div></aside>"
            from .ui import REQUEST
            if getattr(REQUEST,'public_ai',False):
                body+="<p class='public-ai-notice'>LIVE AI TASK TRANSLATION · synthetic data only. Only your question and static task definitions reach the provider when you choose Send or confirm a preview. Financial results come from local tools. Shared daily allowance: up to 1,000 attempts. No self-approval or real payments. Model checks are managed by the operator.</p>"
            else:
                body+=f"<details class='qualification-panel'><summary>Model qualification <span>{qualified}</span></summary><div><h2>Check a model for guided tasks</h2><p>{len(qualification.CASES)} synthetic cases check task selection, exact parameters, clarification and refusal. Explicit unsafe/missing-fact cases may be handled locally. No business ledger is sent. This may incur provider charges. Passing does not certify arbitrary questions, source truth or future availability. A check expires after seven days and is invalidated by relevant configuration/code changes.</p>"
                body+=f"<form method='post' action='/agent/tasks/qualify'>{web.csrf_field()}<input type='hidden' name='token' value='{checks_token}'><label><input type='checkbox' name='confirmed' value='yes' required> I understand this runs up to {len(qualification.CASES)} potentially billed synthetic model checks.</label><button{' disabled' if adapter.status().state=='disconnected' else ''}>Run synthetic model check</button></form></div></details>"
            body+="<p class='copilot-source-note'>Recorded inputs may be wrong or incomplete. Projections are conditional. Owner approval, counterparty acceptance, observed payment and reconciliation remain separate.</p></div>"
            return web.page('Cash-flow tasks',body,active='/agent/tasks')
        return handler._send_html(200,web.read_state(build))

    def quick(handler,req,id):
        if id not in tasks.QUICK_TASKS:raise CashflowError('Choose a listed direct report.')
        return local_report(handler,id)

    def local_report(handler,id,skill=None):
        from .agent import execute_tool
        from .authz import RequestContext
        def build(ledger):
            name,args=tasks.tool_for(dict(task=id,parameters={}))
            reply=execute_tool(RequestContext('direct-report','agent',ledger.business_id),ledger,name,args,provider_label='local deterministic report; no model')
            calls=[dict(name=name,arguments=args,ok=True,result=reply['result'])]
            body=f"<h1>{web.esc(tasks.title(id))}</h1><p>Local deterministic report · no model call. Computed from the current recorded snapshot on {web.esc(str(ledger.as_of))}.</p>"
            if skill:body+=f"<p>Skill: {web.esc(skill['title'])} · v{web.esc(skill['version'])}. <a href='/agent/skills'>All skills</a></p>"
            body+=report_html(web,ledger,calls)+raw_calls(calls)+"<p><a href='/agent/tasks'>Choose another task</a></p>"
            return web.page(tasks.title(id),body,active='/agent')
        return handler._send_html(200,web.read_state(build))

    def skill_directory(handler,req):
        cards=[]
        for s in skills.catalog():
            skills.load(s['id'])
            cards.append(f"<article class='feature-tile'><span class='tile-family'>BUNDLED SKILL · v{web.esc(s['version'])}</span><h2>{web.esc(s['title'])}</h2><p>{web.esc(s['description'])}</p><p><a href='/agent/skills/{s['id']}/report'>Open local report</a> · <a href='/agent/tasks?skill={s['id']}'>Choose for a question</a></p><a href='/agent/skills/{s['id']}'>Instructions &amp; scope →</a></article>")
        body="<h1>Fluxora task skills</h1><p>Six bundled workflows reuse the existing deterministic tools. Local reports work without a model. Natural-language selection needs a configured, qualified model.</p><p>Explicit focus narrows the allowed tasks. Automatic suggestions may be wrong: review the interpretation. Skills never authorize approval, invent evidence or move money.</p><div class='directory-grid'>"+''.join(cards)+"</div><p><a href='/agent/tasks'>Back to Agent</a></p>"
        return handler._send_html(200,web.page('Task skills',body,active='/agent/tasks'))

    def skill_detail(handler,req,id):
        item=skills.load(id);scope=next(s for s in skills.catalog() if s['id']==id)
        body=f"<h1>{web.esc(item['title'])}</h1><p>Bundled guidance · v{web.esc(item['version'])} · synthetic sandbox. Evidence uploads cannot install or modify skills.</p><p><a href='/agent/skills/{id}/report'>Open local report</a> · <a href='/agent/tasks?skill={id}'>Choose this skill</a></p>"
        body+="<h2>Inputs and workflow</h2><pre class='dump'>"+web.esc(item['instructions'])+"</pre><h2>Allowed scope</h2><pre class='dump'>"+web.esc(json.dumps({k:scope[k] for k in ('reports','actions','operations')},indent=2))+"</pre><p>Actions remain DRAFT; operations remain pending separate approval. An empty list permits none.</p><details><summary>Version identity</summary><code>"+web.esc(item['sha256'])+"</code></details><p><a href='/agent/skills'>All skills</a></p>"
        return handler._send_html(200,web.page(item['title'],body,active='/agent/tasks'))

    def skill_report(handler,req,id):
        item=skills.load(id);scope=next(s for s in skills.catalog() if s['id']==id)
        return local_report(handler,scope['report'],item)

    def raw_calls(calls):
        return "<details><summary>Full deterministic tool result</summary><pre class='dump'>"+web.esc(json.dumps(calls,indent=2))+"</pre></details>"

    def prepare(req):
        _fields(req,{'question','business_id','request_id','skill_id'})
        from .agent_requests import _validate
        question=req.field('question').strip();ident=req.field('request_id');_validate(ident,question)
        skill_id=req.field('skill_id') or 'auto';skills.select(question,skill_id)
        adapter=live_adapter()
        def current(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('The active business changed. Reload the task form.')
            return web._owner_ctx(ledger)
        ctx=web.read_state(current)
        from .agent_requests import has_guided_request
        if has_guided_request(web.database_path(),ident,question,ctx=ctx,lock=web.LOCK):return None,None
        if qualification.status(qualification.report_path(web.database_path()),adapter)!='qualified':raise CashflowError('This configuration has not passed the current guided-task check. Direct reports remain available.')
        def build(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('The active business changed. Reload the task form.')
            with web.LOCK:
                for token,stage in web.staged().items():
                    if stage.get('kind')=='guided_task' and stage.get('request_id')==ident and stage.get('database')==str(Path(web.database_path()).resolve()):
                        if stage['question']!=question or stage.get('skill_id')!=skill_id or stage['business_id']!=ledger.business_id:
                            raise CashflowError('This request identity belongs to another task. Reload the task form.')
                        check_stage(stage,'guided_task')
                        return token,stage
            token=web._stage(dict(kind='guided_task',created=time.monotonic(),configuration_id=id(adapter),
                database=str(Path(web.database_path()).resolve()),
                request_id=ident,business_id=ledger.business_id,revision=disclosure_revision(ledger),question=question,translation_provider=adapter.provider_label,
                skill_id=skill_id,skills_fingerprint=skills.fingerprint()))
            return token,web.staged()[token]
        return web.read_state(build)

    def start(handler,req):
        deadline=time.monotonic()+30
        token,stage=prepare(req)
        if stage is None:return handler._redirect('/agent/requests/'+req.field('request_id'))
        return interpret(handler,stage,token,direct=True,deadline=deadline)

    def preview(handler,req):
        token,stage=prepare(req)
        if stage is None:return handler._redirect('/agent/requests/'+req.field('request_id'))
        adapter=live_adapter();question=stage['question'];skill_id=stage['skill_id'];selected=skills.select(question,skill_id)
        def build(ledger):
            body=f"<h1>Review task translation data</h1><p>Destination: {web.esc(adapter.status().base_url)} · model: {web.esc(adapter.status().model)}.</p><p>Only the question below and the server's static task/operation definitions are sent. No ledger, tool results, original attachments or credentials are included in model context. Your question itself may contain identifying or financial information. Provider billing and retention still apply.</p><blockquote>{web.esc(question)}</blockquote>"
            if selected:body+=f"<p>Skill focus: {web.esc(selected['title'])} · v{web.esc(selected['version'])} · {web.esc(selected['selection'])}. A suggested focus is not proof of your intent.</p>"
            body+="<details><summary>Exact outbound messages and task schema</summary><pre class='dump'>"+web.esc(json.dumps(dict(messages=tasks.messages(question,skill_id),tools=[tasks.schema(skill_id)]),indent=2))+"</pre></details>"
            body+=f"<form method='post' action='/agent/tasks/translate'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'><label><input type='checkbox' name='confirmed' value='yes' required> I reviewed the question and provider destination.</label><button>Translate task</button></form><p><a href='/agent/tasks'>Change question</a></p>"
            return web.page('Task data preview',body,active='/agent')
        return handler._send_html(200,web.read_state(build))

    def get_stage(req,kind):
        _fields(req,{'token','confirmed'})
        if req.field('confirmed')!='yes':raise CashflowError('Confirm the current reviewed form.')
        stage=web.staged().get(req.field('token'))
        check_stage(stage,kind)
        return stage

    def check_stage(stage,kind):
        if not stage or stage.get('kind')!=kind or time.monotonic()-stage['created']>900:raise CashflowError('Task review expired. Start a fresh task; saved results remain in history.')
        if stage.get('database') and stage['database']!=str(Path(web.database_path()).resolve()):raise CashflowError('The active workspace changed. Start a fresh task.')
        if stage['configuration_id']!=id(live_adapter()):raise CashflowError('Model configuration changed. Review a fresh task.')
        if stage.get('skills_fingerprint') and stage['skills_fingerprint']!=skills.fingerprint():raise CashflowError('Bundled skills changed. Review a fresh task.')

    def translate(handler,req):
        stage=get_stage(req,'guided_task')
        return interpret(handler,stage,req.field('token'))

    def interpret(handler,stage,token,*,direct=False,deadline=None):
        adapter=live_adapter()
        if qualification.status(qualification.report_path(web.database_path()),adapter)!='qualified':raise CashflowError('Run the current guided-task check or use a direct report.')
        with web.LOCK:
            if stage.get('parsing'):raise CashflowError('This translation is already running. Do not submit it again.')
            result=stage.get('translation')
            if result is None:stage['parsing']=True
        if result is None:
            try:
                def snapshot(ledger):
                    if ledger.business_id!=stage['business_id'] or disclosure_revision(ledger)!=stage['revision']:raise CashflowError('Recorded data changed. Review a fresh task.')
                    return ledger,web._owner_ctx(ledger)
                ledger,ctx=web.read_state(snapshot)
                result=tasks.translate(ledger,stage['question'],adapter,ctx=ctx,skill_id=stage.get('skill_id','auto'),deadline=deadline)
                with web.LOCK:stage['translation']=result
            finally:
                with web.LOCK:stage['parsing']=False
        if result['status']!='review':
            heading={'declined':'Task declined','unavailable':'Task unavailable'}.get(result['status'],'Task needs clarification')
            body=f"<h1>{heading}</h1><p>{web.esc(tasks.REASONS[result['reason']])}</p><p>No proposal or financial change was applied.</p><p><a href='/agent/tasks'>Choose a supported task</a></p>"
            if result.get('selection')=='local_guard':body+='<p>Handled locally before contacting a model. This task made zero provider calls.</p>'
        else:
            plan=result['plan']
            if direct:return execute(handler,stage,plan,deadline=deadline)
            proposal=plan['task'] in {'proposal','action_proposal'}
            body=f"<h1>Review interpreted task</h1><blockquote>{web.esc(stage['question'])}</blockquote><h2>{web.esc(tasks.title(plan['task']))}</h2><p>Check the selected task and exact parameters below. A valid instruction can still misunderstand your question. Nothing has run against the saved ledger.</p>"
            if result.get('skill'):body+=f"<p>Bundled skill: {web.esc(result['skill']['title'])} · v{web.esc(result['skill']['version'])} · {web.esc(result['skill']['selection'])}. <a href='/agent/skills'>View scope</a></p>"
            from .ui import task_review
            body+=task_review(plan)
            body+=f"<details><summary>Exact structured parameters</summary><pre class='dump'>{web.esc(json.dumps(plan,indent=2))}</pre></details><p>{'This creates only a pending workflow or DRAFT action; execution requires separate owner approval and any required source-fact/counterparty confirmations.' if proposal else 'This is read-only; no record or proposal will be changed.'}</p>"
            body+=f"<form method='post' action='/agent/tasks/run'>{web.csrf_field()}<input type='hidden' name='token' value='{web.esc(token)}'><label><input type='checkbox' name='confirmed' value='yes' required> This is the task and exact parameters I intended.</label><button>{'Prepare draft for review' if proposal else 'Run reviewed report'}</button></form><p><a href='/agent/tasks'>Change task</a></p>"
        return handler._send_html(200,web.page('Task interpretation',body,active='/agent'))

    def run(handler,req):
        stage=get_stage(req,'guided_task');result=stage.get('translation',{})
        if result.get('status')!='review':raise CashflowError('Review a valid interpreted task first.')
        return execute(handler,stage,result['plan'])

    def execute(handler,stage,plan,*,deadline=None):
        from .agent_requests import run_request
        from .agent import TIME_BUDGET_S
        remaining=TIME_BUDGET_S if deadline is None else min(TIME_BUDGET_S,deadline-time.monotonic())
        if remaining<=0:raise CashflowError('Task deadline expired. No draft was created. Use a fresh task form.')
        ctx=web.read_state(web._owner_ctx)
        if ctx.business_id!=stage['business_id']:raise CashflowError('The active business changed. Review a fresh task.')
        translated_by=stage.get('translation_provider')
        adapter=tasks.TaskExecutionAdapter(plan,stage['request_id'],
            provider='local deterministic execution'+(' after live task translation' if translated_by else '; no model'),
            translation_provider=translated_by,translation_usage=stage.get('translation',{}).get('usage'),skill=stage.get('translation',{}).get('skill'))
        run_request(web.database_path(),stage['request_id'],stage['question'],'guided',adapter,ctx=ctx,lock=web.LOCK,
            data_mode='minimized',expected_revision=stage['revision'],time_budget_s=remaining)
        return handler._redirect('/agent/requests/'+stage['request_id'])

    def snapshot(handler,req):
        _fields(req,{'label','business_id','request_id'})
        from .agent_requests import _validate
        label=req.field('label').strip();ident=req.field('request_id')
        if not label or len(label)>120:raise CashflowError('Supply a snapshot label of 1–120 characters.')
        question='Prepare a forecast snapshot for owner review with label '+label;_validate(ident,question)
        def build(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('The active business changed. Reload the task form.')
            plan=dict(task='proposal',parameters={'steps':[{'operation':'save_forecast','arguments':{'label':label}}]})
            tasks.validate_plan(ledger,plan,question,ctx=web._owner_ctx(ledger))
            return dict(request_id=ident,business_id=ledger.business_id,question=question,revision=disclosure_revision(ledger)),plan
        stage,plan=web.read_state(build)
        return execute(handler,stage,plan)

    def qualify(handler,req):
        stage=get_stage(req,'guided_qualification')
        with web.LOCK:
            if stage.get('done'):return handler._redirect('/agent/tasks')
            if stage.get('running') or check_running[0]:raise CashflowError('A synthetic check is already running. Direct reports remain available.')
            stage['running']=True;check_running[0]=True
        try:qualification.qualify(live_adapter(),qualification.report_path(web.database_path()))
        finally:
            with web.LOCK:stage['running']=False;stage['done']=True;check_running[0]=False
        return handler._redirect('/agent/tasks')

    web.route('GET','/agent/tasks')(page)
    web.route('GET','/agent/skills')(skill_directory)
    web.route('GET','/agent/skills/<id>/report')(skill_report)
    web.route('GET','/agent/skills/<id>')(skill_detail)
    web.route('GET','/agent/tasks/report/<id>')(quick)
    web.route('POST','/agent/tasks/snapshot')(snapshot)
    web.route('POST','/agent/tasks/preview')(preview)
    web.route('POST','/agent/tasks/start')(start)
    web.route('POST','/agent/tasks/translate')(translate)
    web.route('POST','/agent/tasks/run')(run)
    web.route('POST','/agent/tasks/qualify')(qualify)
