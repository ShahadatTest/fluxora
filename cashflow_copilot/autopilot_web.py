"""Workspace-scoped policy, exact preparation limits and persisted run history."""
import json,re
from decimal import Decimal
from . import autopilot
from .errors import CashflowError
from .money import format_amount,minor_digits


def mount(web):
    def service(handler):
        value=getattr(handler.server,'autopilot',None)
        if value is None:raise CashflowError('Autopilot requires the persistent local launcher or hosted server.')
        return value

    def amount(text,currency):
        if not re.fullmatch(r'\d{1,14}(?:\.\d{1,6})?',text):raise CashflowError('Enter a nonnegative decimal DRAFT limit, without currency symbols or commas.')
        scaled=Decimal(text)*10**minor_digits(currency)
        if scaled!=scaled.to_integral_value():raise CashflowError('The DRAFT limit has too many decimal places for its currency.')
        return int(scaled)

    @web.route('GET','/autopilot')
    def page(handler,req):
        engine=getattr(handler.server,'autopilot',None)
        if engine is None:return handler._send_html(200,web.page('Autopilot',"<h1>Autopilot</h1><p>Use the persistent local launcher (<code>python -m tools.local_setup --start</code>) or hosted demo to run the background worker. No work was enabled.</p>",active='/agent/tasks'))
        def build(ledger):
            ctx=web._owner_ctx(ledger);row=engine.policy(web.database_path(),ledger,ctx=ctx);policy=json.loads(row['policy'])
            policy['caps']={c:policy['caps'].get(c,0) for c in sorted({a.currency for a in ledger.accounts.values()})}
            linked=engine.linked(web.database_path())
            body="<h1>Autopilot</h1><p>Automatic monitoring, conditional plan analysis and DRAFT preparation. Local deterministic tools · zero model calls. Synthetic workspace only.</p>"
            body+="<div class='card autopilot-status'><h2>"+('Running' if policy['enabled'] else 'Paused')+"</h2><p>Checks recorded data at the selected interval. Changed financial inputs trigger analysis; unchanged data produces only the periodic digest. Action/workflow changes trigger follow-up. The ledger snapshot date does not advance itself.</p><p>New actions stay DRAFT. Snapshot workflows stay pending approval. No bank connection, real counterparty message or payment is performed.</p></div>"
            body+="<section class='card'><h2>Rules &amp; limits</h2><form method='post' action='/autopilot/policy'>"+web.csrf_field()+"<input type='hidden' name='revision' value='"+web.esc(row['revision'])+"'>"
            body+="<label><input type='checkbox' name='enabled' value='yes'"+(' checked' if policy['enabled'] else '')+"> Run Autopilot for this workspace; resume this saved policy when the app restarts.</label>"
            body+="<label>Check interval<select name='interval_seconds'>"+''.join(f"<option value='{v}'"+(' selected' if policy['interval_seconds']==v else '')+f">{label}</option>" for v,label in [(30,'30 seconds'),(300,'5 minutes'),(900,'15 minutes'),(3600,'1 hour')])+"</select></label>"
            body+="<label>Summary interval<select name='digest_seconds'>"+''.join(f"<option value='{v}'"+(' selected' if policy['digest_seconds']==v else '')+f">{label}</option>" for v,label in [(3600,'Every hour'),(86400,'Every 24 hours'),(604800,'Every 7 days')])+"</select></label>"
            body+="<label>Automatic DRAFT strategy<select name='strategy'>"+''.join("<option value='"+v+"'"+(' selected' if policy['strategy']==v else '')+">"+label+"</option>" for v,label in [('off','Monitor and report only'),('auto-collections','Earlier collections'),('auto-deferrals','Permitted bill deferrals'),('auto-combined','Bill deferrals plus earlier collections')])+"</select></label><p>Prepare only a financially sufficient plan within the selected bounded strategy and the limits below. Every currency must pass separately. A sufficient conditional plan is not owner approval or counterparty acceptance.</p>"
            for currency,value in policy['caps'].items():
                body+=f"<label>Maximum per DRAFT · {web.esc(currency)}<input name='cap_{web.esc(currency)}' inputmode='decimal' value='{Decimal(value)/(10**minor_digits(currency))}' required></label>"
            body+="<p>Zero disables new DRAFTs in that currency. Up to eight new DRAFTs per batch. Source/consent, payroll, overlap and existing owner constraints remain enforced. No partial over-limit plan is prepared.</p>"
            for key,label in [('snapshot','Prepare a forecast snapshot for separate owner approval after financial input changes.'),('notify','Send Autopilot updates and eligible review buttons to my linked private Telegram account.')]:
                body+=f"<label><input type='checkbox' name='{key}' value='yes'"+(' checked' if policy[key] else '')+(' disabled' if key=='notify' and not linked else '')+"> "+label+"</label>"
            if not linked:body+="<p>Telegram delivery is unavailable until you link this workspace from the configured owner account. Public visitors cannot use the operator's private chat. <a href='/telegram'>Telegram setup</a></p>"
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I authorize these scheduled read/DRAFT/pending preparations and any selected Telegram updates. Each final approval and source confirmation remains separate.</label><button class='primary'>Save policy</button></form></section>"
            body+="<form method='post' action='/autopilot/check'>"+web.csrf_field()+"<button>Check now</button></form><p>Check now does not bypass timing, job, deadline or duplicate limits. Read-only/DRAFT jobs use no provider and run while this app is running.</p>"
            rows=[]
            for job in engine.history(web.database_path(),ledger,ctx=ctx):
                rows.append("<tr><td><a href='/autopilot/jobs/"+job['id']+"'>"+web.esc(job['kind'].replace('_',' '))+"</a></td><td>"+web.esc(job['status'])+"</td><td>"+('Queued for linked Telegram' if job['notified'] else 'No delivery queued')+"</td></tr>")
            body+="<h2>Run history</h2><table><tr><th>Trigger</th><th>Status</th><th>Notification</th></tr>"+(''.join(rows) or "<tr><td colspan='3'>No jobs yet. Save an enabled policy to begin.</td></tr>")+"</table><p><a href='/agent/workflows'>Owner approval queue</a> · <a href='/actions'>Action history</a> · <a href='/docs/autopilot'>Autopilot guide</a></p>"
            return web.page('Autopilot',body,active='/autopilot')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/autopilot/policy')
    def save(handler,req):
        engine=service(handler)
        def apply(ledger):
            allowed={'csrf','revision','enabled','interval_seconds','digest_seconds','strategy','snapshot','notify','confirmed'}|{'cap_'+a.currency for a in ledger.accounts.values()}
            if req.files or req.query or set(req.form)-allowed or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current Autopilot policy form.')
            if req.field('confirmed')!='yes':raise CashflowError('Confirm the exact automatic preparation and notification policy.')
            for field in ['enabled','snapshot','notify']:
                if req.field(field) not in {'','yes'}:raise CashflowError('Use the displayed Autopilot switches.')
            try:interval=int(req.field('interval_seconds'));digest=int(req.field('digest_seconds'))
            except ValueError:raise CashflowError('Choose valid monitoring and summary intervals.') from None
            policy=dict(enabled=req.field('enabled')=='yes',interval_seconds=interval,digest_seconds=digest,strategy=req.field('strategy'),snapshot=req.field('snapshot')=='yes',notify=req.field('notify')=='yes',caps={c:amount(req.field('cap_'+c),c) for c in sorted({a.currency for a in ledger.accounts.values()})})
            engine.save(web.database_path(),ledger,policy,ctx=web._owner_ctx(ledger),expected=req.field('revision'))
        web.read_state(apply);return handler._redirect('/autopilot')

    @web.route('POST','/autopilot/check')
    def check(handler,req):
        if req.files or req.query or set(req.form)!={'csrf'} or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current Check now button.')
        engine=service(handler);engine.tick(engine.identity(web.database_path()),force=True)
        return handler._redirect('/autopilot')

    @web.route('GET','/autopilot/jobs/<id>')
    def detail(handler,req,id):
        engine=service(handler)
        def build(ledger):
            job=next((j for j in engine.history(web.database_path(),ledger,ctx=web._owner_ctx(ledger)) if j['id']==id),None)
            if not job:raise CashflowError('No Autopilot job exists in this active workspace.')
            result=json.loads(job['result']);rows=[]
            for currency,v in result.get('currencies',{}).items():
                rows.append('<tr><td>'+web.esc(currency)+'</td><td>'+web.esc(format_amount(v['minimum_minor'],currency))+'</td><td>'+web.esc(format_amount(v['shortfall_minor'],currency))+'</td><td>'+web.esc(v['first_breach'] or 'None under assumptions')+'</td></tr>')
            body='<h1>Autopilot run</h1><p>Status: '+web.esc(job['status'])+' · '+web.esc(job['kind'])+'</p><p>Local deterministic calculations; no model call. These projections depend on recorded source facts and are not observed cash or source certification.</p><table><tr><th>Currency</th><th>Minimum</th><th>Gap to floor</th><th>First breach</th></tr>'+''.join(rows)+'</table>'
            if result.get('no_feasible_plan'):body+='<p><b>No sufficient plan found within the three bounded strategies.</b> Missing information, consent or additional funding was not invented.</p>'
            for note in result.get('notes',[]):body+='<p>'+web.esc(note)+'</p>'
            body+='<ul>'+''.join("<li><a href='/actions/"+web.esc(i)+"'>DRAFT "+web.esc(i)+"</a></li>" for i in result.get('draft_ids',[]))+''.join("<li><a href='/agent/workflows/"+web.esc(i)+"'>Pending snapshot "+web.esc(i)+"</a></li>" for i in result.get('workflow_ids',[]))+'</ul>'
            body+='<details><summary>Recorded inputs, source references and exact result</summary><pre class="dump">'+web.esc(json.dumps(result,indent=2))+'</pre></details><a href="/autopilot">Back to Autopilot</a>'
            return web.page('Autopilot run',body,active='/autopilot')
        return handler._send_html(200,web.read_state(build))
