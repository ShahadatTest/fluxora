"""English owner forms and read-only exploration; financial logic stays in engines."""
import copy
import csv
from datetime import date
from io import StringIO
import re
from urllib.parse import quote, urlencode

from .errors import CashflowError
from .extension import (record_refund, correct_refund, correct_payment_date, refund_entries,
    effective_refund, create_instalments, advance_instalments, movement_date)
from .exploration import compound_scenario, save_scenario, cash_calendar, search_records, SEARCH_KINDS
from .owner_workflows import review_revision
from .payment_review import observed_movements, payment_warnings
from .planning import _revision
from .settlement import bill_schedule


def mount(web):
    esc,money=web.esc,web.money
    links="<p><a href='/refunds'>Refund settlements</a> · <a href='/payment-dates'>Payment dates</a> · <a href='/search'>Search</a> · <a href='/instalments'>Supplier instalments</a> · <a href='/scenarios'>Combined scenarios</a> · <a href='/calendar'>Cash calendar</a></p>"
    titles={'refund':'observed refund settlement','refund_correct':'refund amount correction',
        'date_correct':'observed payment-date correction','instalment_create':'supplier instalment proposal',
        'instalment_step':'supplier agreement step','scenario_save':'saved conditional scenario'}

    def arg(req,name,default=''):return req.query.get(name,[default])[0]

    def integer(value,label):
        if not re.fullmatch(r'[0-9]{1,6}',value):raise CashflowError(label+' must be a whole number.')
        return int(value)

    def day(value,optional=False):
        if optional and not value:return None
        try:return date.fromisoformat(value)
        except (ValueError,TypeError):raise CashflowError('Use a calendar date in YYYY-MM-DD format.')

    def amount(value,ccy):
        if not re.fullmatch(r'[0-9]{1,13}(\.[0-9]{1,3})?',value):raise CashflowError('Use an ordinary nonnegative money amount in the record currency.')
        return web._parse_major_to_minor(value,ccy)

    def raw(minor,ccy):return web.format_amount(minor,ccy).split(' ',1)[1].replace(',','')

    def fields(req,allowed):
        if set(req.form)-set(allowed)-{'csrf','confirmed','acknowledge_duplicates'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Unexpected or repeated form field. Reload the current owner form.')

    def hidden(ledger,op):
        return web.csrf_field()+f"<input type='hidden' name='operation' value='{op}'><input type='hidden' name='revision' value='{review_revision(ledger)}'>"

    def source(ledger):
        opts='<option value="">Choose checked source</option>'+''.join(f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:65])}</option>" for e in ledger.evidence.values())
        return f"<label>Checked source<select name='evidence_id' required>{opts}</select></label><label>What did you check?<textarea name='note' maxlength='1000' required></textarea></label>"

    def form(ledger,op):return "<form method='post' action='/extensions/preview'>"+hidden(ledger,op)
    def submit(label='Preview reviewed operation'):return f"<button type='submit'>{label}</button></form>"

    def lines(value,width,label,limit):
        if len(value)>20000:raise CashflowError(label+' input exceeds 20,000 characters.')
        try:rows=[r for r in csv.reader(StringIO(value),strict=True) if any(c.strip() for c in r)]
        except csv.Error:raise CashflowError('Use valid CSV rows for '+label+'.')
        if len(rows)>limit or any(len(r)!=width for r in rows):raise CashflowError(f'{label}: use at most {limit} rows with exactly {width} columns.')
        return [[v.strip() for v in r] for r in rows]

    def scenario_inputs(req):
        delays=[dict(invoice_id=r[0],delay_days=integer(r[1],'Delay days')) for r in lines(req.field('delays'),2,'Invoice delays',20)]
        expenses=[dict(label=r[0],currency=r[1],amount_minor=amount(r[2],r[1]),date=r[3]) for r in lines(req.field('expenses'),4,'Hypothetical expenses',20)]
        collections=[dict(invoice_id=r[0],percent=integer(r[1],'Collection percent')) for r in lines(req.field('collections'),2,'Collection assumptions',20)]
        return delays,expenses,collections

    def parse(ledger,req,preview=False):
        op=req.field('operation');base={'operation','revision','evidence_id','note'}
        ev,note,revision=req.field('evidence_id'),req.field('note'),req.field('revision');ctx=web._owner_ctx(ledger)
        if op=='refund':
            fields(req,base|{'kind','item_id','refund_id','amount','date'})
            group=ledger.receivables if req.field('kind')=='invoice' else ledger.outflows if req.field('kind')=='bill' else {}
            item=group.get(req.field('item_id'))
            if item is None:raise CashflowError('Choose an invoice credit or supplier overpayment.')
            result=record_refund(ledger,req.field('kind'),item.id,req.field('refund_id'),amount(req.field('amount'),item.currency),day(req.field('date')),
                ev,note,revision,ctx=ctx,acknowledge_duplicates=True if preview else req.field('acknowledge_duplicates')=='yes')
            return result,'/refunds'
        if op=='refund_correct':
            fields(req,base|{'refund_id','amount','date'})
            item=next((i for _,i,r in refund_entries(ledger) if r['id']==req.field('refund_id')),None)
            if item is None:raise CashflowError('Choose a recorded refund.')
            return correct_refund(ledger,req.field('refund_id'),amount(req.field('amount'),item.currency),day(req.field('date')),ev,note,revision,ctx=ctx),'/refunds'
        if op=='date_correct':
            fields(req,base|{'movement_key','new_date'})
            return correct_payment_date(ledger,req.field('movement_key'),day(req.field('new_date')),ev,note,revision,ctx=ctx),'/payment-dates'
        if op=='instalment_create':
            fields(req,base|{'proposal_id','bill_id','schedule'})
            out=ledger.outflows.get(req.field('bill_id'))
            if out is None:raise CashflowError('Choose a supplier bill.')
            schedule=[dict(date=r[0],amount_minor=amount(r[1],out.currency)) for r in lines(req.field('schedule'),2,'Instalments',12)]
            return create_instalments(ledger,req.field('proposal_id'),out.id,schedule,ev,note,revision,ctx=ctx),'/instalments'
        if op=='instalment_step':
            fields(req,base|{'proposal_id','step'})
            return advance_instalments(ledger,req.field('proposal_id'),req.field('step'),ev,note,revision,ctx=ctx),'/instalments'
        if op=='scenario_save':
            fields(req,{'operation','revision','label','delays','expenses','collections'})
            delays,expenses,collections=scenario_inputs(req)
            return save_scenario(ledger,req.field('label'),delays,expenses,revision,ctx=ctx,collections=collections),'/scenarios'
        raise CashflowError('Choose an operation from the current owner form.')

    @web.route('POST','/extensions/preview')
    def preview(handler,req):
        def build(ledger):
            work=copy.deepcopy(ledger);result,_=parse(work,req,True);op=req.field('operation')
            body=f"<h1>Review {esc(titles[op])}</h1><p>This preview changed no saved record. Original observations and approval histories remain traceable.</p>"+links
            body+="<div class='card'><h2>Exact proposed inputs</h2>"
            for name,values in req.form.items():
                if name not in {'csrf','revision','operation','confirmed','acknowledge_duplicates'}:
                    value=({'approve':'Owner approval','accept':'Record external acceptance','apply':'Apply accepted unpaid schedule','cancel':'Cancel proposal'}.get(values[0],values[0]) if name=='step' else values[0])
                    body+=f"<div class='kv stack'><span>{esc(name.replace('_',' '))}</span><b style='white-space:pre-wrap'>{esc(value)}</b></div>"
            body+='</div>'
            if op=='instalment_step':
                proposal=work.instalment_proposals[req.field('proposal_id')]
                body+=f"<div class='card'><h2>Exact supplier terms: {esc(proposal['bill_id'])}</h2><p>State after confirmation: {esc(proposal['state'].replace('_',' '))}. Currency {esc(proposal['currency'])}.</p>"
                for r in proposal['schedule']:body+=f"<p>{esc(r['date'])}: {money(r['amount_minor'],proposal['currency'])}</p>"
                body+='<p>Acceptance records an explicit owner assertion about external synthetic acceptance of these terms. Uploaded documents alone grant no consent. Applying changes only the unpaid schedule; no payment/message is sent.</p></div>'
            body+="<div class='card'><h2>Recorded cash effect</h2>"
            dates={ledger.as_of}
            if req.field('date'):dates.add(day(req.field('date')))
            if 'old_date' in result:dates|={day(result['old_date']),day(result['new_date'])}
            for ccy in {a.currency for a in ledger.accounts.values()}:
                for d in sorted(dates):
                    body+=f"<div class='kv stack'><span>{esc(ccy)} through {d}: before → after</span><b>{money(ledger.actual_cash(ccy,d),ccy)} → {money(work.actual_cash(ccy,d),ccy)}</b></div>"
            if 'remaining_credit_minor' in result:body+='<p>Remaining source credit after confirmation: '+str(result['remaining_credit_minor'])+' minor units in the selected record currency.</p>'
            body+='</div>'
            warnings=result.get('possible_duplicate_ids',[])
            if op=='date_correct':
                pairs=[p for p in payment_warnings(work,ctx=web._owner_ctx(work)) if p['status'] in {'review_required','possible_duplicate'}]
                if pairs:body+="<div class='card'><h2>Payment warnings after date correction</h2>"+''.join(f"<p>{esc(p['left']['key'])} / {esc(p['right']['key'])}</p>" for p in pairs)+"<p>Review these possible duplicate movements before relying on plans.</p></div>"
            if warnings:body+="<div class='card'><h2>Possible duplicate movements</h2>"+''.join('<p>'+esc(w)+'</p>' for w in warnings)+'</div>'
            body+="<div class='card'><form method='post' action='/extensions/confirm'>"+web.csrf_field()
            for name,values in req.form.items():
                if name not in {'csrf','confirmed','acknowledge_duplicates'}:body+=f"<input type='hidden' name='{esc(name)}' value='{esc(values[0])}'>"
            if warnings:body+="<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I checked the sources and these are distinct observed movements.</label>"
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I reviewed these exact terms/source facts and explicitly confirm this local owner operation. No money or message is sent.</label>"+submit('Confirm exact reviewed operation')+'</div>'
            return web.page('Review owner operation',body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/extensions/confirm')
    def confirm(handler,req):
        if req.field('confirmed')!='yes':raise CashflowError('Review and explicitly confirm the exact owner operation.')
        _,destination=web.mutate_state(lambda ledger:parse(ledger,req))
        return handler._redirect(destination)

    @web.route('GET','/refunds')
    def refunds(handler,req):
        def build(ledger):
            body='<h1>Refund &amp; credit settlement</h1><p>Record a refund only after checking an actual synthetic payment source. Customer refunds reduce cash and net collected allocations; supplier refunds increase cash and reduce net paid. No refund is executed and no expected refund cash is invented. This workflow settles excess credits only; credit transfers, write-offs and contractual waivers are outside it.</p>'+links
            count=0
            for kind,group in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
                for item in group.values():
                    credit=item.credit_balance_minor if kind=='invoice' else item.overpaid_minor
                    if not credit and not item.refunds:continue
                    count+=1
                    body+=f"<div class='card'><h2>{esc(kind)} {esc(item.id)}</h2><p>Remaining {'customer credit' if kind=='invoice' else 'supplier overpayment'}: {money(credit,item.currency)}. {web.evidence_link(ledger,item.evidence_id)}.</p>"
                    if credit:
                        body+=form(ledger,'refund')+f"<input type='hidden' name='kind' value='{kind}'><input type='hidden' name='item_id' value='{esc(item.id)}'><label>Unique refund observation ID<input name='refund_id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>Observed refund amount — {esc(item.currency)}<input name='amount' inputmode='decimal' required></label><label>Observed refund date<input name='date' type='date' value='{ledger.as_of}' required></label>"+source(ledger)+submit('Preview observed refund')
                    for entry in item.refunds:
                        body+=f"<div id='refund-{esc(entry['id'])}'><h3>{esc(entry['id'])}</h3><p>Original {money(entry['amount_minor'],item.currency)} on {esc(entry['date'])}; current {money(effective_refund(entry),item.currency)}, effective observation date {movement_date(ledger,'refund:'+entry['id'],entry['date'])}. {web.evidence_link(ledger,entry['evidence_id'])}.</p>"
                        body+=form(ledger,'refund_correct')+f"<input type='hidden' name='refund_id' value='{esc(entry['id'])}'><label>Corrected refund amount — {esc(item.currency)} (zero reverses a mistaken observation)<input name='amount' value='{raw(effective_refund(entry),item.currency)}' inputmode='decimal' required></label><label>Accounting correction date<input name='date' type='date' value='{ledger.as_of}' required></label>"+source(ledger)+submit('Preview refund correction')
                        for c in entry['adjustments']:body+=f"<p>Correction {esc(c['date'])}: {money(c['old_amount_minor'],item.currency)} → {money(c['new_amount_minor'],item.currency)}; {web.evidence_link(ledger,c['evidence_id'])}. {esc(c['note'])}</p>"
                        body+='</div>'
                    body+='</div>'
            if not count:body+="<div class='card'><p>No customer credit or supplier overpayment to settle. Review source amounts on <a href='/invoice-adjustments'>invoice adjustments</a> or <a href='/bill-adjustments'>bill adjustments</a>; an adjustment by itself creates no refund.</p></div>"
            return web.page('Refund settlements',body,active='/refunds')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/payment-dates')
    def payment_dates(handler,req):
        def build(ledger):
            body='<h1>Payment-date corrections</h1><p>Correct an observed whole-payment date inside the open snapshot. The original date stays in history. This moves the original cash event only; amount corrections and split allocation dates keep their dates. Dates cannot cross a closed opening or move after dependent corrections/allocations/refunds. Amount, currency and action state stay unchanged. Old statement matches/source checks may need review.</p>'+links
            rows=[r for r in observed_movements(ledger) if not r['kind'].startswith('internal_transfer')]
            for row in rows:
                body+=f"<div class='card' id='movement-{esc(row['key'])}'><h2>{esc(row['key'])}</h2><p>{money(row['signed_minor'],row['currency'])}, current observed date {esc(row['date'])}; target {esc(row['target'])}; {web.evidence_link(ledger,row['evidence_id'])}.</p>"
                if day(row['date'])>=ledger.as_of:
                    body+=form(ledger,'date_correct')+f"<input type='hidden' name='movement_key' value='{esc(row['key'])}'><label>Correct observed date<input name='new_date' type='date' value='{esc(row['date'])}' min='{ledger.as_of}' required></label>"+source(ledger)+submit('Preview date correction')
                else:body+='<p>Already absorbed in the opening snapshot: date restatement is blocked.</p>'
                for c in ledger.date_corrections.values():
                    if c['movement_key']==row['key']:body+=f"<p>History {esc(c['old_date'])} → {esc(c['new_date'])}; {web.evidence_link(ledger,c['evidence_id'])}; {esc(c['note'])}.</p>"
                body+='</div>'
            if not rows:body+='<p>No nonzero observed movements yet.</p>'
            return web.page('Payment dates',body,active='/payment-dates')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/instalments')
    def instalments(handler,req):
        def build(ledger):
            body='<h1>Supplier instalment agreements</h1><p>Propose a schedule for the exact remaining supplier payable, including partially paid or source-adjusted bills. Draft, owner approval, recorded counterparty acceptance and applied schedule are distinct. Uploads never grant consent. No message or payment is sent. Protected payroll is excluded.</p>'+links
            for out in ledger.outflows.values():
                if out.protected or not out.counterparty_consent_required_for_change or not out.remaining_minor:continue
                body+=f"<div class='card'><h2>{esc(out.id)}</h2><p>Net paid {money(out.paid_minor,out.currency)}; remaining {money(out.remaining_minor,out.currency)}. Current unpaid schedule:</p>"
                for d,m in bill_schedule(out):body+=f'<p>{d}: {money(m,out.currency)}</p>'
                body+=form(ledger,'instalment_create')+f"<input type='hidden' name='bill_id' value='{esc(out.id)}'><label>Unique proposal ID<input name='proposal_id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>New remaining schedule — one date,amount row per line, amounts in {esc(out.currency)}<textarea name='schedule' rows='4' placeholder='2027-01-15,100.00&#10;2027-01-22,200.00' required></textarea></label>"+source(ledger)+submit('Preview draft agreement')+'</div>'
            for proposal in ledger.instalment_proposals.values():
                body+=f"<div class='card' id='agreement-{esc(proposal['id'])}'><h2>{esc(proposal['id'])} · {esc(proposal['state'].replace('_',' '))}</h2><p>Bill {esc(proposal['bill_id'])}; {web.evidence_link(ledger,proposal['evidence_id'])}. {esc(proposal['note'])}</p>"
                for row in proposal['schedule']:body+=f"<p>{esc(row['date'])}: {money(row['amount_minor'],proposal['currency'])}</p>"
                next_step={'draft':('approve','Approve exact proposed terms as owner'), 'owner_approved':('accept','Record observed external counterparty acceptance'), 'counterparty_accepted':('apply','Apply exact accepted unpaid schedule')}.get(proposal['state'])
                steps=([next_step] if next_step else [])+([('cancel','Cancel this proposal')] if proposal['state'] not in {'applied','cancelled'} else [])
                for step,label in steps:
                    body+=form(ledger,'instalment_step')+f"<input type='hidden' name='proposal_id' value='{esc(proposal['id'])}'><input type='hidden' name='step' value='{step}'><h3>{label}</h3>"+source(ledger)+submit('Preview '+label.lower())
                for h in proposal['history']:body+=f"<p>{esc(h['state'].replace('_',' '))} by {esc(h['actor'])}; {web.evidence_link(ledger,h['evidence_id'])}; {esc(h['note'])}.</p>"
                body+='</div>'
            return web.page('Supplier instalments',body,active='/instalments')
        return handler._send_html(200,web.read_state(build))

    def scenario_form(ledger,delays='',expenses='',label='',collections=''):
        invoices='; '.join(f'{r.id}: {r.currency}, remaining {web.format_amount(r.remaining_minor,r.currency)}, expected {r.expected_date or "unknown"}' for r in ledger.receivables.values() if r.remaining_minor)
        return f"<div class='card'><h2>Test several assumptions together</h2><p>Available invoices: {esc(invoices)}</p><p>Forecast window: {ledger.as_of} for {ledger.horizon_days} days. Use one CSV row per line; quote labels containing commas.</p><form method='post' action='/scenarios/evaluate'>{web.csrf_field()}<label>Scenario name (needed only to save)<input name='label' value='{esc(label)}' maxlength='120'></label><label>Invoice delays: invoice_id,whole_delay_days (0–90)<textarea name='delays' rows='3' placeholder='invoice-one,7'>{esc(delays)}</textarea></label><label>Collection stress: invoice_id,whole_percent (0–100 of current unpaid remainder)<textarea name='collections' rows='3' placeholder='invoice-one,50&#10;invoice-two,0'>{esc(collections)}</textarea></label><p class='mut'>Percentages are assumptions, not probabilities. Minor units round down; the excluded remainder stays owed and supplies no later cash in this scenario. Recorded receipts stay intact.</p><label>Additional hypothetical expenses: label,currency,amount,date<textarea name='expenses' rows='3' placeholder='New stock,USD,100.00,2027-01-15'>{esc(expenses)}</textarea></label><button type='submit'>Compare combined scenario</button></form></div>"

    def scenario_result(ledger,result,req):
        body='<div class="card"><h2>Conditional comparison</h2><p><b>'+esc(result['conclusion'])+'</b></p><p>'+esc(result['note'])+'</p>'
        for ccy,curve in result['scenario'].currencies.items():
            base=result['baseline'].currencies[ccy]
            body+=f"<h3>{esc(ccy)} only</h3><div class='kv stack'><span>Baseline minimum → scenario minimum</span><b>{money(base.min_balance_minor,ccy)} → {money(curve.min_balance_minor,ccy)}</b></div><p>First scenario breach: {curve.first_breach_date or 'none in window'}; worst shortfall {money(curve.worst_shortfall_minor,ccy)}. Closing {money(base.closing_minor,ccy)} → {money(curve.closing_minor,ccy)}.</p>"
        for row in result['collection_rows']:
            body+=f"<p>{esc(row['invoice_id'])}: {row['percent']}% of {money(row['remaining_minor'],row['currency'])} = {money(row['assumed_minor'],row['currency'])} assumed; {money(row['excluded_minor'],row['currency'])} excluded from projected cash.</p>"
        for item in result['assumptions']+result['input_problems']:body+='<p>'+esc(item)+'</p>'
        for eid in result['evidence_ids']:body+=web.evidence_link(ledger,eid)+' '
        body+='</div>'
        if req.field('label').strip():
            body+="<div class='card'>"+form(ledger,'scenario_save')
            for name in ['label','delays','expenses','collections']:body+=f"<input type='hidden' name='{name}' value='{esc(req.field(name))}'>"
            body+=submit('Preview saving these assumptions')+'</div>'
        return body

    @web.route('GET','/scenarios')
    def scenarios(handler,req):
        def build(ledger):
            body='<h1>Combined scenario planning</h1><p>Compare invoice delays, partial/no collection and additional expenses together against the current deterministic baseline. These assumptions create no actual entries, approvals or feasible action plan.</p>'+links
            saved_id=arg(req,'saved');delays=expenses=label=collections=''
            if saved_id:
                saved=ledger.scenarios.get(saved_id)
                if saved is None or saved['business_id']!=ledger.business_id:raise CashflowError('Choose a saved scenario in this business.')
                delays='\n'.join(f"{d['invoice_id']},{d['delay_days']}" for d in saved['delays'])
                buf=StringIO();writer=csv.writer(buf,lineterminator='\n')
                for e in saved['expenses']:writer.writerow([e['label'],e['currency'],raw(e['amount_minor'],e['currency']),e['date']])
                expenses=buf.getvalue();label=saved['label']
                collections='\n'.join(f"{c['invoice_id']},{c['percent']}" for c in saved.get('collections',[]))
                body+=f"<p>Loaded assumptions: {esc(label)}. Saved at {esc(saved['as_of'])}; {'financial inputs changed since save' if saved['basis']!=_revision(ledger,'scenario',[]) else 'financial inputs match save'}. Re-evaluate against the current baseline; the old result is not reused.</p>"
            body+=scenario_form(ledger,delays,expenses,label,collections)
            for saved in ledger.scenarios.values():body+=f"<p><a href='/scenarios?saved={quote(saved['id'],safe='')}'>{esc(saved['label'])}</a> — saved assumptions from {esc(saved['as_of'])}.</p>"
            return web.page('Combined scenarios',body,active='/scenarios')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/scenarios/evaluate')
    def evaluate(handler,req):
        fields(req,{'label','delays','expenses','collections'})
        def build(ledger):
            delays,expenses,collections=scenario_inputs(req);result=compound_scenario(ledger,delays,expenses,ctx=web._owner_ctx(ledger),collections=collections)
            return web.page('Scenario comparison','<h1>Combined scenario comparison</h1>'+links+scenario_result(ledger,result,req)+scenario_form(ledger,req.field('delays'),req.field('expenses'),req.field('label'),req.field('collections')),active='/scenarios')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/search')
    def search(handler,req):
        def build(ledger):
            query,kind,ccy,status=arg(req,'q'),arg(req,'kind'),arg(req,'currency'),arg(req,'status')
            result=search_records(ledger,query,kind,ccy,status,day(arg(req,'from'),True),day(arg(req,'to'),True),integer(arg(req,'offset','0'),'Offset'),ctx=web._owner_ctx(ledger))
            def options(values,selected):return '<option value="">All</option>'+''.join(f"<option value='{esc(v)}' {'selected' if v==selected else ''}>{esc(v.replace('_',' '))}</option>" for v in sorted(values))
            body='<h1>Global search &amp; filters</h1>'+links+f"<div class='card'><form method='get' action='/search'><label>IDs, party, reference or evidence text<input name='q' value='{esc(query)}' maxlength='200'></label><label>Record type<select name='kind'>{options(SEARCH_KINDS,kind)}</select></label><label>Currency<select name='currency'>{options({a.currency for a in ledger.accounts.values()},ccy)}</select></label><label>Status<select name='status'>{options(result['statuses'],status)}</select></label><label>From date<input name='from' type='date' value='{esc(arg(req,'from'))}'></label><label>To date<input name='to' type='date' value='{esc(arg(req,'to'))}'></label><button type='submit'>Search records</button></form><p>{esc(result['note'])}</p></div><p>{result['total']} matching records; showing up to 50 from {result['offset']+1}.</p>"
            for row in result['rows']:
                body+=f"<div class='card'><h2><a href='{esc(row['url'])}'>{esc(row['kind'])}: {esc(row['id'])}</a></h2><p>{esc(row['status'].replace('_',' '))} · {esc(row['date'] or 'undated')} · {esc(row['currency'] or 'currency-neutral')}</p><p>{esc(row['description'])}</p>"
                if row['amount_minor'] is not None and row['currency']:body+='<p>'+money(row['amount_minor'],row['currency'])+'</p>'
                for eid in row['evidence_ids']:body+=web.evidence_link(ledger,eid)+' '
                body+='</div>'
            if result['next_offset'] is not None:
                params={k:arg(req,k) for k in ['q','kind','currency','status','from','to']};params['offset']=result['next_offset']
                body+=f"<p><a href='/search?{esc(urlencode(params))}'>Next 50 results</a></p>"
            return web.page('Search',body,active='/search')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/calendar')
    def calendar(handler,req):
        def build(ledger):
            start=day(arg(req,'start'),True) or ledger.as_of;days=integer(arg(req,'days','30'),'Calendar days');ccy=arg(req,'currency')
            result=cash_calendar(ledger,start,days,ccy,ctx=web._owner_ctx(ledger))
            opts='<option value="">All currencies, shown separately</option>'+''.join(f"<option value='{esc(a.currency)}' {'selected' if a.currency==ccy else ''}>{esc(a.currency)}</option>" for a in ledger.accounts.values())
            body='<h1>Cash calendar</h1>'+links+f"<div class='card'><p>{esc(result['note'])}</p><form method='get' action='/calendar'><label>Start date<input name='start' type='date' min='{ledger.as_of}' value='{start}' required></label><label>Days (1–62)<input name='days' type='number' min='1' max='62' value='{days}' required></label><label>Currency<select name='currency'>{opts}</select></label><button type='submit'>Show calendar</button></form></div>"
            if result['promises']:
                body+="<div class='card'><h2>Customer promises — information only, not added cash</h2>"+''.join(f"<p>{esc(p['due_date'])}: <a href='/promises#promise-{quote(p['id'],safe='')}'>{esc(p['id'])}</a>, unfulfilled {money(p['remaining_minor'],p['currency'])}; {esc(p['status'].replace('_',' '))}.</p>" for p in result['promises'])+'</div>'
            for row in result['undated']:body+=f"<div class='card'><b>Needs timing review: {esc(row['id'])}</b><p>{money(row['amount_minor'],row['currency'])}; {esc(row['reason'])}. {web.evidence_link(ledger,row['evidence_id'])}.</p></div>"
            for row in result['rows']:
                currency=row['currency'];body+=f"<div class='card'><h2>{esc(row['date'])} · {esc(currency)}</h2><p class='{'bad' if row['floor_breached'] else 'mut'}'>{'CASH FLOOR BREACH' if row['floor_breached'] else 'Floor holds under recorded assumptions'}</p><div class='kv stack'><span>Forecast opening → closing</span><b>{money(row['opening_minor'],currency)} → {money(row['closing_minor'],currency)}</b></div><p>Conservative intraday low {money(row['intraday_low_minor'],currency)}; floor {money(row['floor_minor'],currency)}.</p>"
                for event in row['events']:
                    label=event['label']
                    if event['state']=='recorded':
                        code,ident=label.split(' ',1)
                        names={'observed_receipt':'Observed receipt','receipt_correction':'Receipt amount correction',
                            'observed_bill_payment':'Observed bill payment','bill_payment_correction':'Bill payment amount correction',
                            'other_cash_observed':'Observed other cash','other_cash_correction':'Other cash amount correction',
                            'refund_observed':'Observed refund','refund_correction':'Refund amount correction',
                            'unallocated_refund_observed':'Observed unallocated-payment refund','unallocated_refund_correction':'Unallocated refund correction'}
                        label=names.get(code,code.replace('_',' '))+' '+ident
                    body+=f"<p><b>{esc(event['state'].upper())}</b> · <a href='{esc(event['url'])}'>{esc(label)}</a> · {money(event['amount_minor'],currency)}<br>"+' '.join(web.evidence_link(ledger,e) for e in sorted(set(event['evidence_ids'])))+'</p>'
                if not row['events']:body+='<p>No dated entries on this day.</p>'
                body+='</div>'
            if result['clarifications']:body+='<div class="card"><h2>Unresolved forecast inputs</h2>'+''.join('<p>'+esc(c['question'])+'</p>' for c in result['clarifications'])+'</div>'
            return web.page('Cash calendar',body,active='/calendar')
        return handler._send_html(200,web.read_state(build))
