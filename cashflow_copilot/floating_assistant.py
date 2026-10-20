"""Model-free contextual UI and explicit handoff to existing guided review.

Rendering and local guidance never inspect ledger/configuration or call a model.
Only an explicit private POST builds a task form; original gates still apply.
"""
from html import escape
import json
from uuid import uuid4

from .errors import CashflowError

INTENTS={
    'page':dict(title='Explain this page',text='This page helps you: {purpose} Read its guide for the inputs, assumptions and review steps.',links=[]),
    'forecast':dict(title='Understand my cash',text='Open a deterministic report computed from the recorded snapshot. Expected collections are conditional; inspect currencies, dates, assumptions and evidence.',links=[('/agent/tasks/report/forecast','Open cash forecast report'),('/forecast','Forecast workspace')]),
    'next':dict(title='Find my next step',text='Start with Daily review: inspect source gaps and flagged items, then compare allowed plans. This navigation suggestion is not a financial recommendation computed from your records.',links=[('/review','Open Daily review'),('/plans','Compare allowed plans'),('/facts','Resolve missing facts')]),
    'draft':dict(title='Prepare a DRAFT',text='Use Agent with the exact record ID, currency, amount and ISO date. Review the interpretation before creating a DRAFT. It cannot approve itself, prove consent/payment or move money.',links=[('/agent/tasks','Open Agent for a DRAFT'),('/actions','Review Action history')]),
    'approval':dict(title='Review owner decisions',text='This panel cannot approve, execute, send, record consent or create receipts. Review exact pending terms and source facts in the existing owner workflow.',links=[('/agent/workflows','Open Approval queue'),('/docs/approvals','Read approval boundaries')]),
    'telegram':dict(title='Try the Telegram workflow',text='The interactive browser practice needs no Telegram account. Its separate synthetic ledger uses real deterministic tools; delivery is simulated and no live AI is connected.',links=[('/telegram/demo','Open Telegram practice'),('/docs/telegram','Read Telegram guide')]),
    'general':dict(title='Choose a supported workspace',text='I provide local guidance and page links here. Suggested destinations may misunderstand your question. Financial answers come from deterministic reports; model interpretation uses the separate reviewed Agent workflow.',links=[('/agent/tasks','Open full Agent'),('/workspace/features','Browse all features'),('/docs','Read documentation')]),
}


def context(path):
    from .workspace_ui import profile
    public={'/welcome':('Product home','explore the synthetic product and enter a workspace','getting-started'),
        '/product':('Product overview','understand the existing product capabilities','getting-started'),
        '/how-it-works':('How it works','follow evidence, deterministic tools, proposals and owner review','guided-agent'),
        '/trust':('Trust & limitations','inspect the project scope and limitations','security'),
        '/about':('About Fluxora','understand this synthetic project','getting-started')}
    if path in public:return public[path]
    if path=='/docs' or path.startswith('/docs/'):return ('Documentation','find setup instructions, workflow guides and known limitations','getting-started')
    purpose,_,doc=profile(path)
    return ('Current workspace',purpose,doc)


def render(path,public_demo=False):
    # Leave locked previews and focused practice completely self-contained.
    if path.startswith(('/settings/connections','/telegram/demo')):return ''
    title,purpose,doc=context(path)
    payload=json.loads(json.dumps(INTENTS))
    payload['page']['text']=payload['page']['text'].replace('{purpose}',purpose)
    payload['page']['links']=[('/docs/'+doc,'Read this page guide')]
    data=json.dumps(dict(context=title,intents=payload,public=public_demo)).replace('<',chr(92)+'u003c')
    composer='<label for="fluxora-assistant-question">What do you need help with?</label><textarea id="fluxora-assistant-question" name="question" rows="2" maxlength="2000" placeholder="Try: where can I review a draft?"></textarea><button type="button" data-assistant-send>Get local guidance</button>'
    if public_demo:composer='<div class="assistant-composer">'+composer+'<p>This public demo is model-free. Open full Agent for deterministic reports.</p></div>'
    else:
        from .web import csrf_field
        composer='<form class="assistant-composer" method="post" action="/assistant/continue">'+csrf_field()+composer+'<button type="submit" class="assistant-continue">Continue in Agent</button><p>Continue sends only your typed question to this local/server app. Provider disclosure and confirmation follow in Agent; no model call occurs here.</p></form>'
    return '''<div class="floating-assistant" data-floating-assistant>
<button type="button" class="assistant-launcher" data-assistant-open aria-haspopup="dialog" aria-controls="fluxora-assistant-dialog" aria-expanded="false" hidden><span aria-hidden="true">✦</span> Ask Fluxora</button>
<dialog id="fluxora-assistant-dialog" class="assistant-dialog" aria-labelledby="fluxora-assistant-title">
<header class="assistant-header"><div><span class="assistant-eyebrow">YOUR WORKSPACE GUIDE</span><h2 id="fluxora-assistant-title">Ask Fluxora</h2></div><button type="button" class="assistant-close" data-assistant-close aria-label="Close Ask Fluxora">×</button></header>
<p class="assistant-mode">LOCAL GUIDANCE · NO MODEL CALL</p>
<div class="assistant-scroll"><div class="assistant-context">'''+escape(title)+'''</div><div class="assistant-messages" data-assistant-messages role="log" aria-live="polite" aria-relevant="additions"><article class="assistant-reply"><b>How can I help?</b><p>Find a report, understand this page, or prepare a task for review. This panel has not read your financial records. Do not enter keys or tokens.</p></article></div>
<div class="assistant-shortcuts" aria-label="Suggested help">'''+''.join('<button type="button" data-assistant-intent="'+key+'">'+escape(payload[key]['title'])+'</button>' for key in ['page','forecast','next','draft','approval','telegram'])+'</div></div>'+composer+'''<footer class="assistant-footer"><a href="/agent/tasks">Open full Agent →</a><span>Exact review before changes</span></footer>
</dialog><script type="application/json" id="fluxora-assistant-data">'''+data+'''</script><noscript><a class="assistant-fallback" href="/agent/tasks">Ask Fluxora — open Agent</a></noscript></div>'''


def mount(web):
    @web.route('POST','/assistant/continue')
    def handoff(handler,req):
        if req.query or req.files or len(req.body)>10000 or set(req.form)-{'csrf','question'} or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current assistant question form.')
        question=req.field('question').strip()
        if not 1<=len(question)<=2000:raise CashflowError('Write a question of 1–2,000 characters. Do not enter credentials.')
        from . import ui
        if bool(getattr(getattr(handler.server,'config',None),'public_demo',False)):
            return handler._send_html(200,ui.app_page('Guided demo help','<h1>This hosted demo is model-free</h1><p>No question was sent to a provider or executed. Use <a href="/agent/tasks">deterministic Agent reports</a>, <a href="/telegram/demo">Telegram practice</a> or <a href="/docs">documentation</a>.</p>','/assistant/continue','Public guided demo — no model call.'))
        from . import agent_web,task_qualification
        adapter=agent_web.live_adapter()
        qualified=task_qualification.status(task_qualification.report_path(web.database_path()),adapter)=='qualified'
        def build(ledger):
            body='<h1>Continue your question in Agent</h1><p>No model call or financial operation has run. Review/edit your question, then inspect the existing outbound-data preview before confirming any provider call. A supported model instruction can still misunderstand your intent.</p>'
            if not qualified:body+='<p>This configuration needs the current model check. <a href="/agent/tasks">Open Agent qualification or deterministic reports</a>.</p>'
            body+="<form method='post' action='/agent/tasks/preview'>"+web.csrf_field()+"<input type='hidden' name='request_id' value='"+uuid4().hex+"'><input type='hidden' name='business_id' value='"+web.esc(ledger.business_id)+"'><input type='hidden' name='skill_id' value='auto'><label for='assistant-agent-question'>Your exact question</label><textarea id='assistant-agent-question' name='question' rows='5' maxlength='2000' required>"+web.esc(question)+"</textarea><button"+('' if qualified else ' disabled')+">Review provider data preview</button></form><p>DRAFT preparation, owner approval, counterparty acceptance, observed payment and reconciliation remain separate.</p>"
            return ui.app_page('Continue in Agent',body,'/agent/tasks','Agent handoff — no model call yet; separate qualification and disclosure apply.')
        return handler._send_html(200,web.read_state(build))
