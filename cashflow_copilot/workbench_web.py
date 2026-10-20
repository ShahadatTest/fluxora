"""Local English setup, review forms, party statements and opt-in attachments."""
import copy
from datetime import date
import re
import time
from urllib.parse import quote,urlencode

from .errors import CashflowError
from .money import MINOR_DIGITS
from .owner_workflows import review_revision
from .extension import effective_refund
from .settlement import split_unallocated,effective_split_payment,effective_receipt
from .payment_review import observed_movements
from .completion import statement_report
from .workbench import (setup_business,create_promise,review_promise,promise_report,review_net_settlement,
    record_unallocated_refund,correct_unallocated_refund,unallocated_refund_total,party_statement,party_csv,
    validate_attachment,attach_evidence,attachment_bytes,attachment_inventory,retire_attachment)


def mount(web):
    esc,money=web.esc,web.money
    web.CSS+='\n.bench{overflow-wrap:anywhere}.bench select{max-width:100%}.bench code{word-break:break-all}\n'
    links="<p><a href='/setup'>Business setup</a> · <a href='/promises'>Customer promises</a> · <a href='/batch-matching'>Batch / net matching</a> · <a href='/unallocated-refunds'>Unallocated refunds</a> · <a href='/parties'>Party statements</a> · <a href='/attachments'>Evidence attachments</a></p>"
    titles={'setup':'new synthetic business','promise_create':'customer payment promise','promise_review':'promise source review',
        'net_match':'batch / net statement match','unallocated_refund':'observed unallocated-payment refund',
        'unallocated_refund_correct':'unallocated refund correction','attachment_retire':'attachment retirement'}
    def page(title,body,active):return web.page(title,"<div class='bench'>"+body+'</div>',active=active)
    def arg(req,k,default=''):return req.query.get(k,[default])[0]
    def fields(req,allowed,multiple=()):
        if set(req.form)-set(allowed)-{'csrf','confirmed','acknowledge_duplicates','target_business_confirmation'}:
            raise CashflowError('Unexpected form field. Reload the current review form.')
        if any(len(v)!=1 and k not in multiple for k,v in req.form.items()):raise CashflowError('Repeated owner form field; reload the form.')
    def day(value):
        try:return date.fromisoformat(value)
        except (ValueError,TypeError):raise CashflowError('Use an ISO calendar date.')
    def integer(value):
        if not re.fullmatch('[0-9]{1,3}',value):raise CashflowError('Use a whole number.')
        return int(value)
    def amount(value,ccy,signed=False):
        if not re.fullmatch(r'-?[0-9]{1,13}(\.[0-9]{1,3})?' if signed else r'[0-9]{1,13}(\.[0-9]{1,3})?',value):raise CashflowError('Use an ordinary money amount in the selected currency.')
        return web._parse_major_to_minor(value,ccy)
    def raw(value,ccy):return web.format_amount(value,ccy).split(' ',1)[1].replace(',','')
    def hidden(ledger,op):return web.csrf_field()+f"<input type='hidden' name='operation' value='{op}'><input type='hidden' name='revision' value='{review_revision(ledger)}'>"
    def form(ledger,op):return "<form method='post' action='/workbench/preview'>"+hidden(ledger,op)
    def end(label='Preview owner operation'):return f"<button type='submit'>{label}</button></form>"
    def sources(ledger,selected=''):return '<option value="">Choose checked evidence</option>'+''.join(f"<option value='{esc(e.id)}' {'selected' if e.id==selected else ''}>{esc(e.id)} — {esc(e.summary[:60])}</option>" for e in ledger.evidence.values())
    def source(ledger):return f"<label>Checked evidence<select name='evidence_id' required>{sources(ledger)}</select></label><label>Source checks / reason<textarea name='note' maxlength='1000' required></textarea></label>"

    def parse(ledger,req,preview=False):
        op=req.field('operation');base={'operation','revision','evidence_id','note'};ctx=web._owner_ctx(ledger)
        ev,note,rev=req.field('evidence_id'),req.field('note'),req.field('revision')
        if op=='setup':
            allowed={'operation','revision','id','name','base_currency','timezone','as_of','horizon_days','synthetic'}
            allowed|={f'account_{i}_{k}' for i in range(6) for k in ['id','currency','opening','floor','source_note']}
            fields(req,allowed);accounts=[]
            for i in range(6):
                prefix=f'account_{i}_';ident=req.field(prefix+'id');ccy=req.field(prefix+'currency')
                if not ident:
                    if any(req.field(prefix+k) for k in ['currency','opening','floor','source_note']):raise CashflowError('Complete each used account row, or leave all its fields empty.')
                    continue
                accounts.append(dict(id=ident,currency=ccy,opening_minor=amount(req.field(prefix+'opening'),ccy,True),
                    floor_minor=amount(req.field(prefix+'floor'),ccy),source_note=req.field(prefix+'source_note')))
            spec=dict(id=req.field('id'),name=req.field('name'),base_currency=req.field('base_currency'),timezone=req.field('timezone'),
                as_of=req.field('as_of'),horizon_days=integer(req.field('horizon_days')),accounts=accounts,synthetic=req.field('synthetic')=='yes')
            return setup_business(ledger,spec,rev,ctx=ctx),'/entries'
        if op=='promise_create':
            fields(req,base|{'promise_id','invoice_id','amount','due_date'})
            rec=ledger.receivables.get(req.field('invoice_id'))
            if rec is None:raise CashflowError('Choose an invoice in this business.')
            return create_promise(ledger,req.field('promise_id'),rec.id,amount(req.field('amount'),rec.currency),day(req.field('due_date')),ev,note,rev,ctx=ctx),'/promises'
        if op=='promise_review':
            fields(req,base|{'promise_id','step','receipt_id','amount'})
            p=ledger.payment_promises.get(req.field('promise_id'))
            if p is None:raise CashflowError('Choose an existing promise.')
            ccy=ledger.receivables[p['invoice_id']].currency
            return review_promise(ledger,p['id'],req.field('step'),req.field('receipt_id'),amount(req.field('amount','0'),ccy),ev,note,rev,ctx=ctx),'/promises'
        if op=='net_match':
            fields(req,base|{'batch_id','row_id','movement_keys'},multiple={'movement_keys'})
            return review_net_settlement(ledger,req.field('batch_id'),req.field('row_id'),req.form.get('movement_keys',[]),ev,note,rev,ctx=ctx),'/batch-matching?'+urlencode(dict(batch_id=req.field('batch_id'),row_id=req.field('row_id')))
        if op=='unallocated_refund':
            fields(req,base|{'payment_id','refund_id','amount','date'});parent=ledger.split_receipts.get(req.field('payment_id'))
            if parent is None:raise CashflowError('Choose an observed parent payment.')
            return record_unallocated_refund(ledger,parent['id'],req.field('refund_id'),amount(req.field('amount'),parent['currency']),day(req.field('date')),ev,note,rev,ctx=ctx,
                acknowledge_duplicates=True if preview else req.field('acknowledge_duplicates')=='yes'),'/unallocated-refunds'
        if op=='unallocated_refund_correct':
            fields(req,base|{'refund_id','amount','date'});r=ledger.unallocated_refunds.get(req.field('refund_id'))
            if r is None:raise CashflowError('Choose an observed unallocated refund.')
            return correct_unallocated_refund(ledger,r['id'],amount(req.field('amount'),r['currency']),day(req.field('date')),ev,note,rev,ctx=ctx),'/unallocated-refunds'
        if op=='attachment_retire':
            fields(req,{'operation','revision','attachment_id','note'})
            return retire_attachment(ledger,req.field('attachment_id'),note,rev,ctx=ctx),'/attachments'
        raise CashflowError('Choose an operation from the owner workbench.')

    @web.route('POST','/workbench/preview')
    def preview(handler,req):
        def build(ledger):
            work=copy.deepcopy(ledger);result,_=parse(work,req,True);op=req.field('operation')
            body=f"<h1>Review {esc(titles[op])}</h1><p>No saved state changed. Check the exact source facts before confirming.</p>"+links
            body+="<div class='card'><h2>Proposed inputs</h2>"
            for name,values in req.form.items():
                if name not in {'csrf','operation','revision','confirmed','acknowledge_duplicates','target_business_confirmation'} and any(values):
                    body+=f"<div class='kv stack'><span>{esc(name.replace('_',' '))}</span><b>{esc('; '.join(values))}</b></div>"
            body+='</div>'
            if op=='setup':
                body+=f"<div class='card'><h2>Replace active synthetic business</h2><p>{esc(ledger.business_id)} → {esc(work.business_id)}. This discards current active ledger records from the new workspace. An automatic recovery copy is created before replacement. Its attachments are retained in that backup. This is a new owner-attested opening, not an observed cash transfer.</p>"
                for a in work.accounts.values():body+=f"<p>{esc(a.id)}: {money(a.opening_minor,a.currency)}, floor {money(a.floor_minor,a.currency)}; {web.evidence_link(work,a.evidence_id)}.</p>"
                body+='</div>'
            else:
                dates={ledger.as_of}
                if req.field('date'):dates.add(day(req.field('date')))
                body+="<div class='card'><h2>Recorded cash before → after</h2>"
                for a in ledger.accounts.values():
                    for d in sorted(dates):body+=f"<p>{esc(a.currency)} through {d}: {money(ledger.actual_cash(a.currency,d),a.currency)} → {money(work.actual_cash(a.currency,d),a.currency)}</p>"
                body+='</div>'
            if op=='net_match':
                current={m['key']:m for m in observed_movements(ledger)};selected=[current[k] for k in req.form['movement_keys']]
                ccy=selected[0]['currency'];body+="<div class='card'><h2>Exact net settlement</h2>"
                for m in selected:body+=f"<p>{esc(m['key'])}: {money(m['signed_minor'],ccy)} on {esc(m['date'])}; {web.evidence_link(ledger,m['evidence_id'])}.</p>"
                body+=f"<p>Signed total {money(sum(m['signed_minor'] for m in selected),ccy)}. Matching creates no balancing fee, receipt, bank balance verification or action reconciliation.</p></div>"
            if 'unallocated_minor' in result:body+='<p>Unallocated parent balance after confirmation: '+str(result['unallocated_minor'])+' minor units in the selected parent currency.</p>'
            warnings=result.get('possible_duplicate_ids',[])
            if warnings:body+='<div class="card"><h2>Possible duplicates</h2>'+''.join('<p>'+esc(k)+'</p>' for k in warnings)+'</div>'
            body+="<div class='card'><form method='post' action='/workbench/confirm'>"+web.csrf_field()
            for name,values in req.form.items():
                if name not in {'csrf','confirmed','acknowledge_duplicates','target_business_confirmation'}:
                    for value in values:body+=f"<input type='hidden' name='{esc(name)}' value='{esc(value)}'>"
            if op=='setup':body+="<label>Type the NEW business ID to confirm replacement<input name='target_business_confirmation' required autocomplete='off'></label>"
            if warnings:body+="<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I checked these are distinct observed movements.</label>"
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I reviewed these exact facts and explicitly authorize this local owner operation. No money or external message is sent.</label>"+end('Confirm exact owner operation')+'</div>'
            return page('Review workbench operation',body,'/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/workbench/confirm')
    def confirm(handler,req):
        if req.field('confirmed')!='yes':raise CashflowError('Review and explicitly confirm the owner operation.')
        if req.field('operation')=='setup' and req.field('target_business_confirmation')!=req.field('id'):raise CashflowError('Type the exact new business ID to confirm replacement.')
        def apply(ledger):
            if req.field('operation')!='setup':return parse(ledger,req)
            work=copy.deepcopy(ledger);result,dest=parse(work,req)
            if not result['duplicate']:
                from .recovery import create_backup
                create_backup(web.database_path(),'Automatic before guided business setup',ctx=web._owner_ctx(ledger))
                ledger.__dict__.clear();ledger.__dict__.update(work.__dict__)
            return result,dest
        _,dest=web.mutate_state(apply);return handler._redirect(dest)

    @web.route('GET','/setup')
    def setup(handler,req):
        def build(ledger):
            currencies='<option value="">Choose currency</option>'+''.join(f"<option value='{c}'>{c}</option>" for c in MINOR_DIGITS)
            zones=''.join(f"<option value='{z}'>{z}</option>" for z in ['UTC','Asia/Dhaka','Asia/Shanghai','Asia/Singapore','Europe/London','America/New_York'])
            body='<h1>Guided business setup</h1><p>Create a new synthetic business without editing fixture JSON. This replaces the one active business on this local server after explicit review and an automatic recovery copy. Use Daily Cash Update to correct the existing business instead.</p>'+links
            body+="<div class='card'>"+form(ledger,'setup')+f"<label>New unique business ID<input name='id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>Business name<input name='name' maxlength='120' required></label><label>Base currency<select name='base_currency' required>{currencies}</select></label><label>Displayed business timezone<select name='timezone'>{zones}</select></label><label>Opening snapshot date<input name='as_of' type='date' value='{ledger.as_of}' required></label><label>Forecast days (1–120)<input name='horizon_days' type='number' min='1' max='120' value='30' required></label>"
            for i in range(6):
                body+=f"<fieldset><legend>Currency account {i+1}{' (required)' if not i else ' (optional; leave entirely blank if unused)'}</legend><label>Account ID<input name='account_{i}_id' pattern='[A-Za-z0-9_-]{{1,64}}' {'required' if not i else ''}></label><label>Currency<select name='account_{i}_currency'>{currencies}</select></label><label>Opening amount (negative allowed)<input name='account_{i}_opening' inputmode='decimal'></label><label>Minimum cash floor<input name='account_{i}_floor' inputmode='decimal'></label><label>Checked synthetic opening source<textarea name='account_{i}_source_note' maxlength='1000'></textarea></label></fieldset>"
            body+="<label><input type='checkbox' name='synthetic' value='yes' required> All entered business/source data is synthetic.</label>"+end('Preview new business and openings')+'</div>'
            return page('Business setup',body,'/setup')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/promises')
    def promises(handler,req):
        def build(ledger):
            body='<h1>Customer payment promises</h1><p>Record source-backed customer promises, then link already-observed receipts explicitly. A promise changes no cash, expected invoice date, approval or consent. Missed dates use the visible business snapshot date, not a background monitor.</p>'+links
            for rec in ledger.receivables.values():
                if not rec.remaining_minor:continue
                body+=f"<div class='card'><h2>{esc(rec.id)}</h2><p>Current remaining {money(rec.remaining_minor,rec.currency)}; party {esc(rec.counterparty or 'unknown')}; {web.evidence_link(ledger,rec.evidence_id)}.</p>"+form(ledger,'promise_create')+f"<input type='hidden' name='invoice_id' value='{esc(rec.id)}'><label>Unique promise ID<input name='promise_id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>Promised amount — {esc(rec.currency)}<input name='amount' inputmode='decimal' required></label><label>Promised payment date<input name='due_date' type='date' value='{ledger.as_of}' required></label>"+source(ledger)+end('Preview recorded promise')+'</div>'
            for p in promise_report(ledger,ctx=web._owner_ctx(ledger)):
                body+=f"<div class='card' id='promise-{esc(p['id'])}'><h2>{esc(p['id'])} · {esc(p['status'].replace('_',' '))}</h2><p>Invoice {esc(p['invoice_id'])}; due {esc(p['due_date'])}; promised {money(p['amount_minor'],p['currency'])}; linked observed {money(p['fulfilled_minor'],p['currency'])}; unfulfilled {money(p['remaining_minor'],p['currency'])}. Business date {ledger.as_of}. {web.evidence_link(ledger,p['evidence_id'])}.</p>"
                if p['stale_links'] or p['held']:body+='<p>Source/receipt or dispute changed: review this promise. Stale links do not count as fulfillment.</p>'
                if not p['cancelled']:
                    options='<option value="">Choose existing observed receipt/allocation</option>'
                    for rid in ledger.receipts:
                        r=effective_receipt(ledger,rid)
                        if r.receivable_id==p['invoice_id'] and r.state=='observed' and r.amount_minor and r.date_received<=ledger.as_of:
                            options+=f"<option value='{esc(rid)}'>{esc(rid)} — {money(r.amount_minor,p['currency'])} on {r.date_received}</option>"
                    body+=form(ledger,'promise_review')+f"<input type='hidden' name='promise_id' value='{esc(p['id'])}'><input type='hidden' name='step' value='link'><label>Observed receipt<select name='receipt_id' required>{options}</select></label><label>Portion fulfilling this promise — {esc(p['currency'])}<input name='amount' inputmode='decimal' required></label>"+source(ledger)+end('Preview observed fulfillment link')
                    for rid,link in p['links'].items():
                        body+=f"<p>Linked {esc(rid)}: {money(link['amount_minor'],p['currency'])}.</p>"+form(ledger,'promise_review')+f"<input type='hidden' name='promise_id' value='{esc(p['id'])}'><input type='hidden' name='step' value='unlink'><input type='hidden' name='receipt_id' value='{esc(rid)}'><input type='hidden' name='amount' value='0'>"+source(ledger)+end('Preview unlinking this receipt')
                    body+=form(ledger,'promise_review')+f"<input type='hidden' name='promise_id' value='{esc(p['id'])}'><input type='hidden' name='step' value='cancel'><input type='hidden' name='amount' value='0'>"+source(ledger)+end('Preview cancelling promise')
                if p['cancelled']:
                    for rid in p['links']:
                        body+=form(ledger,'promise_review')+f"<input type='hidden' name='promise_id' value='{esc(p['id'])}'><input type='hidden' name='step' value='unlink'><input type='hidden' name='receipt_id' value='{esc(rid)}'><input type='hidden' name='amount' value='0'><p>Correct a mistaken historical receipt link without reviving cancelled terms: {esc(rid)}.</p>"+source(ledger)+end('Preview unlinking historical receipt')
                for h in p['history']:body+=f"<p>{esc(h['step'])}: {esc(h['receipt_id'])}; {esc(h['note'])}; {web.evidence_link(ledger,h['evidence_id'])}.</p>"
                body+='</div>'
            return page('Customer promises',body,'/promises')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/batch-matching')
    def net_matches(handler,req):
        def build(ledger):
            body='<h1>Batch / net settlement matching</h1><p>Match several already-observed signed movements to one statement row. Example: receipts 600 minus separately observed fee 10 = deposit 590. Exact sum/currency and a 7-day posting window are required. No receipt, fee, cash or bank-balance verification is created here.</p>'+links
            movements=observed_movements(ledger)
            for batch in ledger.statement_batches.values():
                if arg(req,'batch_id') and arg(req,'batch_id')!=batch['id']:continue
                for row in statement_report(ledger,batch['id'],ctx=web._owner_ctx(ledger))['rows']:
                    if arg(req,'row_id') and arg(req,'row_id')!=row['id']:continue
                    options=''
                    for m in movements:
                        if m['currency']==row['currency'] and abs((day(m['date'])-day(row['date'])).days)<=7:
                            options+=f"<option value='{esc(m['key'])}'>{esc(m['key'])} — {money(m['signed_minor'],m['currency'])} — {esc(m['date'])}</option>"
                    body+=f"<div class='card'><h2>{esc(row['id'])} · {esc(row['status'].replace('_',' '))}</h2><p>{money(row['signed_minor'],row['currency'])} on {esc(row['date'])}; {esc(row['reference'])}; {web.evidence_link(ledger,batch['evidence_id'])}.</p>"
                    if row['review'] and row['review'].get('mode')=='net':body+='<p>Reviewed components: '+esc(', '.join(row['review']['fingerprints']))+'</p>'
                    body+=form(ledger,'net_match')+f"<input type='hidden' name='batch_id' value='{esc(batch['id'])}'><input type='hidden' name='row_id' value='{esc(row['id'])}'><label>Select 2–30 existing observations (multiple selection)<select name='movement_keys' multiple size='8' required>{options}</select></label>"+source(ledger)+end('Preview exact net match')+f"<p><a href='/statements?batch_id={quote(batch['id'],safe='')}'>Explicitly unmatch/exclude or review individual candidates</a> · <a href='/cash-movements'>Record an already-observed fee separately</a></p></div>"
            if not ledger.statement_batches:body+="<p><a href='/statements'>Import a synthetic statement first</a>.</p>"
            return page('Net settlement matching',body,'/batch-matching')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/unallocated-refunds')
    def unallocated(handler,req):
        def build(ledger):
            body='<h1>Unallocated-payment refunds</h1><p>Record an already-observed return of excess/wrongly deposited money from an observed parent payment. Invoice allocations remain unchanged. An unconfirmed inbox row is not cash and cannot fund a refund. Nothing is sent.</p>'+links
            for parent in ledger.split_receipts.values():
                ccy=parent['currency'];current=effective_split_payment(ledger,parent)
                body+=f"<div class='card'><h2>{esc(parent['id'])}</h2><p>Original whole amount {money(parent['amount_minor'],ccy)}; effective gross {money(current['amount_minor'],ccy)}; effective returned {money(unallocated_refund_total(ledger,parent['id']),ccy)}; unallocated remainder {money(split_unallocated(ledger,parent),ccy)}. {web.evidence_link(ledger,parent['evidence_id'])}.</p>"
                if split_unallocated(ledger,parent)>0:
                    body+=form(ledger,'unallocated_refund')+f"<input type='hidden' name='payment_id' value='{esc(parent['id'])}'><label>Unique refund observation ID<input name='refund_id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>Observed amount — {esc(ccy)}<input name='amount' inputmode='decimal' required></label><label>Observed date<input name='date' type='date' value='{ledger.as_of}' required></label>"+source(ledger)+end('Preview observed excess refund')
                for r in ledger.unallocated_refunds.values():
                    if r['payment_id']!=parent['id']:continue
                    body+=f"<h3>{esc(r['id'])}</h3><p>Original {money(r['amount_minor'],ccy)} on {esc(r['date'])}; effective amount {money(effective_refund(r),ccy)}. {esc(r['note'])}; {web.evidence_link(ledger,r['evidence_id'])}.</p>"+form(ledger,'unallocated_refund_correct')+f"<input type='hidden' name='refund_id' value='{esc(r['id'])}'><label>Corrected amount — {esc(ccy)} (zero reverses mistaken observation)<input name='amount' value='{raw(effective_refund(r),ccy)}' inputmode='decimal' required></label><label>Accounting correction date<input name='date' type='date' value='{ledger.as_of}' required></label>"+source(ledger)+end('Preview refund amount correction')
                    for c in r['adjustments']:body+=f"<p>{esc(c['date'])}: {money(c['old_amount_minor'],ccy)} → {money(c['new_amount_minor'],ccy)}; {web.evidence_link(ledger,c['evidence_id'])}.</p>"
                body+='</div>'
            if not ledger.split_receipts:body+="<p><a href='/split-receipts'>First confirm an observed whole payment and retain its unallocated excess</a>.</p>"
            return page('Unallocated refunds',body,'/unallocated-refunds')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/parties')
    def parties(handler,req):
        def build(ledger):
            role=arg(req,'role','customer');party=arg(req,'party');ccy=arg(req,'currency')
            if role not in {'customer','supplier'}:raise CashflowError('Choose customer or supplier.')
            group=ledger.receivables if role=='customer' else ledger.outflows
            names=sorted({i.counterparty for i in group.values() if i.counterparty})
            choices='<option value="">Choose exact party</option>'+''.join(f"<option value='{esc(n)}' {'selected' if n==party else ''}>{esc(n)}</option>" for n in names)
            currencies='<option value="">All currencies, separate balances</option>'+''.join(f"<option value='{esc(a.currency)}' {'selected' if a.currency==ccy else ''}>{esc(a.currency)}</option>" for a in ledger.accounts.values())
            body='<h1>Customer / supplier account statement</h1><p>Choose the role first, then an exact recorded party label. Similar names are not merged. Assign missing party/reference details through source review.</p>'+links+f"<div class='card'><form method='get' action='/parties'><label>Role<select name='role'><option value='customer' {'selected' if role=='customer' else ''}>Customer</option><option value='supplier' {'selected' if role=='supplier' else ''}>Supplier</option></select></label><label>Exact party<select name='party'>{choices}</select></label><label>Currency<select name='currency'>{currencies}</select></label><button type='submit'>Show current party statement</button></form><p><a href='/parties?role=customer'>Choose customer</a> · <a href='/parties?role=supplier'>Choose supplier</a> · <a href='/records'>Review party details</a></p></div>"
            if party:
                report=party_statement(ledger,role,party,ccy,ctx=web._owner_ctx(ledger));body+='<p>'+esc(report['note'])+'</p>'
                body+=f"<p><a href='/parties/export.csv?{esc(urlencode(dict(role=role,party=party,currency=ccy)))}'>Download this party statement CSV</a></p>"
                for currency,s in report['summary'].items():
                    body+=f"<div class='card'><h2>{esc(currency)} current balances · {esc(party)}</h2>"+''.join(f"<div class='kv stack'><span>{esc(k.replace('_minor','').replace('_',' '))}</span><b>{money(v,currency)}</b></div>" for k,v in s.items())+'</div>'
                for r in report['rows']:body+=f"<div class='card'><b>{esc(r['kind'].replace('_',' '))}: {esc(r['id'])}</b><p>{esc(r['date'] or 'current terms')} · {money(r['amount_minor'],r['currency'])} · {esc(r['target_id'])}</p><p>{esc(r['note'])}</p>{web.evidence_link(ledger,r['evidence_id'])}</div>"
            return page('Party statement',body,'/parties')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/parties/export.csv')
    def export_party(handler,req):
        text=web.read_state(lambda l:party_csv(party_statement(l,arg(req,'role'),arg(req,'party'),arg(req,'currency'),ctx=web._owner_ctx(l))))
        return download(handler,text.encode('utf-8'),'text/csv; charset=utf-8','synthetic-party-statement.csv')

    def download(handler,data,content_type,filename):
        handler.send_response(200);handler.send_header('Content-Type',content_type);handler.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Content-Security-Policy',"sandbox; default-src 'none'")
        handler.send_header('Cache-Control','no-store');handler.send_header('Content-Length',str(len(data)));handler.end_headers();handler.wfile.write(data)

    @web.route('GET','/attachments')
    def attachments(handler,req):
        def build(ledger):
            body='<h1>Original evidence attachments</h1><p>Explicitly retain an original synthetic file linked to an existing evidence record. Older document imports still retain only extracted structured data/hashes. Files grant no approval/consent and are not automatically read by the model. Download only; no inline execution or automatic opening. Plaintext SQLite/backups retain these bytes; basic type checks are not malware or authenticity verification.</p>'+links
            body+=f"<div class='card'><h2>Opt-in original file storage</h2><p>PDF, PNG, JPEG, UTF-8 TXT/CSV; 1 MiB per file, 8 MiB active bytes and 32 lifetime attachment records per business.</p><form method='post' action='/attachments/preview' enctype='multipart/form-data'>{web.csrf_field()}<input type='hidden' name='revision' value='{review_revision(ledger)}'><label>Existing evidence<select name='evidence_id' required>{sources(ledger,arg(req,'evidence_id'))}</select></label><label>Original synthetic file<input type='file' name='file' accept='.pdf,.png,.jpg,.jpeg,.txt,.csv' required></label><label>Source / attachment reason<textarea name='note' maxlength='1000' required></textarea></label><button type='submit'>Preview retaining original bytes</button></form></div>"
            for a in attachment_inventory(ledger,ctx=web._owner_ctx(ledger)):
                if arg(req,'evidence_id') and a['evidence_id']!=arg(req,'evidence_id'):continue
                body+=f"<div class='card'><h2>{esc(a['filename'])} · {'active' if a['active'] else 'retired'}</h2><p>{a['bytes']} original bytes; source {web.evidence_link(ledger,a['evidence_id'])}. {esc(a['note'])}</p><p>SHA-256 <code>{esc(a['sha256'])}</code></p>"
                if a['active']:body+=f"<p><a href='/attachments/{a['id']}/download'>Download original file</a></p>"+form(ledger,'attachment_retire')+f"<input type='hidden' name='attachment_id' value='{a['id']}'><label>Retirement reason<textarea name='note' maxlength='1000' required></textarea></label>"+end('Preview removing bytes from active storage')+'<p>Earlier recovery copies can still contain the original bytes; retirement is not secure deletion from backups.</p>'
                body+='</div>'
            return page('Evidence attachments',body,'/attachments')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/attachments/preview')
    def attachment_preview(handler,req):
        fields(req,{'revision','evidence_id','note'})
        if set(req.files)!={'file'}:raise CashflowError('Upload exactly one original evidence file.')
        up=req.files['file'];metadata=validate_attachment(up['filename'],up['data'])
        def validate(ledger):
            work=copy.deepcopy(ledger);attach_evidence(work,req.field('evidence_id'),up['filename'],up['data'],req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger))
            return ledger.business_id
        business=web.read_state(validate)
        with web.LOCK:
            now=time.monotonic()
            for key,staged in list(web.staged().items()):
                if staged.get('kind')=='evidence-attachment' and staged.get('expires',0)<=now:web.staged().pop(key,None)
            if sum(s.get('kind')=='evidence-attachment' for s in web.staged().values())>=4:
                raise CashflowError('Four original-file reviews are already staged. Confirm them or wait for their 10-minute expiry before another upload.')
            token=web._stage(dict(kind='evidence-attachment',expires=now+600,business_id=business,evidence_id=req.field('evidence_id'),note=req.field('note'),revision=req.field('revision'),filename=up['filename'],data=up['data']))
        body=f"<h1>Review original evidence attachment</h1><div class='card'><p>Filename: {esc(metadata['filename'])}; {metadata['bytes']} bytes; {esc(metadata['media_type'])}. Source {esc(req.field('evidence_id'))}.</p><p>SHA-256 <code>{metadata['sha256']}</code></p><p>{esc(req.field('note'))}</p><p>Confirming retains these original bytes in local plaintext SQLite and future recovery copies. Evidence never grants authority. No file content is sent to a model.</p><form method='post' action='/attachments/confirm'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'><label><input type='checkbox' name='confirmed' value='yes' required> I authorize retaining this exact synthetic file and source association locally.</label><button type='submit'>Confirm original attachment storage</button></form></div>"+links
        return handler._send_html(200,page('Review attachment',body,'/attachments'))

    @web.route('POST','/attachments/confirm')
    def attachment_confirm(handler,req):
        fields(req,{'token'})
        if req.field('confirmed')!='yes':raise CashflowError('Explicitly confirm local original-file storage.')
        token=req.field('token')
        def apply(ledger):
            from .authz import require_access
            require_access(web._owner_ctx(ledger),ledger)
            if token in ledger.attachment_import_receipts:return ledger.attachment_import_receipts[token]
            staged=web.staged().get(token)
            if not staged or staged.get('kind')!='evidence-attachment' or staged.get('expires',0)<=time.monotonic() or staged['business_id']!=ledger.business_id:raise CashflowError('Attachment review expired or belongs to another business. Upload and review again.')
            result=attach_evidence(ledger,staged['evidence_id'],staged['filename'],staged['data'],staged['note'],staged['revision'],ctx=web._owner_ctx(ledger))
            ledger.attachment_import_receipts[token]=result;return result
        web.mutate_state(apply);web._pop_staged(token);return handler._redirect('/attachments')

    @web.route('GET','/attachments/<attachment_id>/download')
    def original_download(handler,req,attachment_id):
        data,meta=web.read_state(lambda ledger:attachment_bytes(ledger,attachment_id,ctx=web._owner_ctx(ledger)))
        return download(handler,data,'application/octet-stream',meta['id']+meta['extension'])
