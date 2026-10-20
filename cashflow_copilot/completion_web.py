"""Reviewed owner forms for source entries, observed cash and statements."""
import copy
import csv
from datetime import date
import hashlib
from io import StringIO
import re
from urllib.parse import quote

from .completion import (create_record,adjust_face,correct_split_payment,record_cash_movement,
    correct_cash_movement,import_statement,statement_report,review_statement_row,CASH_CLASSES)
from .errors import CashflowError
from .owner_workflows import review_revision
from .payment_review import payment_warnings,review_payment_pair
from .settlement import effective_split_payment,split_unallocated


def mount(web):
    esc,money=web.esc,web.money
    labels={'manual_record':'new invoice / bill','invoice_adjustment':'invoice credit / source adjustment',
        'bill_adjustment':'bill correction / cancellation','split_correction':'whole split-payment correction',
        'cash_create':'other observed cash movement','cash_correct':'other cash correction',
        'statement_match':'statement transaction review','payment_pair':'duplicate-payment source review'}
    links="<p><a href='/entries'>New invoice / bill</a> · <a href='/invoice-adjustments'>Invoice adjustments</a> · <a href='/bill-adjustments'>Bill adjustments</a> · <a href='/split-payment-corrections'>Whole-payment corrections</a> · <a href='/cash-movements'>Other cash</a> · <a href='/statements'>Statement reconciliation</a> · <a href='/payment-warnings'>Payment warnings</a></p><p><a href='/setup'>Business setup</a> · <a href='/promises'>Customer promises</a> · <a href='/batch-matching'>Batch / net matching</a> · <a href='/unallocated-refunds'>Unallocated refunds</a> · <a href='/parties'>Party statements</a> · <a href='/attachments'>Original attachments</a></p><p><a href='/refunds'>Refund settlements</a> · <a href='/payment-dates'>Payment dates</a> · <a href='/instalments'>Supplier instalments</a> · <a href='/search'>Search</a> · <a href='/scenarios'>Combined scenarios</a> · <a href='/calendar'>Cash calendar</a></p>"

    def fields(req,allowed,confirmed=False):
        if set(req.form)-set(allowed)-{'csrf','confirmed','acknowledge_duplicates'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Unexpected or repeated owner form field. Reload the current form.')
        if confirmed and req.field('confirmed')!='yes':raise CashflowError('Explicit owner confirmation is required. Documents supply evidence, never authority.')

    def hidden(ledger,operation):
        return web.csrf_field()+f"<input type='hidden' name='revision' value='{review_revision(ledger)}'><input type='hidden' name='operation' value='{operation}'>"

    def evidence(ledger):
        return '<option value="">Choose checked source</option>'+''.join(f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:85])}</option>" for e in ledger.evidence.values())

    def source_fields(ledger):
        return f"<label>Checked source<select name='evidence_id' required>{evidence(ledger)}</select></label><label>What did you verify / why this change?<textarea name='note' maxlength='1000' required></textarea></label>"

    def day(raw):
        try:return date.fromisoformat(raw)
        except (ValueError,TypeError):raise CashflowError('Use a calendar date in YYYY-MM-DD format.')

    def amount(raw,ccy,signed=False):
        pattern=r'-?[0-9]{1,12}(\.[0-9]{1,3})?' if signed else r'[0-9]{1,12}(\.[0-9]{1,3})?'
        if not re.fullmatch(pattern,raw):raise CashflowError('Use an ordinary money amount in the shown currency; no scientific notation or FX.')
        return web._parse_major_to_minor(raw,ccy)

    def raw_amount(minor,ccy):return web.format_amount(minor,ccy).split(' ',1)[1].replace(',','')

    def parse(ledger,req,ack_override=None):
        op=req.field('operation');base={'revision','operation','evidence_id','note'}
        ack=req.field('acknowledge_duplicates')=='yes' if ack_override is None else ack_override
        source,note,revision=req.field('evidence_id'),req.field('note'),req.field('revision');ctx=web._owner_ctx(ledger)
        if op=='manual_record':
            fields(req,base|{'kind','id','currency','amount','date','due_date','category','protected','counterparty','invoice_reference'})
            record=dict(kind=req.field('kind'),id=req.field('id'),currency=req.field('currency'),amount_minor=amount(req.field('amount'),req.field('currency')),
                date=req.field('date') or None,due_date=req.field('due_date') or None,category=req.field('category'),protected=req.field('protected')=='yes',
                counterparty=req.field('counterparty'),invoice_reference=req.field('invoice_reference'),evidence_id=source,note=note)
            return create_record(ledger,record,revision,ctx=ctx,acknowledge_duplicates=ack),'/entries'
        if op in {'invoice_adjustment','bill_adjustment'}:
            fields(req,base|{'item_id','amount','date'})
            kind='invoice' if op=='invoice_adjustment' else 'bill';collection=ledger.receivables if kind=='invoice' else ledger.outflows
            item=collection.get(req.field('item_id'))
            if item is None:raise CashflowError('Choose an existing record in this business.')
            return adjust_face(ledger,kind,item.id,amount(req.field('amount'),item.currency),day(req.field('date')),source,note,revision,ctx=ctx),'/'+('invoice-adjustments' if kind=='invoice' else 'bill-adjustments')
        if op=='split_correction':
            fields(req,base|{'payment_id','amount','date'});parent=ledger.split_receipts.get(req.field('payment_id'))
            if parent is None:raise CashflowError('Choose an existing split payment.')
            return correct_split_payment(ledger,parent['id'],amount(req.field('amount'),parent['currency']),day(req.field('date')),source,note,revision,ctx=ctx),'/split-payment-corrections'
        if op=='cash_create':
            fields(req,base|{'movement_id','classification','currency','amount','date'})
            return record_cash_movement(ledger,req.field('movement_id'),req.field('classification'),req.field('currency'),amount(req.field('amount'),req.field('currency')),
                day(req.field('date')),source,note,revision,ctx=ctx,acknowledge_duplicates=ack),'/cash-movements'
        if op=='cash_correct':
            fields(req,base|{'movement_id','amount','date'});item=ledger.cash_movements.get(req.field('movement_id'))
            if item is None:raise CashflowError('Choose an existing cash movement.')
            return correct_cash_movement(ledger,item['id'],amount(req.field('amount'),item['currency']),day(req.field('date')),source,note,revision,ctx=ctx),'/cash-movements'
        if op=='statement_match':
            fields(req,base|{'batch_id','row_id','movement_key','status'})
            return review_statement_row(ledger,req.field('batch_id'),req.field('row_id'),req.field('movement_key'),req.field('status'),source,note,revision,ctx=ctx),'/statements?batch_id='+quote(req.field('batch_id'),safe='')
        if op=='payment_pair':
            fields(req,base|{'pair_id','verdict'})
            return review_payment_pair(ledger,req.field('pair_id'),req.field('verdict'),source,note,revision,ctx=ctx),'/payment-warnings'
        raise CashflowError('Choose an owner operation from the current form.')

    @web.route('POST','/owner-entries/preview')
    def operation_preview(handler,req):
        def build(ledger):
            before={a.currency:ledger.actual_cash(a.currency) for a in ledger.accounts.values()};work=copy.deepcopy(ledger)
            result,_=parse(work,req,ack_override=True)
            op=req.field('operation')
            body=f"<h1>Review {esc(labels[op])}</h1><p>This preview has not changed the saved ledger. Review the source, inputs and cash effect before confirming.</p><div class='card'><h2>Proposed source facts</h2>"
            names={'kind':'Record kind','id':'New record ID','item_id':'Record ID','payment_id':'Whole payment ID','movement_id':'Movement ID',
                'currency':'Currency','amount':'Amount in record currency','date':'Effective / source date','due_date':'Invoice contractual due date',
                'category':'Bill / invoice category','protected':'Protected obligation','counterparty':'Party','invoice_reference':'External reference',
                'classification':'Cash classification','row_id':'Statement row','movement_key':'Observed movement','status':'Statement decision','verdict':'Duplicate review decision'}
            for name,label in names.items():
                if name in req.form:body+=f"<div class='kv stack'><span>{esc(label)}</span><b>{esc(req.field(name) or 'unknown / not selected')}</b></div>"
            body+=f"<p>Checked source: {web.evidence_link(ledger,req.field('evidence_id'))}. {esc(req.field('note'))}.</p></div><div class='card'><h2>Recorded cash effect</h2>"
            for ccy,old in before.items():
                current=work.actual_cash(ccy)
                body+=f"<div class='kv stack'><span>{esc(ccy)} through current snapshot: before → after</span><b>{money(old,ccy)} → {money(current,ccy)}</b></div>"
            if 'cash_delta_minor' in result:body+='<p>Any dated accounting delta is retained separately in history; it does not execute a refund/transfer. A future-dated entry affects cash only on its stated date.</p>'
            if 'credit_or_overpaid_minor' in result:
                collection=work.receivables if result['kind']=='invoice' else work.outflows;item=collection[result['item_id']]
                body+=f"<p>Remaining obligation/claim: {money(result['remaining_minor'],item.currency)}. Customer credit / supplier overpayment requiring separate review: {money(result['credit_or_overpaid_minor'],item.currency)}. This creates no refund cash.</p>"
            warnings=result.get('possible_duplicate_ids',[])
            body+='</div>'
            if warnings:body+="<div class='card'><h2>Possible duplicates to check</h2>"+''.join('<p>'+esc(w)+'</p>' for w in warnings)+'<p>Equal amounts/dates can be legitimate. No automatic merge or deletion occurs.</p></div>'
            body+=f"<div class='card'><form method='post' action='/owner-entries/confirm'>{web.csrf_field()}"
            for name,values in req.form.items():
                if name not in {'csrf','confirmed','acknowledge_duplicates'}:body+=f"<input type='hidden' name='{esc(name)}' value='{esc(values[0])}'>"
            if warnings:body+="<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I checked the possible duplicate IDs against the source and confirm these are distinct records/movements.</label>"
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I reviewed these exact source facts and authorize this local owner operation. No message, transfer, refund or counterparty consent is executed.</label><button type='submit'>Confirm reviewed owner operation</button></form></div>"+links
            return web.page('Review owner entry',body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/owner-entries/confirm')
    def operation_confirm(handler,req):
        if req.field('confirmed')!='yes':raise CashflowError('Review the preview and explicitly confirm as owner.')
        result,destination=web.mutate_state(lambda ledger:parse(ledger,req))
        return handler._redirect(destination)

    @web.route('GET','/entries')
    def entries_page(handler,req):
        def build(ledger):
            currencies=''.join(f"<option value='{esc(a.currency)}'>{esc(a.currency)}</option>" for a in ledger.accounts.values())
            body="<h1>Owner entries &amp; source corrections</h1><p>Add an invoice or scheduled bill from checked evidence. This records a claim/obligation, never observed cash, approval or consent.</p>"+links
            body+="<p><a href='/party-register'>Party register</a> · <a href='/cash-accounts'>Cash accounts / transfers</a> · <a href='/cash-availability'>Cash availability</a> · <a href='/statement-balances'>Statement balance checks</a> · <a href='/credits'>Credit carry-forward</a> · <a href='/loans'>Loans / EMI</a> · <a href='/account-forecast'>Account funding</a> · <a href='/weekly-cashflow'>13-week cash view</a> · <a href='/decision-brief'>Decision Brief</a></p>"
            for kind in ['invoice','bill']:
                body+=f"<div class='card'><h2>New {kind}</h2><form method='post' action='/owner-entries/preview'>{hidden(ledger,'manual_record')}<input type='hidden' name='kind' value='{kind}'>"
                body+="<label>Unique record ID<input name='id' pattern='[A-Za-z0-9_-]{1,64}' required></label>"+f"<label>Currency<select name='currency'>{currencies}</select></label><label>Original amount<input name='amount' inputmode='decimal' required></label>"
                body+=f"<label>{'Expected collection date (blank = unknown)' if kind=='invoice' else 'Contractual bill date'}<input type='date' name='date' {'required' if kind=='bill' else ''}></label>"
                if kind=='invoice':body+="<input type='hidden' name='category' value='invoice'><label>Contractual invoice due date (blank = unknown)<input type='date' name='due_date'></label>"
                else:body+="<label>Category<select name='category'><option value='supplier'>Supplier — changes need counterparty consent</option><option value='payroll'>Payroll — always protected</option><option value='operations'>Operations — no deferral limit granted by creation</option></select></label><label><input type='checkbox' name='protected' value='yes'> Protect this obligation (payroll is always protected)</label>"
                body+="<label>Customer / supplier (blank = unknown)<input name='counterparty' maxlength='200'></label><label>External invoice reference<input name='invoice_reference' maxlength='200'></label>"+source_fields(ledger)+"<button type='submit'>Preview new record</button></form></div>"
            if ledger.entry_records:
                body+="<div class='card'><h2>Owner-created records</h2>"+''.join(f"<p>{esc(r['kind'])} {esc(r['id'])}; {web.evidence_link(ledger,r['evidence_id'])}.</p>" for r in ledger.entry_records.values())+'</div>'
            return web.page('Owner entries',body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    def face_page(handler,req,kind):
        def build(ledger):
            collection=ledger.receivables if kind=='invoice' else ledger.outflows
            title='Invoice credit notes & source adjustments' if kind=='invoice' else 'Bill corrections & cancellation'
            body=f"<h1>{title}</h1><p>Original money/source/history remains. Confirm a source-backed effective total at the current snapshot; zero cancels the remaining claim/obligation. Observed payments are not erased. Credits/overpayments require separate observed refund settlement and create no refund cash by themselves. This does not grant contractual consent or alter protected dates.</p>"+links
            for item in collection.values():
                original=item.original_minor if kind=='invoice' else item.amount_minor
                paid=item.allocated_minor if kind=='invoice' else item.paid_minor
                credit=item.credit_balance_minor if kind=='invoice' else item.overpaid_minor
                body+=f"<div class='card' id='record-{esc(item.id)}'><h2>{esc(item.id)}</h2><p>Original {money(original,item.currency)}; effective total {money(item.effective_total_minor,item.currency)}; recorded allocation/paid {money(paid,item.currency)}; remaining {money(item.remaining_minor,item.currency)}; credit/overpaid {money(credit,item.currency)}. {web.evidence_link(ledger,item.evidence_id)}.</p>"
                if kind=='bill' and item.protected:body+='<p class="warn">Protected obligation: generic amount correction/cancellation is blocked. Payroll date remains protected.</p>'
                else:
                    body+=f"<form method='post' action='/owner-entries/preview'>{hidden(ledger,kind+'_adjustment')}<input type='hidden' name='item_id' value='{esc(item.id)}'><input type='hidden' name='date' value='{ledger.as_of.isoformat()}'>"
                    body+=f"<label>New effective total — {esc(item.currency)} (zero = cancellation, not refund)<input name='amount' inputmode='decimal' value='{raw_amount(item.effective_total_minor,item.currency)}' required></label>"+source_fields(ledger)+"<button type='submit'>Preview source adjustment</button></form>"
                for change in item.face_adjustments:body+=f"<p>History {esc(change['date'])}: {money(change['old_total_minor'],item.currency)} → {money(change['new_total_minor'],item.currency)}; {web.evidence_link(ledger,change['evidence_id'])}; {esc(change['note'])}.</p>"
                body+='</div>'
            return web.page(title,body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/invoice-adjustments')
    def invoices_page(handler,req):return face_page(handler,req,'invoice')

    @web.route('GET','/bill-adjustments')
    def bills_page(handler,req):return face_page(handler,req,'bill')

    @web.route('GET','/split-payment-corrections')
    def split_corrections_page(handler,req):
        def build(ledger):
            body="<h1>Whole split-payment corrections</h1><p>Correct the original whole-payment amount with dated accounting compensation. Original total/date/source stays in history. Correct child allocations first if the new total would be below their allocated sum; zero reverses only an entirely unallocated effective payment. No refund/transfer is executed. Use Payment dates for a separate source-backed correction inside the open snapshot.</p>"+links
            for parent in ledger.split_receipts.values():
                current=effective_split_payment(ledger,parent);allocated=current['amount_minor']-split_unallocated(ledger,parent)
                dates=[ledger.as_of,day(parent['date'])]+[ledger.receipts[r].date_received for r in parent['allocation_ids']]
                dates += [day(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==parent['id']]
                dates += [day(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
                earliest=max(dates)
                body+=f"<div class='card'><h2>{esc(parent['id'])}</h2><p>Original {money(parent['amount_minor'],parent['currency'])} on {esc(parent['date'])}; effective {money(current['amount_minor'],parent['currency'])}; allocated {money(allocated,parent['currency'])}; unallocated {money(split_unallocated(ledger,parent),parent['currency'])}. {web.evidence_link(ledger,parent['evidence_id'])}.</p>"
                body+=f"<form method='post' action='/owner-entries/preview'>{hidden(ledger,'split_correction')}<input type='hidden' name='payment_id' value='{esc(parent['id'])}'><label>Corrected whole amount — {esc(parent['currency'])}<input name='amount' inputmode='decimal' value='{raw_amount(current['amount_minor'],parent['currency'])}' required></label><label>Correction effective date<input type='date' name='date' min='{earliest.isoformat()}' value='{earliest.isoformat()}' required></label>"+source_fields(ledger)+"<button type='submit'>Preview whole-payment correction</button></form>"
                for change in ledger.split_adjustments.values():
                    if change['payment_id']==parent['id']:body+=f"<p>{esc(change['date'])}: {money(change['old_amount_minor'],parent['currency'])} → {money(change['new_amount_minor'],parent['currency'])}; signed accounting cash adjustment {money(change['cash_delta_minor'],parent['currency'])}. {web.evidence_link(ledger,change['evidence_id'])}; {esc(change['note'])}.</p>"
                body+='</div>'
            if not ledger.split_receipts:body+="<p>No split payment yet. <a href='/split-receipts'>Review imported observed payments</a>.</p>"
            return web.page('Whole-payment corrections',body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/cash-movements')
    def cash_page(handler,req):
        def build(ledger):
            currencies=''.join(f"<option value='{esc(a.currency)}'>{esc(a.currency)}</option>" for a in ledger.accounts.values())
            classes=''.join(f"<option value='{k}'>{esc(k.replace('_',' '))} — {'cash increase' if sign>0 else 'cash decrease'}</option>" for k,sign in CASH_CLASSES.items())
            body="<h1>Other observed cash movements</h1><p>Record an already-observed bank fee, owner capital, loan or other non-sales movement with checked evidence. Classification is an owner assertion, not tax/accounting advice. Do not also record the same movement as a receipt/bill payment. No transfer is executed; no invoice allocation or loan schedule is invented.</p>"+links
            body+=f"<div class='card'><h2>New observation</h2><form method='post' action='/owner-entries/preview'>{hidden(ledger,'cash_create')}<label>Unique movement ID<input name='movement_id' pattern='[A-Za-z0-9_-]{{1,64}}' required></label><label>Classification / cash direction<select name='classification'>{classes}</select></label><label>Currency<select name='currency'>{currencies}</select></label><label>Positive observed amount<input name='amount' inputmode='decimal' required></label><label>Observed date<input type='date' name='date' min='{ledger.as_of.isoformat()}' value='{ledger.as_of.isoformat()}' required></label>"+source_fields(ledger)+"<button type='submit'>Preview observed cash movement</button></form></div>"
            for item in ledger.cash_movements.values():
                current=item['adjustments'][-1]['new_signed_minor'] if item['adjustments'] else item['signed_minor']
                earliest=max([ledger.as_of,day(item['date'])]+[day(c['date']) for c in item['adjustments']])
                body+=f"<div class='card'><h2>{esc(item['id'])}</h2><p>{esc(item['classification'].replace('_',' '))}; original signed {money(item['signed_minor'],item['currency'])} on {esc(item['date'])}; effective signed {money(current,item['currency'])}. {web.evidence_link(ledger,item['evidence_id'])}.</p><form method='post' action='/owner-entries/preview'>{hidden(ledger,'cash_correct')}<input type='hidden' name='movement_id' value='{esc(item['id'])}'><label>Corrected absolute amount — {esc(item['currency'])} (zero = accounting reversal)<input name='amount' value='{raw_amount(abs(current),item['currency'])}' required inputmode='decimal'></label><label>Correction date<input type='date' name='date' min='{earliest.isoformat()}' value='{earliest.isoformat()}' required></label>"+source_fields(ledger)+"<button type='submit'>Preview accounting correction</button></form>"
                for change in item['adjustments']:body+=f"<p>{esc(change['date'])}: signed cash adjustment {money(change['cash_delta_minor'],item['currency'])}; {web.evidence_link(ledger,change['evidence_id'])}; {esc(change['note'])}.</p>"
                body+='</div>'
            return web.page('Other cash movements',body,active='/entries')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/payment-warnings')
    def warning_page(handler,req):
        def build(ledger):
            pairs=payment_warnings(ledger,ctx=web._owner_ctx(ledger))
            body="<h1>Duplicate observed-payment warnings</h1><p>Different IDs with the same signed amount, currency and observed date need a source check. They may be legitimate separate movements. Current effective observations are compared; split children and accounting deltas are not extra payments. Nothing is automatically merged, deleted or refunded.</p>"+links
            for pair in pairs:
                body+=f"<div class='card'><h2>{esc(pair['status'].replace('_',' '))}</h2>"
                for row in [pair['left'],pair['right']]:body+=f"<p>{esc(row['key'])}; target {esc(row['target'])}; {money(row['signed_minor'],row['currency'])} on {esc(row['date'])}; {web.evidence_link(ledger,row['evidence_id'])}.</p>"
                if pair['review']:body+=f"<p>Owner review: {esc(pair['review']['note'])}; {web.evidence_link(ledger,pair['review']['evidence_id'])}.</p>"
                body+=f"<form method='post' action='/owner-entries/preview'>{hidden(ledger,'payment_pair')}<input type='hidden' name='pair_id' value='{pair['id']}'><label>Source decision<select name='verdict'><option value='distinct'>Distinct observed movements</option><option value='possible_duplicate'>Possible duplicate — source correction needed</option></select></label>"+source_fields(ledger)+"<button type='submit'>Preview source review</button></form></div>"
            if not pairs:body+='<p>No pair matched this rule. This does not prove absence of duplicate real-world payments.</p>'
            return web.page('Payment warnings',body,active='/payment-warnings')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/statements')
    def statements_page(handler,req):
        def build(ledger):
            body="<h1>Statement transaction reconciliation</h1><p>Import a synthetic CSV and explicitly review matches to existing observed movements. This never creates cash, a receipt/payment, consent or an action reconciliation. Equal amount/date is only a candidate, not proof.</p>"+links
            body+=f"<div class='card'><h2>Import synthetic statement</h2><p><a href='/statement-mapping'>Map other statement columns / date formats, reuse profiles, or declare a zero-activity period</a>.</p><p>Exact CSV headers: id,date,currency,amount,reference. Amount is signed: cash increase positive, decrease negative. Up to 100 rows, UTF-8, 1 MiB. No bank connection.</p><form method='post' action='/statements/import' enctype='multipart/form-data'>{web.csrf_field()}<label>Synthetic CSV<input type='file' name='file' accept='.csv' required></label><label>Statement source note<textarea name='note' maxlength='1000' required></textarea></label><button type='submit'>Preview statement rows — no cash effect</button></form></div>"
            wanted=req.query.get('batch_id',[''])[0]
            if wanted and wanted not in ledger.statement_batches:raise CashflowError('Unknown statement for this business.')
            for batch in ledger.statement_batches.values():
                if wanted and batch['id']!=wanted:continue
                report=statement_report(ledger,batch['id'],ctx=web._owner_ctx(ledger))
                if batch.get('context'):
                    c=batch['context'];body+=f"<p>Explicit account {esc(c['account_id'])}; declared inclusive period {esc(c['start'])} to {esc(c['end'])}. {'Declared zero activity; source assertion only.' if not batch['rows'] else 'Mapped source interpretation; not bank verification.'} <a href='/statement-balances'>Review boundary balances</a>.</p>"
                body+=f"<div class='card'><h2>{esc(batch['id'])}</h2><p>{len(batch['rows'])} imported rows; {web.evidence_link(ledger,batch['evidence_id'])}. {esc(report['note'])}</p></div>"
                for row in report['rows']:
                    body+=f"<div class='card'><h3>{esc(row['id'])} — {esc(row['status'].replace('_',' '))}</h3><p>{money(row['signed_minor'],row['currency'])} on {esc(row['date'])}; reference {esc(row['reference'] or 'unknown')}.</p>"
                    if row['review']:body+=f"<p>Previous/current source review: {esc((', '.join(row['review']['fingerprints']) if row['review'].get('mode')=='net' else row['review']['movement_key'] or row['review']['status']))}; {esc(row['review']['note'])}. {web.evidence_link(ledger,row['review']['evidence_id'])}.</p>"
                    body+=f"<p><a href='/batch-matching?batch_id={batch['id']}&amp;row_id={esc(row['id'])}'>Match several observed payments and fees to this net amount</a></p>"
                    options='<option value="">No movement selected</option>'+''.join(f"<option value='{esc(c['key'])}'>{esc(c['key'])} — {esc(c['target'])}, date {esc(c['date'])}</option>" for c in row['candidates'])
                    body+=f"<form method='post' action='/owner-entries/preview'>{hidden(ledger,'statement_match')}<input type='hidden' name='batch_id' value='{batch['id']}'><input type='hidden' name='row_id' value='{esc(row['id'])}'><label>Disposition<select name='status'><option value='unmatched'>Unmatched — investigate</option><option value='matched'>Match existing observed movement</option><option value='excluded'>Exclude this source row with reason</option></select></label><label>Same-currency/amount candidate within 7 days<select name='movement_key'>{options}</select></label>"+source_fields(ledger)+"<button type='submit'>Preview transaction review — no cash effect</button></form></div>"
                body+="<div class='card'><h3>Ledger observations without a match in this file's currency/date ranges</h3>"+''.join(f"<p>{esc(r['key'])}: {money(r['signed_minor'],r['currency'])} on {esc(r['date'])}; {web.evidence_link(ledger,r['evidence_id'])}.</p>" for r in report['ledger_movements_without_match'])+'<p>A partial CSV can omit legitimate movements; this list is not proof of missing bank activity or a reconciled bank balance.</p></div>'
            return web.page('Statement reconciliation',body,active='/statements')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/statements/import')
    def statement_import_preview(handler,req):
        fields(req,{'note'});up=req.files.get('file')
        if not up or not up['filename'].lower().endswith('.csv') or len(up['data'])>web.MAX_UPLOAD_BYTES:raise CashflowError('Upload a synthetic CSV up to 1 MiB.')
        try:
            reader=csv.DictReader(StringIO(up['data'].decode('utf-8-sig')))
            if reader.fieldnames!=['id','date','currency','amount','reference']:raise CashflowError('Use exact CSV headers/order: id,date,currency,amount,reference.')
            rows=[]
            for row in reader:
                if None in row or any(v is None for v in row.values()):raise CashflowError('Statement row has incomplete/extra columns.')
                rows.append(dict(id=row['id'].strip(),date=day(row['date'].strip()).isoformat(),currency=row['currency'].strip().upper(),
                    signed_minor=amount(row['amount'].strip(),row['currency'].strip().upper(),signed=True),reference=row['reference'].strip()))
                if len(rows)>100:raise CashflowError('Import at most 100 statement rows at a time.')
        except (UnicodeError,csv.Error):raise CashflowError('Use a valid UTF-8 statement CSV.')
        digest=hashlib.sha256(up['data']).hexdigest();note=req.field('note');revision=web.read_state(review_revision)
        web.read_state(lambda ledger:import_statement(copy.deepcopy(ledger),rows,digest,note,revision,ctx=web._owner_ctx(ledger)))
        token=web._stage(dict(kind='statement',rows=rows,digest=digest,note=note,revision=revision))
        body="<h1>Preview synthetic statement</h1><p>Source rows only. Import creates no observed payment, allocation or cash.</p>"
        body+=''.join(f"<div class='card'><h2>{esc(r['id'])}</h2><p>{money(r['signed_minor'],r['currency'])} on {esc(r['date'])}; {esc(r['reference'] or 'unknown')}.</p></div>" for r in rows)
        body+=f"<div class='card'><form method='post' action='/statements/import/confirm'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'><label><input type='checkbox' name='confirmed' value='yes' required> I reviewed these synthetic source rows. Import for transaction matching only; create no cash.</label><button type='submit'>Confirm source statement import</button></form></div>"
        return handler._send_html(200,web.page('Statement preview',body,active='/statements'))

    @web.route('POST','/statements/import/confirm')
    def statement_import_confirm(handler,req):
        fields(req,{'token'},confirmed=True);stage=web.staged().get(req.field('token'))
        if stage is None or stage.get('kind')!='statement':raise CashflowError('Statement preview expired. Upload/review again; completed imported file digests remain deduplicated.')
        result=web.mutate_state(lambda ledger:import_statement(ledger,stage['rows'],stage['digest'],stage['note'],stage['revision'],ctx=web._owner_ctx(ledger)))
        return handler._redirect('/statements?batch_id='+quote(result['batch_id'],safe=''))
