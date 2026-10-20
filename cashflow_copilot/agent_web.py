"""Server-rendered agent panel, routes and in-memory provider status.

The web server mounts this module once. All mutation routes reuse its CSRF
and transactional boundaries. Verification is scoped to the active environment
configuration and forgotten on server startup.
"""

from __future__ import annotations

import threading
import json
import time
from uuid import uuid4

from .errors import CashflowError
from .model_adapter import (
    ENV_API_KEY,
    MOCK_PROVIDER_LABEL,
    OpenAICompatibleAdapter,
)

_MOCK_OPTION_LABEL = f"Mock — {MOCK_PROVIDER_LABEL}"
_adapter_lock = threading.RLock()
_active_adapter = None
_public_adapter = OpenAICompatibleAdapter()


def reset_live_adapter():
    """A server start forgets verification. No configuration is persisted."""
    global _active_adapter
    with _adapter_lock:
        _active_adapter = None


def live_adapter():
    from .ui import REQUEST
    if getattr(REQUEST,'public_demo',False):return getattr(REQUEST,'public_ai_adapter',None) or _public_adapter
    manager=getattr(REQUEST,'local_connections',None)
    if manager is not None:return manager.active_adapter()
    global _active_adapter
    with _adapter_lock:
        if (_active_adapter is None
                or not _active_adapter.configuration_matches_env()):
            _active_adapter = OpenAICompatibleAdapter.from_env()
        return _active_adapter


def _live_status():
    """Current live-provider status from the environment (no network)."""
    return live_adapter().status()


def banner_status():
    status = _live_status()
    if status.state == 'disconnected':
        return 'AI not connected — disconnected; deterministic tools available'
    return f'Live provider: {status.state} — synthetic sandbox'


def _state_pill(web, state: str) -> str:
    cls = {
        "disconnected": "warn",
        "configured-but-unverified": "warn",
        "verified": "ok",
        "error": "bad",
        "mock": "sim",
    }.get(state, "warn")
    return f"<span class='pill {cls}'>{web.esc(state)}</span>"


def _panel_html(web, compact: bool = False, heading: bool = True) -> str:
    """The 'Ask about your cash flow' panel (used on / and /agent)."""
    from .agent_recovery import reports_html
    status = _live_status()
    disconnected = status.state == "disconnected"
    live_option = ""
    cooldown=getattr(status,'retry_after_seconds',0)
    if not disconnected:
        live_option = (
            f"<option value='live'{' disabled' if cooldown else ''}>Live model "
            f"({web.esc(status.provider_label)})</option>")
    notice = ""
    from .ui import REQUEST
    if getattr(REQUEST,'local_connections',None) is not None:notice='<p><a href="/settings/connections">Manage API profiles and Telegram in local settings</a></p>'
    if disconnected:
        notice += (
            "<p class='warn'><b>AI not connected</b> — no model is called. "
            "You can still try the labelled mock demo below; it is "
            "deterministic and never presented as a live model.</p>")
    elif status.state == "error":
        notice += (
            f"<p class='err'><b>Provider error:</b> "
            f"{web.esc(status.detail)}</p>")
        if cooldown:notice+=f"<p class='warn'>Provider cooldown: approximately {cooldown} seconds. No background retry is running. Reload after the cooldown; deterministic tools and the labelled mock remain available.</p>"
    live_line = ""
    if not disconnected:
        live_line = (
            f"<p class='mut'>Live provider: {web.esc(status.provider_label)}"
            f"{' — ' + web.esc(status.detail) if status.detail else ''}</p>")
    rows = "3" if compact else "2"
    h2 = "<h2>Ask about your cash flow</h2>" if heading else ""
    business_id=web.read_state(lambda ledger:ledger.business_id)
    policy = getattr(live_adapter(), 'request_policy', lambda: {})()
    policy_line = ''
    if policy:
        reasoning_line=(f"Reasoning effort: {web.esc(policy['reasoning_effort'])}. " if 'reasoning_effort' in policy
                        else '512 reasoning tokens requested. ')
        policy_line = (f"<p class='mut'>OpenRouter model: {web.esc(status.model or 'not configured')}.</p>"
            "<details><summary>OpenRouter limits and data policy</summary><p class='mut'>"
            f"OpenRouter request limits: {policy.get('max_tokens_per_response',2048):,} output tokens per call "
            "(including reasoning on supported providers). "+reasoning_line+
            "256 KiB request limit; tool parameter support required, data collection denied, "
            "provider fallback disabled. These are request settings, not a dollar spending cap "
            "or a guarantee about third-party retention. Live model/tool quality is untested "
            "until you run and review an evaluation.</p></details>")
    return f"""
<div class="card" id="agent-panel">
{h2}
<p><a href='/agent/tasks'>Cash-flow tasks — direct reports and reviewed task translation</a></p>
<p>Connection: {_state_pill(web, status.state)}
<span class='pill sim'>{web.esc('mocked provider — deterministic demo, not a live model')}</span></p>
{live_line}
{notice}
{policy_line}
<form method="post" action="/agent/preview">{web.csrf_field()}
<input type='hidden' name='request_id' value='{uuid4().hex}'>
<input type='hidden' name='business_id' value='{web.esc(business_id)}'>
<label>Question
<textarea name="question" rows="{rows}" style="width:100%" required
placeholder="Why will we breach our cash floor?"></textarea></label>
<div class="row">
<div><label>Provider
<select name="provider">
<option value="mock">{web.esc(_MOCK_OPTION_LABEL)}</option>
{live_option}
</select></label></div>
<div><label>Data sent in tool replies<select name='data_mode'><option value='minimized'>Minimized facts — omit source notes and party labels</option><option value='full_metadata'>Full structured metadata — include source text</option></select></label></div>
<div><button class="primary" type="submit">Preview data before asking</button></div>
</div></form>
<p class='mut'>Model prose is unverified commentary and may contain errors.
Use the separately displayed deterministic tool results and evidence IDs for
financial figures. The agent can only read and create
drafts and pending workflows. It cannot approve itself or execute unapproved changes. Drafts appear on
the <a href="/actions">Action History</a> page for owner review.</p>
{reports_html(web)}
</div>"""


def _run_history_html(web, db_path: str, business_id: str,
                      limit: int = 10) -> str:
    from .agent import list_agent_runs
    runs = list_agent_runs(db_path, business_id=business_id, limit=limit)
    if not runs:
        return "<p class='mut'>No agent runs yet.</p>"
    rows = []
    for r in runs:
        excerpt = '; '.join(_deterministic_lines(r['tool_calls']))[:220]
        question=web.esc(r['question'][:80])
        if r.get('request_id'):
            question=f"<a href='/agent/requests/{web.esc(r['request_id'])}'>{question}</a>"
        rows.append(
            f"<tr><td>{web.esc(r['created_at'])}</td>"
            f"<td>{question}</td>"
            f"<td>{web.esc(r['provider_label'])}</td>"
            f"<td>{web.esc(r.get('disclosure',{}).get('mode','historical / not recorded'))}<br>"
            f"{web.esc(json.dumps(r.get('usage',{}).get('reported_subtotals',{})) if r.get('usage',{}).get('reported_subtotals') else 'Usage unavailable')}; "
            f"{'complete response counters' if r.get('usage',{}).get('complete') else 'incomplete / unknown'}</td>"
            f"<td>{len(r['tool_calls'])}</td>"
            f"<td>{web.esc(excerpt)}</td></tr>")
    return ("<table class='wrap-t'><tr><th>When</th><th>Question</th>"
            "<th>Provider</th><th>Data / measured usage (no cost estimate)</th><th>Tools</th><th>Answer excerpt</th></tr>"
            + "".join(rows) + "</table>")


def _answer_html(web, answer: str) -> str:
    return ("<details class='card' data-unverified-commentary><summary>Model commentary (unverified)</summary>"
            "<p>This optional text can contain incorrect amounts, dates or explanations. It is not the financial answer. Review the deterministic results and exact proposed terms above.</p>"
            f"{web.esc(answer).replace(chr(10), '<br>')}</details>")


def _deterministic_lines(calls):
    lines=[]
    for call in calls:
        if not call.get('ok'):continue
        result=call.get('result',{});name=call['name']
        if name=='compute_forecast':
            for ccy,row in result.get('currencies',{}).items():
                lines.append(f"{ccy}: projected minimum {row['min_balance']} on {row['min_balance_date']}; closing {row['closing']}; first floor breach {row['first_breach_date'] or 'none under recorded assumptions'}; worst shortfall {row['worst_shortfall']}. Expected collections are conditional.")
        elif name=='compare_plans':
            lines.append('No feasible plan within these bounded allowed strategies.' if result.get('no_feasible_plan') else 'Financially sufficient conditional proposals exist; owner approval, counterparty acceptance and observed cash remain separate.')
        elif name=='prepare_workflow':lines.append(f"Workflow {result['id']}: {result['status']}. Review exact operations and source evidence; a pending proposal has no financial effect.")
        elif name=='draft_action':lines.append(f"Action {result['action_id']}: DRAFT, {result['terms'].get('amount','amount in exact terms')}. No approval, sending or payment.")
        elif name=='get_evidence':lines.append('Recorded settlement and loan facts are shown below. All-dates settlement is separate from current cash; source authenticity is not certified.')
        elif name=='read_workspace':
            view=call.get('arguments',{}).get('view','catalog')
            if view=='weekly_cashflow':
                from .money import format_amount
                for ccy,row in result.get('currencies',{}).items():
                    weeks=row.get('weeks',[])
                    if weeks:
                        lowest=min(weeks,key=lambda w:w['min_balance_minor'])
                        lines.append(f"{ccy}: {len(weeks)}-week projected minimum {format_amount(lowest['min_balance_minor'],ccy)} on {lowest['min_date']}. Expected collections are conditional; see the deterministic weekly table and sources below.")
            else:lines.append(f"Read-only workspace report: {view}. Inspect the exact returned report in the tool results.")
        elif name=='missing_facts':lines.append('Missing-fact clarifications are listed in the deterministic tool result. No missing date or source was invented.')
    return lines or ['No computed financial answer or new proposal was returned. Inspect the request status and tool log.']


def _deterministic_answer_html(web,calls):
    return "<div class='card'><h2>Deterministic answer</h2><p>Generated directly from successful tool results. This does not certify source truth or optional model commentary.</p><ul>"+''.join('<li>'+web.esc(line)+'</li>' for line in _deterministic_lines(calls))+'</ul></div>'


def _sources_html(web, calls):
    sections = []
    for index, call in enumerate(calls, 1):
        if not call.get('ok'):
            continue
        result = call.get('result', {})
        if not isinstance(result,dict):
            continue  # Some workspace reports are lists; raw results remain available.
        if call.get('name')=='read_workspace' and call.get('arguments',{}).get('view')=='weekly_cashflow':
            from .money import format_amount
            for ccy,currency in result.get('currencies',{}).items():
                rows=[]
                for week in currency.get('weeks',[]):
                    refs=sorted({ref for driver in week.get('drivers',[]) for ref in driver.get('evidence_ids',[])})
                    cells=[week['week'],week['start'],week['end'],week['min_date'],
                           format_amount(week['min_balance_minor'],ccy),format_amount(week['closing_minor'],ccy),
                           format_amount(week['floor_minor'],ccy),week.get('first_breach_date') or 'None',
                           ', '.join(refs) or 'No dated drivers this week']
                    rows.append('<tr>'+''.join('<td>'+web.esc(str(cell))+'</td>' for cell in cells)+'</tr>')
                sections.append("<section class='card' data-weekly-financial-facts><h3>Deterministic weekly cash: "+web.esc(ccy)+
                    "</h3><p>Projected obligations and expected collections, not observed bank balances. Amounts are currency-qualified major units.</p>"
                    "<p class='mut'>On a narrow screen, scroll the table horizontally to inspect all columns.</p>"
                    "<div class='weekly-cash-scroll' tabindex='0' role='region' aria-label='Weekly cash table: "+web.esc(ccy)+"'><table class='weekly-cash-table'><tr><th>Week</th><th>Start</th><th>End</th><th>Minimum date</th><th>Minimum</th><th>Closing</th><th>Floor</th><th>First breach</th><th>Driver evidence</th></tr>"+
                    ''.join(rows)+"</table></div></section>")
        protected=[row['id'] for row in result.get('outflows',[]) if row.get('protected')] if call.get('name')=='get_evidence' else []
        if protected:
            sections.append("<section class='card' data-protected-obligations><h3>Protected obligations</h3><p>"+
                web.esc(', '.join(protected))+": no deferral draft or workflow is available. Owner approval does not waive protection.</p></section>")
        review=result.get('financial_review',{})
        if review.get('rows'):
            records=[]
            for row in review['rows']:
                facts=''.join(f"<tr><td>{web.esc(label)}</td><td>{web.esc(value)}</td></tr>" for label,value in row['values'].items())
                records.append(f"<details{' open' if row['record_type']=='loan' else ''}><summary>{web.esc(row['record_type'])}: {web.esc(row['id'])} — {web.esc(row['currency'])}</summary>"
                    f"<p>{web.esc(row['interpretation'])}</p><table class='wrap-t'><tr><th>Recorded calculation</th><th>Value</th></tr>{facts}</table>"
                    f"<p>Evidence: {web.esc(', '.join(row['evidence_ids']) or 'No source recorded')}</p></details>")
            sections.append(f"<h3>Tool {index}: deterministic settlement and loan facts</h3><p>Snapshot: {web.esc(review['as_of'])}. {web.esc(review['rules'])}</p>"+''.join(records)
                +('<p>Fact rows truncated; inspect the ledger for remaining records.</p>' if review['truncated'] else ''))
        if all(isinstance(result.get(k),dict) for k in ('cash_before_display','cash_after_display')):
            rows=''.join(f"<tr><td>{web.esc(ccy)}</td><td>{web.esc(value)}</td><td>{web.esc(result['cash_after_display'].get(ccy,'Not available'))}</td></tr>"
                for ccy,value in result['cash_before_display'].items())
            sections.append(f"<h3>Tool {index}: deterministic workflow cash</h3>"
                f"<p>Reported workflow status: {web.esc(result.get('status','unknown'))}. Book cash before and proposed cash after are recorded ledger values. "
                "A pending proposal has not executed. These values do not verify bank/source truth or the model commentary.</p>"
                "<table class='wrap-t'><tr><th>Currency</th><th>Book cash before</th><th>Proposed after</th></tr>"+rows+'</table>')
        payloads = [('forecast', result)] if call['name'] == 'compute_forecast' else []
        if call['name'] in {'compute_forecast', 'compare_plans'} and 'baseline' in result:
            payloads.append(('baseline', result['baseline']))
        for label, payload in payloads:
            for currency, values in payload.get('currencies', {}).items():
                basis = values.get('source_basis')
                if not basis:
                    continue  # Historical runs have no added provenance field.
                rows = []
                for row in basis['inputs'] + basis['drivers']:
                    refs = ', '.join(row['evidence_ids']) or 'No source recorded'
                    rows.append(f"<tr><td>{web.esc(row['kind'])}</td><td>{web.esc(row['id'])}</td>"
                                f"<td>{web.esc(str(row.get('date', 'snapshot')))}</td><td>{web.esc(refs)}</td></tr>")
                sections.append(f"<h3>Tool {index}: {web.esc(label)} — {web.esc(currency)}</h3>"
                    f"<p>{web.esc(payload.get('scenario_description', 'Recorded baseline inputs'))}</p>"
                    + '<table class="wrap-t"><tr><th>Computed forecast metric</th><th>Value</th></tr>'
                    + ''.join(f"<tr><td>{web.esc(title)}</td><td>{web.esc(str(values.get(key) or 'None under assumptions'))}</td></tr>" for title,key in [
                        ('Opening available cash','opening'),('Cash floor','floor'),('First breach date','first_breach_date'),
                        ('Balance at first breach','first_breach_balance'),('Shortfall at first breach','first_breach_shortfall'),
                        ('Minimum balance','min_balance'),('Minimum date','min_balance_date'),('Worst shortfall','worst_shortfall'),('Closing','closing')])
                    + '</table>'
                    +
                    "<table class='wrap-t'><tr><th>Input kind</th><th>Record</th><th>Date</th><th>Evidence IDs</th></tr>"
                    + ''.join(rows) + '</table>'
                    + ("<p>Source list truncated. Inspect Evidence &amp; Ledger for remaining inputs.</p>" if basis['truncated'] else ''))
    if not sections:
        return ''
    return ("<div class='card'><h2>Sources returned by deterministic tools</h2>"
            "<p>These references come from tool results, independently of the commentary. "
            "They do not certify source authenticity, model claims or authorization. "
            "Expected receipts remain conditional; floor values are recorded owner constraints. "
            "Inspect the full tool results below for amounts and assumptions.</p>"
            + ''.join(sections) + '</div>')


def _usage_html(web,result):
    u=result.usage or {};d=result.disclosure or {}
    if d.get('execution_mode')=='guided':
        provider=d.get('translation_provider')
        detail=('Task translation: '+web.esc(provider)+'. Provider-reported translation counters: '+web.esc(json.dumps(d.get('translation_usage') or 'unavailable'))+'. Translation can incur charges; no dollar estimate is made.' if provider else 'This task used no model for either selection or execution.')
        return "<div class='card'><h2>Task execution and model usage</h2><p>Financial execution: zero external model calls. Financial results were never sent back to a model.</p><p>"+detail+'</p></div>'
    totals=u.get('reported_subtotals',{})
    counters=', '.join(f'{k.replace("_"," ")}: {v}' for k,v in totals.items()) or 'Unavailable — no provider-reported token counters'
    return ("<div class='card'><h2>Data selection and measured usage</h2>"
        f"<p>Data mode: {web.esc(d.get('mode','not recorded (historical run)'))}. {web.esc(d.get('note',''))}</p>"
        f"<p>{web.esc(counters)}. {'All adapter responses reported the three counters.' if u.get('complete') else 'Coverage incomplete or unavailable; unknown is not zero.'}</p>"
        f"<p>{web.esc(u.get('note','Usage was not recorded for this historical run. No cost estimate.'))}</p></div>")


def mount(web) -> None:
    """Register agent routes + panel on the existing web app object."""
    if getattr(web, "_agent_mounted", False):
        return

    from . import agent as agent_mod
    from . import agent_workflow_web
    agent_workflow_web.mount(web)
    from . import agent_tasks_web
    agent_tasks_web.mount(web)
    from .model_adapter import AdapterError, MockProviderAdapter

    def agent_page(handler, req):
        def build(ledger):
            body = (
                "<h1>Ask about your cash flow</h1>"
                "<div class='card'><h2>Guided tasks and direct reports</h2><p>Run common reports without a model, or review a model-translated task whose financial answer is produced by code.</p><a class='btn primary' href='/agent/tasks'>Open cash-flow tasks</a></div>"
                "<p><a href='/agent/workflows'>Agent workflows — review once, then run approved local steps</a></p>"
                f"{_panel_html(web, heading=False)}"
                "<div class='card'><h2>Recent agent runs</h2>"
                f"{_run_history_html(web, web.database_path(), ledger.business_id)}"
                "</div>"
                "<div class='card'><h2>Verify live provider</h2>"
                "<p class='mut'>Runs a minimal real call against the "
                "configured endpoint. Only meaningful when "
                f"{web.esc(ENV_API_KEY)} is set.</p>"
                f"<form method='post' action='/agent/verify'>"
                f"{web.csrf_field()}"
                "<button type='submit'>Verify connection</button></form></div>"
            )
            return web.page("Ask Agent", body, active="/agent")
        return handler._send_html(200, web.read_state(build))

    def agent_ask(handler, req):
        if set(req.form)-{'csrf','request_id','business_id','question','provider','data_mode','token','confirmed'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Reload Ask Agent and use only its current request fields.')
        stage=None
        if req.field('token'):
            if set(req.form)-{'csrf','token','confirmed'} or req.field('confirmed')!='yes':raise CashflowError('Confirm the exact data preview without replacing its inputs.')
            stage=web.staged().get(req.field('token'))
            if not stage or stage.get('kind')!='agent_disclosure' or time.monotonic()-stage['created']>900:
                raise CashflowError('Data preview expired. Review a fresh preview; saved requests remain available in history.')
        question = stage['question'] if stage else req.field("question").strip()
        provider = stage['provider'] if stage else req.field("provider")
        data_mode=stage['data_mode'] if stage else req.field('data_mode','full_metadata')
        if not question:
            raise CashflowError("Ask a question first.")
        if provider == "live":
            adapter = live_adapter()
            if stage and stage['configuration_id']!=id(adapter):raise CashflowError('Provider configuration changed. Review a fresh preview before sending.')
        elif provider == "mock":
            adapter = MockProviderAdapter()
        else:
            raise CashflowError(f"unknown provider {provider!r}")

        from .agent_requests import run_request
        owner_ctx=web.read_state(web._owner_ctx)
        if (stage['business_id'] if stage else req.field('business_id'))!=owner_ctx.business_id:
            raise CashflowError('The active business changed since this form was opened. Reload and review the current business.')
        outcome=run_request(web.database_path(),stage['request_id'] if stage else req.field('request_id'),question,provider,adapter,
            ctx=owner_ctx,lock=web.LOCK,data_mode=data_mode,expected_revision=stage['revision'] if stage else None)
        return render_outcome(handler,outcome)

    def agent_preview(handler,req):
        from .agent_disclosure import disclosure_preview,disclosure_revision
        from .agent_requests import _validate
        if set(req.form)-{'csrf','request_id','business_id','question','provider','data_mode'} or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Reload the current Ask Agent form.')
        question=req.field('question').strip();provider=req.field('provider');ident=req.field('request_id')
        _validate(ident,question,provider)
        adapter=live_adapter() if provider=='live' else MockProviderAdapter()
        def build(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('The active business changed. Reload Ask Agent.')
            preview=disclosure_preview(ledger,question,req.field('data_mode','minimized'),ctx=web._owner_ctx(ledger))
            token=web._stage(dict(kind='agent_disclosure',created=time.monotonic(),request_id=ident,business_id=ledger.business_id,
                question=question,provider=provider,data_mode=preview['mode'],configuration_id=id(adapter),revision=disclosure_revision(ledger)))
            destination=adapter.status().base_url if provider=='live' else 'local deterministic mock'
            body=f"<h1>Review AI request data</h1><p>Provider: {web.esc(adapter.provider_label)}. Destination: {web.esc(destination)}. Data mode: {web.esc(preview['mode'])}.</p><p>{web.esc(preview['notice'])}</p><p>{'Labelled mock runs locally: no external provider receives this data.' if provider=='mock' else 'Confirming sends these messages and subsequent filtered tool replies to your configured live provider. Preview itself made no provider call.'}</p>"
            policy = getattr(adapter, 'request_policy', lambda: {})()
            if provider == 'live' and policy:
                body += ("<div class='card'><h2>Provider request settings</h2>"
                    "<p>OpenRouter receives this request. Output and context are bounded; "
                    "data collection is denied in the routing settings. Third-party behavior "
                    "has not been independently verified.</p>"
                    "<details><summary>Inspect exact request limits and routing settings</summary>"
                    f"<pre class='dump'>{web.esc(json.dumps(policy,indent=2))}</pre>"
                    "</details>"
                    "<p>Provider reasoning continuation may be returned to the same configured gateway "
                    "during this run. It is not financial evidence and is not saved in run history. "
                    "No automatic cross-model fallback. Token limits are not a dollar spending cap.</p></div>")
            for key,label in [('initial_messages','Exact initial messages'),('tool_definitions','Available read/proposal tool schemas'),('current_read_tool_samples','Current read-tool reply samples after selected filtering')]:
                body+=f"<div class='card'><details><summary>{label}</summary><pre class='dump'>{web.esc(json.dumps(preview[key],indent=2))}</pre></details></div>"
            body+=f"<p><a href='/agent'>Change question or data selection</a></p><form method='post' action='/agent/ask'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'><label><input type='checkbox' name='confirmed' value='yes' required> I reviewed the question, metadata selection and provider destination.</label><button type='submit'>Confirm and ask {'labelled mock' if provider=='mock' else 'live provider'}</button></form>"
            return web.page('AI data preview',body,active='/agent')
        return handler._send_html(200,web.read_state(build))

    def agent_status(handler,req,id):
        from .agent_requests import request_status
        ctx=web.read_state(web._owner_ctx)
        return render_outcome(handler,request_status(web.database_path(),id,ctx=ctx,lock=web.LOCK))

    def render_outcome(handler,outcome):
        from .agent_recovery import recovery_html
        result=outcome.result
        if result is None:
            body=f"<h1>Agent request — {web.esc(outcome.status)}</h1><p>{web.esc(outcome.note)}</p><p><a href='/agent/requests/{outcome.request_id}'>Refresh this request</a> · <a href='/review'>Daily Review</a> · <a href='/agent'>Ask a fresh question</a></p>"
            body+=recovery_html(web,outcome)
            return handler._send_html(202 if outcome.status=='running' else 200,web.page('Agent request',body,active='/agent'))
        tool_rows = "".join(
            f"<li><b>{web.esc(c['name'])}</b> "
            f"<span class='pill {'ok' if c['ok'] else 'bad'}'>"
            f"{'ok' if c['ok'] else 'error'}</span>"
            + (f" <span class='mut'>{web.esc(c['error'][:200])}</span>"
               if c.get("error") else "")
            + ("<details data-tool-result><summary>Deterministic tool result and evidence</summary>"
               f"<pre class='dump'>{web.esc(json.dumps(c['result'], indent=2))}</pre>"
               "</details>" if 'result' in c else '')
            + ("<details><summary>Filtered tool reply prepared for the next call</summary>"
               f"<pre class='dump'>{web.esc(json.dumps(c['outbound_tool_reply'],indent=2))}</pre><p>Included in a request attempt: {web.esc(str(c.get('included_in_request_attempt',False)))}. Transport delivery is not independently verified.</p></details>" if 'outbound_tool_reply' in c and result.disclosure.get('execution_mode')!='guided' else '')
            + "</li>"
            for c in result.tool_calls)
        if result.disclosure.get('execution_mode')=='guided':
            from .agent_task_reports import report_html
            financial_body=web.read_state(lambda ledger:report_html(web,ledger,result.tool_calls))
            skill=result.disclosure.get('skill')
            if skill:financial_body="<p>Reviewed skill: "+web.esc(skill['title'])+" · v"+web.esc(skill['version'])+" · "+web.esc(skill['selection'])+". <a href='/agent/skills'>Skill scope</a></p>"+financial_body
        else:
            financial_body=_deterministic_answer_html(web,result.tool_calls)+_sources_html(web,result.tool_calls)+_answer_html(web,result.answer)
        next_panel=("<p><a class='btn primary' href='/agent/tasks'>Choose another guided task</a></p>"
                    if result.disclosure.get('execution_mode')=='guided' else _panel_html(web,compact=True))
        body = (
            "<h1>Agent answer</h1>"
            f"<p>Request: {web.esc(outcome.status)}{' · replayed' if outcome.replayed else ''}. {web.esc(outcome.note)}</p>"
            f"<p><a href='/agent/requests/{outcome.request_id}'>Saved request result</a></p>"
            f"<p class='mut agent-question' style='overflow-wrap:anywhere'>Q: {web.esc(result.question)}</p>"
            f"<p>Provider: <span class='pill sim'>"
            f"{web.esc(result.provider_label)}</span></p>"
            f"{recovery_html(web,outcome)}"
            f"{financial_body}"
            f"{_usage_html(web,result)}"
            "<div class='card'><h2>Tool calls</h2>"
            f"<ul>{tool_rows or '<li class=mut>none</li>'}</ul></div>"
            "<div class='card'><h2>Next actions</h2><ul>"
            "<li><a href='/agent/workflows'>Agent workflows</a> — review proposed operations, approve once, and inspect executed results.</li>"
            "<li><a href='/plans'>Compare plans</a> — review engine-computed "
            "alternatives.</li>"
            "<li><a href='/actions'>Action History</a> — review or approve "
            "drafts. Approval, sandbox send, external acceptance, observed payment "
            "and reconciliation are separate steps; approval alone creates no cash.</li>"
            "<li><a href='/ledger'>Evidence &amp; Ledger</a> — inspect "
            "sources behind the figures.</li>"
            "</ul></div>"
            f"{next_panel}"
        )
        return handler._send_html(200, web.page("Agent answer", body,
                                                active="/agent"))

    def agent_verify(handler, req):
        adapter = live_adapter()
        try:
            adapter.verify()
        except AdapterError:
            pass  # status() already records the error state
        return handler._redirect("/agent")

    web.route("GET", "/agent")(agent_page)
    web.route("POST", "/agent/ask")(agent_ask)
    web.route('POST','/agent/preview')(agent_preview)
    web.route('GET','/agent/requests/<id>')(agent_status)
    web.route("POST", "/agent/verify")(agent_verify)

    # Add the "Ask Agent" tab and the overview panel via the shared page()
    # wrapper. web.py resolves `page` through module globals at call time,
    # so patching web.page affects every rendered page without editing web.py.
    _orig_page = web.page

    def page_with_agent(title, body, active=""):
        if active == "/" and 'class="dashboard-intro"' not in body:
            body = body + _panel_html(web, compact=True)
        cls = "on" if active == "/agent" else ""
        out = _orig_page(title, body, active)
        anchor = ">Demo Workflow</a>"
        if anchor in out:
            out = out.replace(
                anchor,
                f"{anchor}<a href='/agent' class='{cls}'>Ask Agent</a>")
        return out

    web.page = page_with_agent
    web._agent_mounted = True
