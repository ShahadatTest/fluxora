"""Local owner controls for accounting observations and recoverable state."""
from datetime import date, timedelta
import re
from urllib.parse import quote

from .accounting import correct_receipt, record_bill_payment, save_forecast, compare_forecast
from .owner_workflows import review_revision
from .settlement import effective_receipt, split_parent, split_unallocated
from .errors import CashflowError
from . import recovery


def mount(web):
    esc, money = web.esc, web.money

    def evidence(ledger):
        return '<option value="">Choose evidence checked</option>' + ''.join(
            f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:100])}</option>" for e in ledger.evidence.values())

    def hidden(ledger):
        return web.csrf_field() + f"<input type='hidden' name='revision' value='{review_revision(ledger)}'>"

    def confirmation(text):
        return f"<label><input type='checkbox' name='confirmed' value='yes' required> {esc(text)}</label>"

    def fields(req, names):
        if set(req.form) - set(names) - {'csrf','confirmed'}:
            raise CashflowError('Unexpected confirmation field. Reload the owner review.')
        if req.field('confirmed') != 'yes':
            raise CashflowError('Explicit owner confirmation is required; documents supply evidence only.')

    def day(raw):
        try: return date.fromisoformat(raw)
        except (ValueError,TypeError): raise CashflowError('Use a calendar date in YYYY-MM-DD form.')

    def amount(raw, ccy, zero=False):
        if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?', raw):
            raise CashflowError('Enter a nonnegative ordinary money amount in the shown currency.')
        result = web._parse_major_to_minor(raw, ccy)
        if result == 0 and not zero: raise CashflowError('Payment amount must be positive.')
        return result

    @web.route('GET','/corrections')
    def corrections_page(handler, req):
        def build(ledger):
            rid = req.query.get('receipt_id',[''])[0]
            original = ledger.receipts.get(rid) if rid else next(iter(ledger.receipts.values()),None)
            if rid and original is None: raise CashflowError('Unknown receipt for this business.')
            choices = ''.join(f"<option value='{esc(r.id)}' {'selected' if original and r.id == original.id else ''}>{esc(r.id)}</option>" for r in ledger.receipts.values())
            body = ("<h1>Receipt corrections &amp; reversals</h1><p>Correct recorded accounting facts with evidence. "
                "The original receipt stays immutable; compensating allocation/cash entries retain the history. "
                "This page does not initiate a refund or transfer. Zero reverses the recorded amount as an accounting correction.</p>"
                f"<div class='card'><form method='get' action='/corrections'><label>Original receipt<select name='receipt_id'>{choices}</select></label><button type='submit'>Review original and current terms</button></form></div>")
            if original:
                current = effective_receipt(ledger,original.id)
                parent = split_parent(ledger,original.id)
                ccy = ledger.receivables[original.receivable_id].currency
                if parent:
                    body += (f"<div class='card'><h2>Allocation of existing split payment {esc(parent['id'])}</h2>"
                        f"<p>This entry allocates existing cash. Correcting or reversing it changes invoice allocation and the unallocated balance only; <b>cash delta is always zero</b>. "
                        f"Original whole payment remains {money(parent['amount_minor'],ccy)}; currently unallocated {money(split_unallocated(ledger,parent),ccy)}. "
                        f"<a href='/split-receipts?payment_id={quote(parent['id'],safe='')}'>Review whole payment and allocations</a>.</p></div>")
                invoices = ''.join(f"<option value='{esc(r.id)}' {'selected' if r.id == current.receivable_id else ''}>{esc(r.id)} — remaining {money(r.remaining_minor,r.currency)}</option>" for r in ledger.receivables.values() if r.currency == ccy)
                earliest = max([ledger.as_of,current.date_received] + [day(r['date']) for r in ledger.receipt_adjustments.values() if r['receipt_id'] == original.id])
                if parent:
                    earliest = max([earliest]+[ledger.receipts[r].date_received for r in parent['allocation_ids']]+
                        [day(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']])
                value = web.format_amount(current.amount_minor,ccy).split(' ',1)[1].replace(',','')
                body += (f"<div class='card'><h2>{esc(original.id)}</h2><p><b>Original:</b> {esc(original.receivable_id)}, {money(original.amount_minor,ccy)}, {original.date_received.isoformat()}. Source: {web.evidence_link(ledger,original.evidence_id)}.</p>"
                    f"<p><b>Effective now:</b> {esc(current.receivable_id)}, {money(current.amount_minor,ccy)}. Existing action states and original approvals remain historical events.</p>"
                    f"<form method='post' action='/corrections/confirm'>{hidden(ledger)}<input type='hidden' name='receipt_id' value='{esc(original.id)}'>"
                    f"<label>Correct invoice (same currency only)<select name='invoice_id'>{invoices}</select></label>"
                    f"<label>Correct effective amount — {esc(ccy)} (zero = reversal)<input name='amount' inputmode='decimal' value='{value}' required></label>"
                    f"<label>Correction effective date<input type='date' name='date' min='{earliest.isoformat()}' value='{earliest.isoformat()}' required></label>"
                    f"<label>Correction evidence<select name='evidence_id' required>{evidence(ledger)}</select></label>"
                    "<label>What was wrong / source check<textarea name='note' maxlength='1000' required></textarea></label>" +
                    confirmation('I checked the source and authorize this accounting correction as owner. No payment/refund is being sent; the original entry/history remains.') +
                    "<button class='primary' type='submit'>Record correction and reforecast</button></form>"
                    "<p>Regular-receipt cash adjustment is corrected amount minus current effective amount. A split child allocation always has zero cash delta; whole-payment corrections have a separate owner workflow. Moving only the invoice within one currency changes no cash. "
                    "Earlier reconciliation is archived for re-review; it is not silently treated as verification of the new terms.</p></div>")
                for change in reversed(list(ledger.receipt_adjustments.values())):
                    if change['receipt_id'] == original.id:
                        body += (f"<div class='card'><h2>{esc(change['id'])}</h2><p>{esc(change['date'])}: "
                            f"{esc(change['old_invoice_id'])} {money(change['old_amount_minor'],ccy)} → {esc(change['new_invoice_id'])} {money(change['new_amount_minor'],ccy)}.</p>"
                            f"<p>Cash adjustment: {money(change['cash_delta_minor'],ccy)}. {esc(change['note'])}. Source: {web.evidence_link(ledger,change['evidence_id'])}.</p></div>")
            else: body += '<p>No recorded receipt is available to correct.</p>'
            return web.page('Receipt corrections',body + "<p><a href='/receipts'>Review effective receipts</a> · <a href='/audit/export'>Export audit</a></p>",active='/corrections')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/corrections/confirm')
    def correction_confirm(handler, req):
        fields(req,{'receipt_id','invoice_id','amount','date','evidence_id','note','revision'})
        def apply(ledger):
            original = ledger.receipts.get(req.field('receipt_id'))
            if original is None: raise CashflowError('Unknown receipt.')
            ccy = ledger.receivables[original.receivable_id].currency
            return correct_receipt(ledger,original.id,req.field('invoice_id'),amount(req.field('amount'),ccy,True),
                day(req.field('date')),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger))
        result = web.mutate_state(apply)
        return handler._redirect('/corrections?receipt_id=' + quote(result['receipt_id'],safe=''))

    @web.route('GET','/payments')
    def payments_page(handler,req):
        def build(ledger):
            wanted = req.query.get('bill_id',[''])[0]
            body = "<h1>Observed bill payments</h1><p>Record an already-observed synthetic payment, including a partial payment. "
            body += "No transfer is initiated. Original bill amounts/dates and consent requirements stay unchanged. Source IDs and payment IDs are retained.</p>"
            for out in ledger.outflows.values():
                if wanted and out.id != wanted: continue
                body += (f"<div class='card' id='bill-{esc(out.id)}'><h2>{esc(out.id)}</h2>"
                    f"<div class='kv'><span>Original bill / paid / remaining</span><b>{money(out.amount_minor,out.currency)} / {money(out.paid_minor,out.currency)} / {money(out.remaining_minor,out.currency)}</b></div>"
                    f"<p>Contractual date {out.date.isoformat()}; {'protected' if out.protected else 'scheduled terms retained'}. Source: {web.evidence_link(ledger,out.evidence_id)}.</p>")
                for item in out.observed_payments:
                    body += f"<p>{esc(item['id'])}: {money(item['amount_minor'],out.currency)} on {esc(item['date'])}; {web.evidence_link(ledger,item['evidence_id'])}. {esc(item['note'])}.</p>"
                if out.observed_payments or out.legacy_payment or out.status=='paid':
                    body+=f"<p><a href='/payment-corrections?bill_id={quote(out.id,safe='')}'>Review original/effective payments and corrections</a></p>"
                if out.remaining_minor > 0:
                    earliest_payment=max([ledger.as_of]+[date.fromisoformat(c['date']) for c in out.payment_adjustments])
                    body += (f"<form method='post' action='/payments/confirm'>{hidden(ledger)}<input type='hidden' name='bill_id' value='{esc(out.id)}'>"
                        "<label>Unique payment observation ID<input name='payment_id' pattern='[A-Za-z0-9_-]{1,100}' required></label>"
                        f"<label>Observed amount — {esc(out.currency)}<input name='amount' inputmode='decimal' required></label>"
                        f"<label>Observed date<input type='date' name='date' min='{earliest_payment.isoformat()}' value='{earliest_payment.isoformat()}' required></label>"
                        f"<label>Payment evidence<select name='evidence_id' required>{evidence(ledger)}</select></label>"
                        "<label>Source/payment note<textarea name='note' maxlength='1000' required></textarea></label>" + "<label><input type='checkbox' name='acknowledge_duplicates' value='yes'> I checked possible equal-amount/date observations and confirm a distinct movement; see Payment Warnings.</label>" +
                        confirmation('I verified that this synthetic bill payment was observed; record it once without initiating any transfer.') +
                        "<button type='submit'>Record observed bill payment</button></form>")
                body += '</div>'
            return web.page('Bill payments',body,active='/payments')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/payments/confirm')
    def payment_confirm(handler,req):
        fields(req,{'bill_id','payment_id','amount','date','evidence_id','note','revision','acknowledge_duplicates'})
        def apply(ledger):
            out = ledger.outflows.get(req.field('bill_id'))
            if out is None: raise CashflowError('Unknown bill.')
            return record_bill_payment(ledger,out.id,req.field('payment_id'),amount(req.field('amount'),out.currency),
                day(req.field('date')),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger),acknowledge_duplicates=req.field('acknowledge_duplicates')=='yes')
        result = web.mutate_state(apply)
        return handler._redirect('/payments?bill_id=' + quote(result['outflow_id'],safe=''))

    @web.route('GET','/variance')
    def variance_page(handler,req):
        def build(ledger):
            body = ("<h1>Forecast versus recorded actual</h1><p>Save the current deterministic forecast before events unfold. "
                "Saved values remain unchanged as the ledger evolves. Compare only recorded observations, separately by currency; "
                "no observed entry is not proof that no payment occurred.</p>"
                f"<div class='card'><h2>Save a forecast snapshot</h2><form method='post' action='/variance/save'>{hidden(ledger)}"
                "<label>Snapshot label<input name='label' maxlength='120' required></label>" +
                confirmation('Save the current recorded forecast and its assumptions for later comparison; no financial records are changed.') +
                "<button type='submit'>Save immutable forecast snapshot</button></form></div>")
            selected = req.query.get('snapshot_id',[''])[0]
            if ledger.forecast_snapshots:
                selected = selected or list(ledger.forecast_snapshots)[-1]
                snap = ledger.forecast_snapshots.get(selected)
                if snap is None: raise CashflowError('Unknown saved forecast.')
                end = day(snap['as_of']) + timedelta(days=snap['horizon_days']-1)
                through = day(req.query.get('through',[min(max(ledger.as_of,day(snap['as_of'])),end).isoformat()])[0])
                options = ''.join(f"<option value='{esc(s['id'])}' {'selected' if s['id'] == selected else ''}>{esc(s['label'])} — {esc(s['as_of'])}</option>" for s in ledger.forecast_snapshots.values())
                body += (f"<div class='card'><form method='get' action='/variance'><label>Saved snapshot<select name='snapshot_id'>{options}</select></label>"
                    f"<label>Virtual evaluation date (recorded events through this date)<input type='date' name='through' min='{snap['as_of']}' max='{end.isoformat()}' value='{through.isoformat()}' required></label>"
                    "<button type='submit'>Compare saved forecast with recorded events</button></form></div>")
                report = compare_forecast(ledger,selected,through,ctx=web._owner_ctx(ledger))
                for ccy, rows in report['currencies'].items():
                    last = rows[-1]
                    body += (f"<div class='card'><h2>{esc(ccy)} — {esc(snap['label'])}</h2>"
                        f"<p>Comparison uses book cash; recorded cash is not reduced by restrictions. Saved restricted amount: {money(snap['currencies'][ccy].get('restricted_at_save_minor',0),ccy)}. Decision forecast balances are shown separately in Overview / Cash availability.</p><div class='kv stack'><span>Saved forecast book closing / recorded book closing / difference</span><b>{money(last['closing_minor'],ccy)} / {money(last['recorded_closing_minor'],ccy)} / {money(last['difference_minor'],ccy)}</b></div>"
                        "<table><thead><tr><th>Date</th><th>Forecast inflow</th><th>Observed receipts</th><th>Receipt corrections</th><th>Forecast outflow</th><th>Observed paid</th><th>Bill payment corrections (signed cash)</th><th>Other observed cash / corrections (signed)</th><th>Snapshot variance</th><th>Forecast close</th><th>Recorded close</th><th>Evidence</th></tr></thead><tbody>")
                    for row in rows:
                        body += '<tr><td>' + row['date'] + '</td>' + ''.join(f"<td>{money(row[key],ccy)}</td>" for key in
                            ['inflow_minor','recorded_receipts_minor','receipt_corrections_minor','outflow_minor','recorded_paid_minor','bill_payment_corrections_minor','other_cash_minor','snapshot_variance_minor','closing_minor','recorded_closing_minor']) + '<td>' + ', '.join(web.evidence_link(ledger,e) for e in row['evidence_ids']) + '</td></tr>'
                    body += '</tbody></table></div>'
                body += '<div class="card"><h2>Invoice expectations frozen at capture</h2>'
                for item in report['invoices']:
                    body += f"<p><b>{esc(item['id'])}</b>: expected {money(item['remaining_minor'],item['currency'])} on {esc(item['expected_date'] or 'unknown')}; recorded net allocation in comparison window {money(item['recorded_net_allocation_minor'],item['currency'])}. Source: {web.evidence_link(ledger,item['evidence_id'])}; date source: {web.evidence_link(ledger,item['date_evidence_id'])}.</p>"
                body += '<p>' + esc(report['note']) + '</p></div>'
            else: body += '<p>No forecast saved yet. Save one before recording later events.</p>'
            return web.page('Forecast variance',body,active='/variance')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/variance/save')
    def variance_save(handler,req):
        fields(req,{'label','revision'})
        result = web.mutate_state(lambda ledger: save_forecast(ledger,req.field('label'),req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/variance?snapshot_id=' + result['snapshot_id'])

    @web.route('GET','/backups')
    def backups_page(handler,req):
        def build(ledger):
            backups = recovery.list_backups(web.database_path(),ctx=web._owner_ctx(ledger))
            cards = ''.join(f"<div class='card'><h2>{esc(b['label'])}</h2><p>{esc(b['business_name'])} ({esc(b['business_id'])}); snapshot {esc(b['as_of'])}; saved {esc(b['created_at'])}. {b['bytes']} bytes.</p>"
                f"<a class='btn' href='/backups/{b['id']}/preview'>Preview restore</a> <a class='btn' href='/backups/{b['id']}/download'>Download local SQLite recovery copy</a></div>" for b in backups)
            return web.page('Backup and restore',"<h1>Local backup &amp; restore</h1>"
                "<p>Recovery copies are local SQLite files with version/checksum manifests. Keep them private: they contain the synthetic ledger and saved agent history. "
                "Checksums detect corruption; they are not protection against someone who can edit your local files. Credentials are not stored by the application.</p>"
                f"<div class='card'><form method='post' action='/backups/create'>{web.csrf_field()}<label>Backup label<input name='label' maxlength='200' required></label>" +
                confirmation('Create a local recovery copy of the current database; no external upload or deployment.') +
                "<button type='submit'>Create local backup</button></form></div>" + cards +
                "<p>Automatic copies are retained before fixture replacement, reset and restore. Restore first shows the business/date/cash/count changes and requires a typed target business ID. "
                "A pre-restore copy retains the active state. The supported UI restores only its own inventoried recovery files; arbitrary paths/uploads are not accepted.</p>",active='/backups')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/backups/create')
    def backup_create(handler,req):
        fields(req,{'label'})
        with web.LOCK:
            web.read_state(lambda ledger: recovery.create_backup(web.database_path(),req.field('label'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/backups')

    @web.route('GET','/backups/<backup_id>/preview')
    def backup_preview(handler,req,backup_id):
        def build(ledger):
            report = recovery.restore_preview(web.database_path(),backup_id,ctx=web._owner_ctx(ledger))
            body = f"<h1>Preview local restore</h1><div class='card'><h2>{esc(report['backup']['label'])}</h2>"
            for heading,key in [('Current active state','current'),('State after restore','restored')]:
                item = report[key]
                body += f"<h3>{heading}</h3><p>{esc(item['business_name'])} ({esc(item['business_id'])}); as of {esc(item['as_of'])}. Receipts {item['receipts']}; actions {item['actions']}; corrections {item['corrections']}; partial-payment records {item['bill_payments']}; forecasts {item['forecasts']}.</p>"
                body += ''.join(f"<div class='kv'><span>Observed cash {esc(ccy)}</span><b>{money(value,ccy)}</b></div>" for ccy,value in item['cash'].items())
            body += f"<p>Receipt IDs removed from active state: {esc(', '.join(report['removed_receipt_ids']) or 'none')}.</p><p>Action IDs removed: {esc(', '.join(report['removed_action_ids']) or 'none')}.</p>"
            body += '<p>Recurring templates / generated occurrences / duplicate reviews / contractual due dates: ' + ' → '.join(
                ' / '.join(str(report[key][field]) for field in ['recurring_templates','recurring_occurrences','duplicate_reviews','due_dates'])
                for key in ['current','restored']) + ' (current → restored).</p>'
            body += '<p>Bill payment corrections / active collection holds: ' + ' → '.join(
                str(report[key]['payment_corrections'])+' / '+str(report[key]['active_collection_holds']) for key in ['current','restored'])+' (current → restored).</p>'
            body += '<p>Split payments / source checks: ' + ' → '.join(
                str(report[key]['split_payments'])+' / '+str(report[key]['source_checks']) for key in ['current','restored'])+' (current → restored).</p>'
            body += '<p>Source amount adjustments / whole-payment corrections / other cash / statement files / statement reviews: ' + ' → '.join(
                ' / '.join(str(report[key][field]) for field in ['source_adjustments','whole_payment_corrections','other_cash_movements','statement_batches','statement_reviews']) for key in ['current','restored'])+' (current → restored).</p>'
            body += '<p class="warn">' + esc(report['warning']) + '</p>'
            if report['cross_business']: body += '<p class="warn">This replaces the current synthetic business with a different saved business. The local owner must explicitly acknowledge the target below.</p>'
            body += (f"<form method='post' action='/backups/{backup_id}/restore'>{web.csrf_field()}<input type='hidden' name='revision' value='{report['revision']}'>"
                f"<label>Type target business ID exactly: {esc(report['restored']['business_id'])}<input name='target_business_id' autocomplete='off' required></label>" +
                confirmation('I reviewed the changes and authorize this local restore, including discarded later active entries. A pre-restore recovery copy must be retained.') +
                "<button class='danger' type='submit'>Confirm reviewed restore</button></form></div>")
            return web.page('Preview restore',body,active='/backups')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/backups/<backup_id>/restore')
    def backup_restore(handler,req,backup_id):
        fields(req,{'revision','target_business_id'})
        with web.LOCK:
            web.read_state(lambda ledger: recovery.restore_backup(web.database_path(),backup_id,req.field('revision'),req.field('target_business_id'),ctx=web._owner_ctx(ledger)))
            web.staged().clear()  # previews from the discarded state no longer apply
        return handler._redirect('/today')

    @web.route('GET','/backups/<backup_id>/download')
    def backup_download(handler,req,backup_id):
        def fetch(ledger):
            from .authz import require_access
            require_access(web._owner_ctx(ledger),ledger)
            path, info, _ = recovery.checked_backup(web.database_path(),backup_id)
            return path.read_bytes()
        data = web.read_state(fetch)
        handler.send_response(200); handler.send_header('Content-Type','application/vnd.sqlite3')
        handler.send_header('Content-Disposition',f'attachment; filename="cashflow-recovery-{backup_id}.sqlite"')
        handler.send_header('Content-Length',str(len(data))); handler.end_headers(); handler.wfile.write(data)
