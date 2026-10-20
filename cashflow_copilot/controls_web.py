"""Owner accounting/dispute controls and read-only financial downloads."""
from datetime import date
import re
from urllib.parse import quote

from .accounting import correct_bill_payment, set_collection_hold
from .errors import CashflowError
from .owner_workflows import review_revision
from .settlement import bill_payment_entries, effective_bill_payment, collection_held
from .reports import report_tables, xlsx_report, csv_report, csv_archive, Money


def mount(web):
    esc,money=web.esc,web.money
    def hidden(ledger):return web.csrf_field()+f"<input type='hidden' name='revision' value='{review_revision(ledger)}'>"
    def sources(ledger):return '<option value="">Choose checked source</option>'+''.join(f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:90])}</option>" for e in ledger.evidence.values())
    def confirm(text):return f"<label><input type='checkbox' name='confirmed' value='yes' required> {esc(text)}</label>"
    def fields(req,names):
        if set(req.form)-set(names)-{'csrf','confirmed'} or req.field('confirmed')!='yes':raise CashflowError('Review the owner form and explicitly confirm using only its fields.')
    def day(raw):
        try:return date.fromisoformat(raw)
        except (ValueError,TypeError):raise CashflowError('Use a calendar date in YYYY-MM-DD format.')

    @web.route('GET','/payment-corrections')
    def payment_corrections(handler,req):
        def build(ledger):
            eligible=[o for o in ledger.outflows.values() if bill_payment_entries(o)]
            bid=req.query.get('bill_id',[eligible[0].id if eligible else ''])[0]
            out=ledger.outflows.get(bid) if bid else None
            if bid and (out is None or not bill_payment_entries(out)):raise CashflowError('Choose a bill with an original observed payment.')
            options=''.join(f"<option value='{esc(o.id)}' {'selected' if o.id==bid else ''}>{esc(o.id)}</option>" for o in eligible)
            body=("<h1>Bill payment corrections &amp; reversals</h1><p>Correct an accounting observation amount, or use zero for reversal. "
                "The original payment stays in history; a new dated adjustment changes effective paid, remaining payable and recorded cash. "
                "No refund or transfer is executed. Bill currency and protected/contractual dates stay unchanged.</p>"
                f"<div class='card'><form method='get' action='/payment-corrections'><label>Bill with observed payment<select name='bill_id'>{options}</select></label><button type='submit'>Review observations</button></form></div>")
            if out:
                entries=bill_payment_entries(out);pid=req.query.get('payment_id',[entries[0]['id']])[0]
                original=next((p for p in entries if p['id']==pid),None)
                if original is None:raise CashflowError('Choose an original payment ID for this bill.')
                body+="<div class='card'><h2>Original observations</h2>"+''.join(
                    f"<p><a href='/payment-corrections?bill_id={quote(out.id,safe='')}&amp;payment_id={quote(p['id'],safe='')}'>{esc(p['id'])}</a>: {money(p['amount_minor'],out.currency)} on {esc(p['date'])}; {web.evidence_link(ledger,p['evidence_id'])}.</p>" for p in entries)+'</div>'
                current=effective_bill_payment(out,pid,ledger)
                earliest=max([ledger.as_of,day(current['date'])]+[day(c['date']) for c in out.payment_adjustments if c['payment_id']==pid])
                value=web.format_amount(current['amount_minor'],out.currency).split(' ',1)[1].replace(',','')
                body+=(f"<div class='card'><h2>{esc(pid)}</h2><p>Original {money(original['amount_minor'],out.currency)} on {esc(original['date'])}; effective now {money(current['amount_minor'],out.currency)} on {esc(current['date'])}. <a href='/payment-dates'>Review payment date separately</a>.</p>"
                    f"<div class='kv stack'><span>Bill / effective paid / remaining payable</span><b>{money(out.amount_minor,out.currency)} / {money(out.paid_minor,out.currency)} / {money(out.remaining_minor,out.currency)}</b></div>"
                    f"<form method='post' action='/payment-corrections/confirm'>{hidden(ledger)}<input type='hidden' name='bill_id' value='{esc(out.id)}'><input type='hidden' name='payment_id' value='{esc(pid)}'>"
                    f"<label>Corrected amount — {esc(out.currency)} (zero = accounting reversal)<input name='amount' inputmode='decimal' value='{value}' required></label>"
                    f"<label>Correction effective date<input type='date' name='date' min='{earliest.isoformat()}' value='{earliest.isoformat()}' required></label>"
                    f"<label>Checked correction source<select name='evidence_id' required>{sources(ledger)}</select></label>"
                    "<label>What was wrong / source check<textarea name='note' maxlength='1000' required></textarea></label>"+
                    confirm('I checked the original and source and authorize this accounting correction. It sends no payment/refund and preserves the original history.')+
                    "<button type='submit'>Record correction and reforecast</button></form></div>")
                for change in out.payment_adjustments:
                    body+=f"<div class='card'><h3>{esc(change['id'])}</h3><p>{esc(change['payment_id'])} on {esc(change['date'])}: {money(change['old_amount_minor'],out.currency)} → {money(change['new_amount_minor'],out.currency)}; signed cash correction {money(change['cash_delta_minor'],out.currency)}. {web.evidence_link(ledger,change['evidence_id'])}. {esc(change['note'])}.</p></div>"
            else:body+='<p>No observed bill payments yet. Record an observed synthetic payment in Bill Payments before reviewing a correction.</p>'
            return web.page('Bill payment corrections',body,active='/payment-corrections')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/payment-corrections/confirm')
    def payment_correction_confirm(handler,req):
        fields(req,{'bill_id','payment_id','amount','date','evidence_id','note','revision'})
        def apply(ledger):
            out=ledger.outflows.get(req.field('bill_id'))
            if out is None:raise CashflowError('Unknown bill.')
            raw=req.field('amount')
            if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?',raw):raise CashflowError('Enter a nonnegative ordinary amount in this bill currency.')
            return correct_bill_payment(ledger,out.id,req.field('payment_id'),web._parse_major_to_minor(raw,out.currency),day(req.field('date')),
                req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger))
        result=web.mutate_state(apply)
        return handler._redirect('/payment-corrections?bill_id='+quote(result['outflow_id'],safe='')+'&payment_id='+quote(result['payment_id'],safe=''))

    @web.route('GET','/holds')
    def holds_page(handler,req):
        def build(ledger):
            body=("<h1>Disputed invoices &amp; collection holds</h1><p>Hold a disputed remaining collection with evidence and a reason. "
                "The invoice, original amount, dates and already-observed receipts remain. Held balances are excluded from forecast inflow and new collection requests. "
                "Only a separate owner resolution releases a hold; release creates no payment promise or receipt.</p>")
            wanted=req.query.get('invoice_id',[''])[0]
            if wanted and wanted not in ledger.receivables:raise CashflowError('Unknown invoice for this business.')
            for rec in ledger.receivables.values():
                if wanted and rec.id!=wanted:continue
                if rec.remaining_minor<=0 and not rec.collection_hold:continue
                held=collection_held(rec)
                body+=(f"<div class='card' id='invoice-{esc(rec.id)}'><h2>{esc(rec.id)}</h2><p><b>{'HELD — remaining collection excluded' if held else 'Collection eligible under recorded expectations'}</b></p>"
                    f"<div class='kv'><span>Original / remaining receivable</span><b>{money(rec.original_minor,rec.currency)} / {money(rec.remaining_minor,rec.currency)}</b></div>"
                    f"<p>Expected collection {esc(rec.expected_date.isoformat() if rec.expected_date else 'unknown')}; contractual due {esc(rec.due_date.isoformat() if rec.due_date else 'unknown')}. Original source {web.evidence_link(ledger,rec.evidence_id)}.</p>")
                if rec.collection_hold:body+=f"<p>Last source decision: {web.evidence_link(ledger,rec.collection_hold['evidence_id'])}; {esc(rec.collection_hold['note'])}.</p>"
                if held or rec.remaining_minor>0:
                    body+=(f"<form method='post' action='/holds/confirm'>{hidden(ledger)}<input type='hidden' name='invoice_id' value='{esc(rec.id)}'><input type='hidden' name='active' value='{'no' if held else 'yes'}'>"
                        f"<label>{'Resolution evidence' if held else 'Dispute / hold evidence'}<select name='evidence_id' required>{sources(ledger)}</select></label>"
                        f"<label>{'What resolved the hold' if held else 'Why collection is disputed / on hold'}<textarea name='note' maxlength='1000' required></textarea></label>"+
                        confirm('I checked the source and authorize this collection-status change as owner. It grants no consent, write-off or cash.')+
                        f"<button type='submit'>{'Release collection hold' if held else 'Place remaining collection on hold'}</button></form>")
                for operation in ledger.owner_operations.values():
                    p=operation['payload']
                    if operation['kind']=='collection_hold_changed' and p['invoice_id']==rec.id:
                        body+=f"<p class='mut'>History: {'hold' if p['active'] else 'release'}; source {web.evidence_link(ledger,p['evidence_id'])}; {esc(p['note'])}.</p>"
                body+=f"<p><a href='/facts?invoice_id={quote(rec.id,safe='')}'>Review expected collection date after resolution</a></p></div>"
            return web.page('Collection holds',body,active='/holds')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/holds/confirm')
    def holds_confirm(handler,req):
        fields(req,{'invoice_id','active','evidence_id','note','revision'})
        if req.field('active') not in {'yes','no'}:raise CashflowError('Choose explicit hold or release.')
        result=web.mutate_state(lambda ledger:set_collection_hold(ledger,req.field('invoice_id'),req.field('active')=='yes',req.field('evidence_id'),
            req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/holds?invoice_id='+quote(result['invoice_id'],safe=''))

    def download(handler,data,mime,filename):
        handler.send_response(200);handler.send_header('Content-Type',mime)
        handler.send_header('Content-Disposition',f'attachment; filename="{filename}"');handler.send_header('Content-Length',str(len(data)))
        handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Cache-Control','no-store')
        handler.end_headers();handler.wfile.write(data)

    @web.route('GET','/reports')
    def reports_page(handler,req):
        def build(ledger):
            report=report_tables(ledger,ctx=web._owner_ctx(ledger))
            body=("<h1>Financial report exports</h1><p>Download a static deterministic snapshot with separate currencies, recorded cash history, accounting corrections, remaining claims, obligations and forecast. "
                "Held collections stay outstanding and are excluded from planned inflow. No bank verification or live-model prose is included.</p>"
                f"<div class='card'><h2>{esc(ledger.business_name)}</h2><p>Ledger snapshot: {ledger.as_of.isoformat()}. Each download captures the current state under the local read lock.</p>"
                "<a class='btn' href='/reports/workbook.xlsx'>Download Excel workbook</a> <a class='btn' href='/reports/csv.zip'>Download all CSV tables</a></div>")
            if report['warnings']:body+="<div class='card'><h2>Source issues to review</h2>"+''.join('<p class="warn">'+esc(w)+'</p>' for w in report['warnings'])+'</div>'
            table=report['tables'][0]
            def display(v):
                if isinstance(v,Money):return money(v.minor,v.currency)
                if type(v) is date:return v.isoformat()
                return esc(v if v is not None else 'unknown')
            body+='<div class="card"><h2>Current financial summary</h2><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in table['headers'])+'</tr></thead><tbody>'
            body+=''.join('<tr>'+''.join('<td>'+display(v)+'</td>' for v in row)+'</tr>' for row in table['rows'])+'</tbody></table></div>'
            for table in report['tables']:
                name=table['name'].lower().replace(' ','-')
                body+=f"<div class='card'><h2>{esc(table['name'])}</h2><p>{esc(table['note'])}</p><a class='btn' href='/reports/csv/{name}'>Download this CSV table</a></div>"
            body+='<p>'+esc(report['note'])+'</p><p>CSV text beginning with spreadsheet formula markers is prefixed with an apostrophe. Excel strings are literal cells. Numeric money and dates retain their types; exact amounts beyond Excel precision are text.</p>'
            return web.page('Financial reports',body,active='/reports')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/reports/workbook.xlsx')
    def report_workbook(handler,req):
        data=web.read_state(lambda ledger:xlsx_report(report_tables(ledger,ctx=web._owner_ctx(ledger))))
        return download(handler,data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','cashflow-financial-report.xlsx')

    @web.route('GET','/reports/csv.zip')
    def report_csv_archive(handler,req):
        data=web.read_state(lambda ledger:csv_archive(report_tables(ledger,ctx=web._owner_ctx(ledger))))
        return download(handler,data,'application/zip','cashflow-financial-csv.zip')

    @web.route('GET','/reports/csv/<name>')
    def report_csv_table(handler,req,name):
        def build(ledger):
            report=report_tables(ledger,ctx=web._owner_ctx(ledger))
            table=next((t for t in report['tables'] if t['name'].lower().replace(' ','-')==name),None)
            if table is None:raise CashflowError('Unknown financial report table.')
            return csv_report(report,table['name'])
        return download(handler,web.read_state(build),'text/csv; charset=utf-8','cashflow-'+name+'.csv')
