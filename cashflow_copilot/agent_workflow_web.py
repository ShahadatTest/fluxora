"""Explicit owner review, approval and automatic local batch application."""
import json
from uuid import uuid4

from .errors import CashflowError
from .agent_workflows import catalog, decide, revision
from .money import format_amount


def _terms(web,job,ledger):
    def currency(args,parent=''):
        if args.get('currency'):return args['currency']
        for field in ['invoice_id','receivable_id','bill_id','outflow_id','item_id','account_id','source_id']:
            ident=args.get(field)
            for group in [ledger.receivables,ledger.outflows,ledger.accounts]:
                if isinstance(ident,str) and ident in group:return group[ident].currency
        return parent
    money_fields={'amount','amount_minor','new_amount','new_amount_minor','new_total','opening','closing','signed_minor','principal_minor','interest_minor','opening_principal_minor'}
    def values(args,ccy=''):
        if isinstance(args,list):return '<ol>'+''.join('<li>'+values(v,ccy)+'</li>' for v in args)+'</ol>'
        if not isinstance(args,dict):return web.esc(str(args))
        ccy=currency(args,ccy);rows=[]
        for key,value in args.items():
            if key=='assignments' and isinstance(value,dict):
                display='<ul>'+''.join('<li>'+web.esc(str(ident))+': '+web.esc(format_amount(amount,ledger.receivables[ident].currency) if ident in ledger.receivables and type(amount) is int else str(amount))+'</li>' for ident,amount in value.items())+'</ul>'
            elif isinstance(value,(dict,list)):display=values(value,ccy)
            elif key in money_fields and type(value) is int and ccy:display=web.esc(format_amount(value,ccy))
            else:display=web.esc('Not set' if value is None else 'Yes' if value is True else 'No' if value is False else str(value))
            rows.append(f"<li><b>{web.esc(key.replace('_',' '))}:</b> {display}</li>")
        return '<ul style="overflow-wrap:anywhere">'+''.join(rows)+'</ul>'
    sections=[]
    for index,step in enumerate(job['steps'],1):
        sections.append(f"<h3>{index}. {web.esc(step['operation'].replace('_',' ').capitalize())}</h3>"+values(step['arguments']))
    return ''.join(sections)


def _financial_preview(web,job):
    preview=job['preview'];rows=[]
    for ccy,before in preview['cash_before_minor'].items():
        rows.append(f"<tr><td>Current book cash</td><td>{web.esc(format_amount(before,ccy))}</td><td>{web.esc(format_amount(preview['cash_after_minor'][ccy],ccy))}</td></tr>")
    for ccy,before in preview['forecast_before'].items():
        after=preview['forecast_after'][ccy]
        for label,key in [('Forecast minimum','minimum_minor'),('Forecast closing','closing_minor'),('Cash floor','floor_minor')]:
            rows.append(f"<tr><td>{web.esc(label)}</td><td>{web.esc(format_amount(before[key],ccy))}</td><td>{web.esc(format_amount(after[key],ccy))}</td></tr>")
        rows.append(f"<tr><td>{web.esc(ccy)} first breach</td><td>{web.esc(before['first_breach_date'] or 'None under assumptions')}</td><td>{web.esc(after['first_breach_date'] or 'None under assumptions')}</td></tr>")
    return ("<table class='wrap-t'><tr><th>Book cash / conditional forecast</th><th>Before</th><th>Proposed</th></tr>"+''.join(rows)+'</table>')


def mount(web):
    if ('GET','/agent/workflows') in web.ROUTES:return

    def queue(handler, req):
        def build(ledger):
            rows=''.join(f"<tr><td><a href='/agent/workflows/{web.esc(j['id'])}'>{web.esc(j['label'])}</a></td><td>{web.esc(j['status'])}</td><td>{len(j['steps'])}</td></tr>" for j in reversed(list(ledger.agent_workflows.values())))
            body=("<h1>Agent workflows</h1><p>The agent prepares work. You review exact changes and approve once. "
                  "The server then applies those approved local steps automatically. No model approval tool, real message or payment.</p>"
                  "<p><a href='/agent'>Ask Agent to prepare work</a> · <a href='/actions'>Action History</a></p>"
                  "<table class='wrap-t'><tr><th>Workflow</th><th>Status</th><th>Steps</th></tr>"+rows+'</table>'
                  "<div class='card'><h2>Try a labelled mock workflow</h2><p>Prepare a forecast snapshot for review. No external model call.</p>"
                  f"<form method='post' action='/agent/preview'>{web.csrf_field()}"
                  f"<input type='hidden' name='request_id' value='{uuid4().hex}'><input type='hidden' name='business_id' value='{web.esc(ledger.business_id)}'>"
                  "<input type='hidden' name='provider' value='mock'><input type='hidden' name='data_mode' value='minimized'>"
                  "<input type='hidden' name='question' value='Prepare a workflow to save a forecast snapshot'>"
                  "<button>Preview mock request</button></form></div>"
                  "<details><summary>Supported local workflow operations and required arguments</summary>"
                  f"<pre class='dump'>{web.esc(json.dumps(catalog(),indent=2))}</pre></details>")
            return web.page('Agent workflows',body,active='/agent')
        return handler._send_html(200,web.read_state(build))

    def detail(handler,req,id):
        def build(ledger):
            job=ledger.agent_workflows.get(id)
            if job is None:raise CashflowError('No workflow exists in this active business.')
            stale=revision(ledger)!=job['basis']
            body=(f"<h1>Review workflow: {web.esc(job['label'])}</h1><p>Status: <b>{web.esc(job['status'])}</b></p>"
                  "<p>Approve the exact parameters below. Documents are evidence, never authority. This sandbox sends no real messages or money.</p>"
                  f"<p><a href='/agent/workflows'>All workflows</a></p><div class='card'><h2>Exact proposed operations</h2>"
                  +_terms(web,job,ledger)+f"<details><summary>Inspect exact structured parameters</summary><pre class='dump'>{web.esc(json.dumps(job['steps'],indent=2))}</pre></details></div>"
                  "<div class='card'><h2>Private simulation of changes</h2><p>Preview only. Generated identifiers/timestamps may differ on execution. All amounts are integer minor units, with currencies separate.</p>"
                  +_financial_preview(web,job)+f"<details><summary>Inspect source-linked changes and current book cash</summary><pre class='dump'>{web.esc(json.dumps(job['preview'],indent=2,default=str))}</pre></details></div>")
            fields=(web.csrf_field()+f"<input type='hidden' name='business_id' value='{web.esc(ledger.business_id)}'>"
                    f"<input type='hidden' name='review_token' value='{job['review_token']}'>")
            if job['status']=='pending_approval':
                if stale:body+="<p class='err'>Ledger or action terms changed. Ask Agent to prepare a fresh workflow before approval.</p>"
                else:
                    facts=("<label><input type='checkbox' name='observed_confirmed' value='yes' required> I separately confirm the cited payment/refund/transfer observations are source-backed facts, not model assumptions. Approval itself is not evidence of payment.</label>" if job['requires_observed_confirmation'] else '')
                    consent=("<label><input type='checkbox' name='counterparty_confirmed' value='yes' required> I separately confirm source-backed external counterparty acceptance of these exact supplier terms. My approval alone supplies no consent.</label>" if job['requires_counterparty_confirmation'] else '')
                    body+=(f"<form method='post' action='/agent/workflows/{web.esc(id)}/decide'>{fields}<input type='hidden' name='decision' value='approve'>"
                           "<label><input type='checkbox' name='review_confirmed' value='yes' required> I reviewed and approve these exact operations and source-linked changes. For sandbox sends this includes approval of the displayed action terms.</label>"
                           +facts+consent+"<label><input type='checkbox' name='duplicates_confirmed' value='yes'> I checked any duplicate warnings and explicitly permit the reviewed duplicates.</label>"
                           "<button>Approve &amp; run approved steps</button></form>")
                body+=f"<form method='post' action='/agent/workflows/{web.esc(id)}/decide'>{fields}<input type='hidden' name='decision' value='reject'><button>Reject workflow</button></form>"
            if job['execution'] is not None:
                body+="<div class='card'><h2>Executed local results</h2>"+f"<pre class='dump'>{web.esc(json.dumps(job['execution'],indent=2,default=str))}</pre></div>"
            return web.page('Review agent workflow',body,active='/agent')
        return handler._send_html(200,web.read_state(build))

    def decision(handler,req,id):
        allowed={'csrf','business_id','review_token','decision','review_confirmed','observed_confirmed','duplicates_confirmed','counterparty_confirmed'}
        if set(req.form)-allowed or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current owner review form.')
        if req.field('decision')=='approve' and req.field('review_confirmed')!='yes':raise CashflowError('Explicitly approve the exact reviewed operations.')
        def apply(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('Business changed. Reload the current workflow.')
            return decide(ledger,id,req.field('review_token'),req.field('decision'),ctx=web._owner_ctx(ledger),
                observed_confirmed=req.field('observed_confirmed')=='yes',duplicates_confirmed=req.field('duplicates_confirmed')=='yes',
                counterparty_confirmed=req.field('counterparty_confirmed')=='yes')
        web.mutate_state(apply)
        return handler._redirect('/agent/workflows/'+id)

    web.route('GET','/agent/workflows')(queue)
    web.route('GET','/agent/workflows/<id>')(detail)
    web.route('POST','/agent/workflows/<id>/decide')(decision)
    web._workflow_mounted=True
