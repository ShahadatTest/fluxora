"""Mobile funding/weekly views and an offline editable one-page decision brief."""
import copy,json
from urllib.parse import urlencode

from .errors import CashflowError
from .owner_workflows import review_revision
from .planning import _encode
from .funding import account_funding,assign_forecast_route,weekly_cashflow
from .brief import decision_brief
from .reports import csv_report,Money


def mount(web):
    esc,money=web.esc,web.money
    links="<p><a href='/entries'>Owner entries</a> · <a href='/account-forecast'>Account funding</a> · <a href='/weekly-cashflow'>13-week cash view</a> · <a href='/decision-brief'>Decision Brief</a></p>"
    # Owner-entry navigation is composed before the shared workspace layout.
    web.CSS+='\n.funding{overflow-wrap:anywhere}.funding select{max-width:100%}.funding .kv b{white-space:normal}.funding label{display:block;margin:10px 0}.funding label>input:not([type=checkbox]),.funding label>select,.funding textarea{display:block;box-sizing:border-box;width:100%}.funding .table-wrap{max-width:100%;overflow-x:auto}.funding .table-wrap table{min-width:600px}.funding td,.funding th{white-space:normal}\n'
    def page(title,body):return web.page(title,"<div class='funding'><h1>"+esc(title)+'</h1>'+links+body+'</div>',active='/entries')
    def query(req,name,default=''):
        values=req.query.get(name,[default])
        if len(values)!=1:raise CashflowError('Use one value per view option.')
        return values[0]
    def hidden(name,value):return f"<input type='hidden' name='{name}' value='{esc(value)}'>"
    def options(rows):return ''.join(f"<option value='{esc(v)}'>{esc(label)}</option>" for v,label in rows)
    def fields(req):
        allowed={'csrf','confirmed','revision','kind','item_id','account_id','evidence_id','note'}
        if set(req.form)-allowed or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Unexpected or repeated future-account field.')
    def parse(l,req):
        fields(req)
        return assign_forecast_route(l,req.field('kind'),req.field('item_id'),req.field('account_id'),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(l))
    def download(handler,data,mime,name):
        handler.send_response(200);handler.send_header('Content-Type',mime);handler.send_header('Content-Length',str(len(data)))
        handler.send_header('Content-Disposition','attachment; filename="'+name+'"');handler.end_headers();handler.wfile.write(data)
    def table(ledger,title,headers,rows,note):
        return dict(business_id=ledger.business_id,business_name=ledger.business_name,as_of=ledger.as_of,
                    tables=[dict(name=title,headers=headers,rows=rows,note=note)],warnings=[],note=note)

    @web.route('GET','/account-forecast')
    def accounts(handler,req):
        def build(l):
            r=account_funding(l,ctx=web._owner_ctx(l));body='<p>'+esc(r['note'])+"</p><p><a href='/account-forecast/csv'>Download account daily CSV</a> · <a href='/cash-accounts'>Observed account cash / own transfers</a></p>"
            if not r['complete']:body+=f"<p class='err'>INCOMPLETE: {len(r['unassigned'])} timed records need account review; {len(r['unknown_timing'])} collections have unknown/past timing. An account curve cannot certify funding while these are unresolved.</p>"
            for row in r['accounts']:
                c=row['forecast'];ccy=row['currency']
                body+=f"<section class='card'><h2>{esc(row['name'])} · {esc(row['account_id'])}</h2><p>{esc(row['status'].replace('_',' '))} · {esc(ccy)} · {r['horizon_days']} days from {r['as_of']}.</p><div class='kv stack'><span>Minimum available balance / date</span><b>{money(c.min_balance_minor,ccy)} / {c.min_balance_date}</b></div><div class='kv stack'><span>Floor / worst shortfall</span><b>{money(c.floor_minor,ccy)} / {money(c.worst_shortfall_minor,ccy)}</b></div><p>First floor breach: {c.first_breach_date or 'none among assigned flows'}; closing {money(c.closing_minor,ccy)}. Restricted opening funds {money(row['restricted_minor'],ccy)}.</p>"
                body+=web.evidence_link(l,row['evidence_id'])+web.forecast_svg(c)+"<details><summary>Daily assigned-account data</summary><div class='table-wrap'>"+web.forecast_table(c)+'</div></details></section>'
            body+="<section class='card'><h2>Review future account assumptions</h2><p>These routes affect this account projection only. They do not assign observed cash, approve a bill, release payroll, promise collection or execute a transfer. Source changes require fresh review.</p>"
            for row in r['routes']:
                kind,ident=row['kind'],row['item_id']
                body+=f"<p>{esc(kind)} {esc(ident)} · {esc(row['currency'])} · {esc(row['status'])} → {esc(row['account_id'] or 'UNASSIGNED')}{' · PROTECTED' if row['protected'] else ''}{' · COLLECTION HELD' if row['held'] else ''}. {web.evidence_link(l,row['source_evidence_id'])}</p>"
                if row['held']:continue
                body+="<form method='post' action='/account-forecast/preview'>"+web.csrf_field()+hidden('revision',review_revision(l))+hidden('kind',kind)+hidden('item_id',ident)
                body+="<label>Expected receipt / payment account<select name='account_id' required>"+options((a.id,a.id+' · '+a.currency) for a in l.accounts.values() if a.currency==row['currency'])+"</select></label><label>Checked source evidence<select name='evidence_id' required>"+options((e.id,e.id+' — '+e.summary[:60]) for e in l.evidence.values())+"</select></label><label>Source check / routing assumption<textarea name='note' maxlength='1000' required></textarea></label><button type='submit'>Preview account assumption</button></form>"
            body+='</section>'
            for bridge in r['currency_bridges']:
                c=bridge['currency'];body+=f"<p>{esc(c)} closing bridge: assigned account sum {money(bridge['assigned_account_closing_minor'],c)} + unassigned signed net {money(bridge['unassigned_net_minor'],c)} = pooled forecast {money(bridge['pooled_closing_minor'],c)}. This equality does not resolve account funding.</p>"
            return page('Account-specific future funding',body)
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/account-forecast/preview')
    def preview(handler,req):
        def build(l):
            candidate=copy.deepcopy(l);result=parse(candidate,req)
            body='<p>No saved state changed. Review the exact future-account assumption; no payment or transfer is executed.</p><div class="card">'
            for name,values in req.form.items():
                if name not in {'csrf','confirmed','revision'}:body+=f"<p>{esc(name)}: <b>{esc(values[0])}</b></p>"
            body+='</div>'
            for row in account_funding(candidate,ctx=web._owner_ctx(candidate))['accounts']:
                c=row['forecast'];body+=f"<p>{esc(row['account_id'])}: min {money(c.min_balance_minor,row['currency'])}; first breach {c.first_breach_date or 'none among assigned flows'}; {esc(row['status'])}.</p>"
            body+="<form method='post' action='/account-forecast/confirm'>"+web.csrf_field()
            for name,values in req.form.items():
                if name not in {'csrf','confirmed'}:body+=hidden(name,values[0])
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I checked these source-backed account assumptions. This grants no payment, approval or consent.</label><button type='submit'>Confirm future account assumption</button></form>"
            return page('Review future account assumption',body)
        return handler._send_html(200,web.read_state(build))
    @web.route('POST','/account-forecast/confirm')
    def confirm(handler,req):
        if req.field('confirmed')!='yes':raise CashflowError('Preview and explicitly confirm the routing assumption.')
        web.mutate_state(lambda l:parse(l,req));return handler._redirect('/account-forecast')

    @web.route('GET','/account-forecast/csv')
    def account_csv(handler,req):
        def build(l):
            r=account_funding(l,ctx=web._owner_ctx(l));rows=[]
            for a in r['accounts']:
                c=a['currency']
                for d in a['forecast'].days:rows.append([a['account_id'],c,a['status'],d.date,Money(d.opening_minor,c),Money(d.inflow_minor,c),Money(d.outflow_minor,c),Money(d.intraday_low_minor,c),Money(d.closing_minor,c),d.floor_breached])
            return csv_report(table(l,'Account funding',['Account ID','Currency','Review status','Date','Available opening','Inflow','Outflow','Intraday low','Available closing','Floor breach'],rows,r['note']),'Account funding')
        return download(handler,web.read_state(build),'text/csv; charset=utf-8','account-funding.csv')

    @web.route('GET','/weekly-cashflow')
    def weekly(handler,req):
        def build(l):
            r=weekly_cashflow(l,ctx=web._owner_ctx(l));body='<p>'+esc(r['note'])+"</p><p><a href='/weekly-cashflow/csv'>Download weekly CSV</a> · <a href='/calendar'>Existing daily cash calendar</a></p>"
            if r['clarifications']:body+=f"<p class='warn'>{len(r['clarifications'])} source/collection clarifications remain. See recorded assumptions below.</p>"
            for ccy,data in r['currencies'].items():
                body+=f"<h2>{esc(ccy)} — separate currency</h2><p>Book opening {money(data['book_opening_minor'],ccy)} minus active restrictions {money(data['restricted_minor'],ccy)}. No restriction release assumed.</p>"
                for w in data['weeks']:
                    body+=f"<section class='card'><h3>Week {w['week']} · {w['start']} to {w['end']}</h3><div class='kv stack'><span>Available opening → closing</span><b>{money(w['opening_minor'],ccy)} → {money(w['closing_minor'],ccy)}</b></div><p>Inflow {money(w['inflow_minor'],ccy)}; outflow {money(w['outflow_minor'],ccy)}. Minimum {money(w['min_balance_minor'],ccy)} on {w['min_date']}; worst floor shortfall {money(w['shortfall_minor'],ccy)}.</p>"
                    body+=f"<p class='{'err' if w['first_breach_date'] else 'mut'}'>First intraday breach: {w['first_breach_date'] or 'none in this week'}.</p><details><summary>Source-linked cash drivers ({len(w['drivers'])})</summary>"
                    for driver in w['drivers']:
                        body+=f"<p>{driver['date']} · {esc(driver['kind'])} {esc(driver['id'])} · {money(driver['signed_minor'],ccy)} · {esc(driver['basis'])}. "+' '.join(web.evidence_link(l,e) for e in driver['evidence_ids'])+'</p>'
                    body+='</details><details><summary>Daily risk inside this week</summary><div class="table-wrap"><table><thead><tr><th>Date</th><th>Available closing</th><th>Intraday low</th><th>Floor breach</th></tr></thead><tbody>'
                    for d in w['days']:body+=f"<tr><td>{d.date}</td><td>{money(d.closing_minor,ccy)}</td><td>{money(d.intraday_low_minor,ccy)}</td><td>{'yes' if d.floor_breached else 'no'}</td></tr>"
                    body+='</tbody></table></div></details></section>'
            body+='<details><summary>Recorded assumptions / missing facts</summary>'+''.join('<p>'+esc(a)+'</p>' for a in r['assumptions'])+''.join('<p>'+esc(c['question'])+'</p>' for c in r['clarifications'])+'</details>'
            return page('13-week cash-flow review',body)
        return handler._send_html(200,web.read_state(build))
    @web.route('GET','/weekly-cashflow/csv')
    def weekly_csv(handler,req):
        def build(l):
            r=weekly_cashflow(l,ctx=web._owner_ctx(l));rows=[]
            for c,data in r['currencies'].items():
                for w in data['weeks']:rows.append([c,w['week'],w['start'],w['end'],Money(w['opening_minor'],c),Money(w['inflow_minor'],c),Money(w['outflow_minor'],c),Money(w['closing_minor'],c),Money(w['min_balance_minor'],c),w['min_date'],Money(w['shortfall_minor'],c),w['first_breach_date'] or '',', '.join(sorted({e for d in w['drivers'] for e in d['evidence_ids']}))])
            return csv_report(table(l,'Weekly cash',['Currency','Week','Start','End','Available opening','Inflow','Outflow','Available closing','Intraday minimum','Minimum date','Floor shortfall','First breach','Evidence IDs'],rows,r['note']),'Weekly cash')
        return download(handler,web.read_state(build),'text/csv; charset=utf-8','13-week-cashflow.csv')

    def brief_html(r,standalone=False):
        c=r['currency'];b=r['balances'];f=r['account_funding'];scope=urlencode(dict(currency=c))
        styles=""".decision-paper{background:white;color:#17212b;padding:18px;max-width:760px;margin:12px auto;overflow-wrap:anywhere;font:13px/1.35 Arial,sans-serif}.decision-paper h1{font-size:22px;margin:0 0 5px}.decision-paper h2{font-size:15px;margin:12px 0 5px}.decision-paper p{margin:5px 0}.decision-paper table{width:100%;border-collapse:collapse;table-layout:fixed;min-width:0}.decision-paper td,.decision-paper th{padding:5px;border-bottom:1px solid #ddd;text-align:left;white-space:normal;overflow-wrap:anywhere}.decision-paper .numbers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.decision-paper small{font-size:10px}.decision-paper a{color:#174b84}@page{size:A4;margin:12mm}@media print{body{margin:0;background:white}.decision-paper{width:auto;max-width:none;margin:0;padding:0;font-size:10pt}.decision-paper h1{font-size:17pt}.decision-paper h2{font-size:11pt;margin-top:8px}.no-print{display:none!important}}"""
        body="<article class='decision-paper'><h1>Cashflow Decision Brief</h1><p><b>"+esc(r['business_name'][:100])+f"</b> · {esc(r['business_id'][:64])}</p><p>SYNTHETIC · deterministic read-only snapshot · {r['as_of']} · {esc(c)} only · {r['horizon_days']} days</p>"
        body+='<div class="numbers">'
        for label,field in [('Book cash','book_minor'),('Restricted cash','restricted_minor'),('Available now','available_minor'),('Cash floor','floor_minor'),('Forecast available minimum','forecast_min_minor'),('Worst floor shortfall','worst_shortfall_minor')]:body+=f"<div>{label}: <b>{money(b[field],c)}</b></div>"
        body+=f"</div><p>First pooled floor breach: <b>{b['first_breach_date'] or 'none in this window'}</b>. Forecast closing: {money(b['forecast_closing_minor'],c)}.</p>"
        body+='<h2>Account funding / protected obligations</h2>'
        gaps=sum(a['first_breach_date'] is not None for a in f['accounts'])
        body+=f"<p>{len(f['accounts'])} accounts; {gaps} show a shortfall among assigned flows; {len(f['unassigned'])} unassigned/stale timed routes; {len(f['unknown_timing'])} collections need timing review. <b>{'No account shortfall under recorded assumptions.' if f['no_shortfall_under_recorded_assumptions'] else 'Account funding is incomplete or has a shortfall; pooled cash is insufficient evidence.'}</b></p>"
        protected=r['protected_obligations'];soon=min((str(o['date']) for o in protected),default='none')
        body+=f"<p>{len(protected)} unpaid protected bills, including any recorded payroll/loan obligations; earliest due {soon}. Protected dates stay unchanged. Restrictions are deducted once; payroll stays a separate scheduled outflow.</p>"
        body+='<h2>Compared options - all-currency arithmetic, conditional</h2><table><thead><tr><th>Option</th><th>'+esc(c)+' min / shortfall</th><th>Conclusion</th></tr></thead><tbody>'
        for p in r['options']:
            body+=f"<tr><td>{esc(p['name'])}</td><td>{money(p['currency_min_minor'],c) if p['currency_min_minor'] is not None else 'unavailable'} / {money(p['currency_shortfall_minor'],c) if p['currency_shortfall_minor'] is not None else 'unavailable'}</td><td>{'Conditional arithmetic only' if p['global_financially_sufficient'] else 'Insufficient / blocked'}; {len(p['pending_permissions'])} permission/payment conditions</td></tr>"
        body+='</tbody></table><p><b>'+('No feasible plan found by these three bounded strategies.' if r['no_feasible_plan_found'] else 'Arithmetic sufficiency is conditional; account funding, owner approval, acceptance and observed payment remain separate.')+'</b></p>'
        body+='<h2>Owner / counterparty / observed / reconciled state</h2><p>'+esc('; '.join(k.replace('_',' ')+': '+str(v) for k,v in sorted(r['action_states'].items())) or 'No scoped actions recorded.')+f" Recorded business cash entries (own transfers excluded): {r['observed_cash_record_count']}; future-dated entries: {r['future_recorded_cash_count']}.</p>"
        body+='<h2>Missing facts and next review</h2>'
        problems=r['input_problems'];body+=f"<p>{len(problems)} input/source issues; {len(r['clarifications'])} forecast clarifications. "
        body+=esc(' '.join(p[:140] for p in problems[:2])) if problems else 'Review evidence, account assignments and conditional collection dates before any owner action.'
        body+='</p><p>Expected receipts are assumptions. A draft is not approval, acceptance, observed payment or reconciliation. No message/payment is executed by this brief.</p>'
        sources=r['sources'];body+=f"<small>Snapshot {r['snapshot_id'][:16]} · generated {esc(r['generated_at'])}. Sources: "+esc(', '.join(s['id'][:50] for s in sources[:5]))+f" ({len(sources)} total). Compact one-page summary; full sources, assumptions, options and omitted issue details are in the <a href='/decision-brief/data.json?{scope}'>full snapshot JSON</a>. "+esc(r['scope_note'])+' No bank/source truth or model prose is certified.</small></article>'
        if standalone:
            body=body.replace(f"/decision-brief/data.json?{scope}",'#full-snapshot')
            body+='<details id="full-snapshot" class="no-print"><summary>Full dated snapshot JSON included in this editable file (excluded from the printed summary)</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'+esc(json.dumps(r,default=_encode,indent=2))+'</pre></details>'
            return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cashflow Decision Brief</title><style>'+styles+'</style><body>'+body+'</body></html>'
        return '<style>'+styles+'</style>'+body

    @web.route('GET','/decision-brief')
    def brief_page(handler,req):
        def build(l):
            c=query(req,'currency',l.base_currency);r=decision_brief(l,c,ctx=web._owner_ctx(l));scope=urlencode(dict(currency=c))
            body="<form method='get' action='/decision-brief'><label>One-page currency scope<select name='currency'>"+''.join(f"<option value='{esc(ccy)}' {'selected' if ccy==c else ''}>{esc(ccy)}</option>" for ccy in dict.fromkeys(a.currency for a in l.accounts.values()))+"</select></label><button type='submit'>Review currency brief</button></form>"
            body+=f"<p><a href='/decision-brief/print?{scope}'>Open print view</a> · <a href='/decision-brief/download.html?{scope}'>Download editable HTML</a> · <a href='/decision-brief/data.json?{scope}'>Download full snapshot JSON</a> · <a href='/plans'>Full plan comparison</a></p>"+brief_html(r)
            # The page already has one h1; the embedded paper uses h2 in-app.
            return page('Decision Brief',body.replace('<h1>Cashflow Decision Brief</h1>','<h2>Cashflow Decision Brief</h2>'))
        return handler._send_html(200,web.read_state(build))
    @web.route('GET','/decision-brief/print')
    def print_brief(handler,req):
        return handler._send_html(200,web.read_state(lambda l:brief_html(decision_brief(l,query(req,'currency',l.base_currency),ctx=web._owner_ctx(l)),True)))
    @web.route('GET','/decision-brief/download.html')
    def download_brief(handler,req):
        data=web.read_state(lambda l:brief_html(decision_brief(l,query(req,'currency',l.base_currency),ctx=web._owner_ctx(l)),True).encode('utf-8'))
        return download(handler,data,'text/html; charset=utf-8','cashflow-decision-brief.html')
    @web.route('GET','/decision-brief/data.json')
    def brief_json(handler,req):
        data=web.read_state(lambda l:json.dumps(decision_brief(l,query(req,'currency',l.base_currency),ctx=web._owner_ctx(l)),default=_encode,indent=2).encode('utf-8'))
        return download(handler,data,'application/json; charset=utf-8','cashflow-decision-snapshot.json')
