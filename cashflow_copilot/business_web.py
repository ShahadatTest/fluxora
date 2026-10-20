"""Local purchase previews, explicit split allocation and source-age review."""
from datetime import date, timedelta
import re
from urllib.parse import quote

from .business_tools import affordability, data_freshness, confirm_fact_check, confirm_split_observation, allocate_split_balance
from .errors import CashflowError
from .owner_workflows import review_revision, observation_candidates
from .settlement import effective_receipt, split_unallocated, effective_split_payment


def mount(web):
    esc, money = web.esc, web.money

    def hidden(ledger):
        return web.csrf_field() + f"<input type='hidden' name='revision' value='{review_revision(ledger)}'>"

    def sources(ledger):
        return '<option value="">Choose checked source</option>' + ''.join(
            f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:90])}</option>" for e in ledger.evidence.values())

    def confirm(text):
        return f"<label><input type='checkbox' name='confirmed' value='yes' required> {esc(text)}</label>"

    def fields(req, allowed, confirmation=True):
        if set(req.form) - set(allowed) - {'csrf', 'confirmed'} or any(len(v) != 1 for v in req.form.values()):
            raise CashflowError('Unexpected or repeated form field. Reload the current review.')
        if confirmation and req.field('confirmed') != 'yes':
            raise CashflowError('Explicit owner confirmation is required. Documents supply evidence, never authority.')

    def day(raw):
        try:
            return date.fromisoformat(raw)
        except (ValueError, TypeError):
            raise CashflowError('Use a calendar date in YYYY-MM-DD format.')

    def amount(raw, currency):
        if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?', raw):
            raise CashflowError('Enter an ordinary nonnegative money amount in the displayed currency.')
        return web._parse_major_to_minor(raw, currency)

    @web.route('GET', '/affordability')
    def affordability_form(handler, req):
        def build(ledger):
            options = ''.join(f"<option value='{esc(a.currency)}'>{esc(a.currency)}</option>" for a in ledger.accounts.values())
            last = ledger.as_of + timedelta(days=ledger.horizon_days-1)
            body = ("<h1>Can I afford a new expense?</h1><p>Preview a hypothetical purchase against the current deterministic cash forecast. "
                "Recorded opening, expected collections and existing obligations keep their separate meanings. "
                "This creates no bill, draft, approval or payment.</p>"
                f"<div class='card'><h2>Purchase assumptions</h2><p>Snapshot {ledger.as_of.isoformat()}; window ends {last.isoformat()}. No FX or financing is assumed.</p>"
                f"<form method='post' action='/affordability/preview'>{web.csrf_field()}"
                "<label>What are you considering?<input name='label' maxlength='120' placeholder='For example, new stock' required></label>"
                f"<label>Currency<select name='currency'>{options}</select></label>"
                "<label>Expense amount<input name='amount' inputmode='decimal' required></label>"
                f"<label>Assumed payment date<input type='date' name='date' min='{ledger.as_of.isoformat()}' max='{last.isoformat()}' value='{ledger.as_of.isoformat()}' required></label>"
                "<button type='submit'>Compare purchase with current forecast</button></form></div>"
                "<p><a href='/freshness'>Check whether the underlying sources need review</a></p>")
            return web.page('Affordability check', body, active='/affordability')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/affordability/preview')
    def affordability_preview(handler, req):
        fields(req, {'label', 'currency', 'amount', 'date'}, confirmation=False)
        def build(ledger):
            result = affordability(ledger, req.field('currency'), amount(req.field('amount'), req.field('currency')),
                day(req.field('date')), req.field('label'), ctx=web._owner_ctx(ledger))
            ccy = result['currency']
            before, after = result['baseline'].currencies[ccy], result['scenario'].currencies[ccy]
            messages = dict(floor_breach='Cash floor breach in this scenario — review the gap before any spending decision.',
                source_review_required='Numerically within the cash floor; source review is required before relying on this preview.',
                within_floor_under_assumptions='Within the cash floor under recorded assumptions for this window. This is not payment authorization.')
            body = (f"<h1>Purchase affordability preview</h1><div class='card'><h2>{esc(result['label'])}</h2>"
                f"<p>{money(result['amount_minor'],ccy)} on {result['purchase_date'].isoformat()}.</p><p><b>{esc(messages[result['status']])}</b></p>"
                + ("<p class='warn'>The baseline already has a breach before considering this purchase.</p>" if result['baseline_already_breached'] else '') +
                f"<div class='kv stack'><span>Additional shortfall caused by this expense</span><b>{money(result['additional_shortfall_minor'],ccy)}</b></div>"
                f"<div class='kv stack'><span>Numerical headroom from purchase date (source assumptions still apply)</span><b>{money(result['numerical_headroom_minor'],ccy)}</b></div></div>"
                "<div class='card'><h2>Before and after</h2><table><thead><tr><th>Measure</th><th>Current forecast</th><th>With hypothetical expense</th></tr></thead><tbody>"
                f"<tr><td>Lowest intraday balance</td><td>{money(before.min_balance_minor,ccy)}</td><td>{money(after.min_balance_minor,ccy)}</td></tr>"
                f"<tr><td>First floor breach</td><td>{before.first_breach_date.isoformat() if before.first_breach_date else 'none in window'}</td><td>{after.first_breach_date.isoformat() if after.first_breach_date else 'none in window'}</td></tr>"
                f"<tr><td>Shortfall against floor</td><td>{money(before.worst_shortfall_minor,ccy)}</td><td>{money(after.worst_shortfall_minor,ccy)}</td></tr>"
                f"<tr><td>Window closing balance</td><td>{money(before.closing_minor,ccy)}</td><td>{money(after.closing_minor,ccy)}</td></tr></tbody></table></div>")
            body += "<div class='card'><h2>Protected obligations remain scheduled</h2>"
            body += ''.join(f"<p>{esc(o['id'])}: {money(o['remaining_minor'],ccy)} due {esc(o['date'])}; scenario low on reserved day {money(o['scenario_intraday_low_minor'],ccy)}. {web.evidence_link(ledger,o['evidence_id'])}.</p>" for o in result['protected']) or '<p>No unpaid protected obligation in this currency/window.</p>'
            body += '</div>'
            if result['issues']:
                body += "<div class='card'><h2>Source limitations to review</h2>" + ''.join('<p class="warn">'+esc(s)+'</p>' for s in result['issues']) + '</div>'
            body += "<div class='card'><h2>Daily effect — " + esc(ccy) + "</h2><table><thead><tr><th>Date</th><th>Current low</th><th>Scenario low</th><th>Scenario closing</th><th>Floor breach</th></tr></thead><tbody>"
            body += ''.join(f"<tr><td>{d.date.isoformat()}</td><td>{money(b.intraday_low_minor,ccy)}</td><td>{money(d.intraday_low_minor,ccy)}</td><td>{money(d.closing_minor,ccy)}</td><td>{'yes' if d.floor_breached else 'no'}</td></tr>" for b,d in zip(before.days,after.days))
            body += '</tbody></table></div><p>'+esc(result['note'])+"</p><p>Other currency balances are unchanged. <a href='/affordability'>Try another expense</a> · <a href='/freshness'>Review source freshness</a></p>"
            return web.page('Purchase preview', body, active='/affordability')
        return handler._send_html(200, web.read_state(build))

    @web.route('GET', '/freshness')
    def freshness_page(handler, req):
        def build(ledger):
            raw = req.query.get('reference_date', [''])[0]
            report = data_freshness(ledger, day(raw) if raw else None, ctx=web._owner_ctx(ledger))
            body = ("<h1>Data freshness &amp; source checks</h1><p>See when the owner last checked each current opening cash snapshot and outstanding collection expectation. "
                "An imported document or fixture is not an owner verification. A source check changes no money, dates, consent or hold.</p>"
                f"<div class='card'><h2>Displayed reference date: {esc(report['reference_date'])}</h2><p>Ledger snapshot: {esc(report['ledger_as_of'])}. Source checks below are recorded for the ledger business date.</p>"
                f"<form method='get' action='/freshness'><label>Evaluate review age at this date<input type='date' name='reference_date' value='{esc(report['reference_date'])}' min='{ledger.as_of.isoformat()}' max='{(ledger.as_of+timedelta(days=366)).isoformat()}' required></label><button type='submit'>Show review age</button></form></div>")
            for row in report['rows']:
                check = row['review']
                ident, kind = row['id'], row['kind']
                amount_minor = row['amount_minor'] if kind == 'cash' else row['remaining_minor']
                body += (f"<div class='card'><h2>{'Opening cash' if kind=='cash' else 'Collection expectation'} — {esc(ident)}</h2>"
                    f"<p><b>{esc(row['status'].replace('_',' ').upper())}</b>{' · HELD: no assumed collection' if row['held'] else ''}</p>"
                    f"<div class='kv stack'><span>{'Opening snapshot amount' if kind=='cash' else 'Outstanding invoice amount'}</span><b>{money(amount_minor,row['currency'])}</b></div>"
                    f"<p>Current source: {web.evidence_link(ledger,row['evidence_id']) if row['evidence_id'] else 'unknown'}. "
                    + (f"Expected collection: {esc(row['expected_date'] or 'unknown')}. " if kind == 'collection' else f"Opening date: {esc(row['as_of'])}. ") + '</p>')
                if check:
                    body += f"<p>Last checked for business date {esc(check['reviewed_on'])}; age {row['age_days']} calendar days; owner {esc(check['actor'])}. Source {web.evidence_link(ledger,check['evidence_id'])}. {esc(check['note'])}.</p>"
                else:
                    body += '<p>No explicit owner source check is recorded for this fact.</p>'
                if kind == 'collection' and (not row['expected_date'] or day(row['expected_date']) < ledger.as_of):
                    body += f"<p><a href='/facts?invoice_id={quote(ident,safe='')}'>Resolve the expected date before confirming its source</a>.</p>"
                else:
                    body += (f"<form method='post' action='/freshness/confirm'>{hidden(ledger)}<input type='hidden' name='kind' value='{kind}'><input type='hidden' name='id' value='{esc(ident)}'>"
                        f"<label>Source checked<select name='evidence_id' required>{sources(ledger)}</select></label>"
                        "<label>What did you verify?<textarea name='note' maxlength='1000' required></textarea></label>" +
                        confirm('I checked this exact current fact against the source as owner. This is an attestation, not independent bank verification, a payment or consent.') +
                        f"<button type='submit'>Confirm source checked for {ledger.as_of.isoformat()}</button></form>")
                body += '</div>'
            body += '<p>'+esc(report['note'])+'</p>'
            return web.page('Data freshness', body, active='/freshness')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/freshness/confirm')
    def freshness_confirm(handler, req):
        fields(req, {'kind','id','evidence_id','note','revision'})
        web.mutate_state(lambda ledger: confirm_fact_check(ledger, req.field('kind'), req.field('id'),
            req.field('evidence_id'), req.field('note'), req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/freshness')

    def allocation_inputs(ledger, currency):
        body = '<h3>Explicit invoice allocations</h3><p>Enter amounts for up to 50 invoices. Blank or zero leaves that invoice unchanged. The remainder stays unallocated; identities must be checked against the source.</p>'
        for rec in ledger.receivables.values():
            if rec.currency == currency and rec.remaining_minor > 0:
                body += (f"<label>{esc(rec.id)} — remaining {money(rec.remaining_minor,currency)}{' · HELD (observed allocation allowed)' if rec.collection_hold and rec.collection_hold['active'] else ''}"
                    f"<input name='allocate-{esc(rec.id)}' inputmode='decimal' placeholder='Amount in {esc(currency)}'></label>"
                    f"<p class='mut'>Customer {esc(rec.counterparty or 'unknown')}; reference {esc(rec.invoice_reference or 'unknown')}; {web.evidence_link(ledger,rec.evidence_id)}.</p>")
        return body

    @web.route('GET', '/split-receipts')
    def split_receipts_page(handler, req):
        def build(ledger):
            body = ("<h1>Split one observed payment across invoices</h1><p>Start with an imported source observation from Receipt Review. "
                "Only explicit owner confirmation records its whole cash amount once. Allocations reduce invoice balances; they create no extra cash, counterparty consent or automatic reconciliation. "
                "Excess remains an unallocated customer receipt, not revenue or an FX conversion.</p>"
                "<p><a href='/receipts'>Import synthetic observations</a> · <a href='/receipts/inbox'>Inspect observation inbox</a></p>")
            available = [r for r in ledger.receipt_inbox.values() if r['status'] == 'unallocated']
            options = ''.join(f"<option value='{esc(r['id'])}'>{esc(r['id'])} — {money(r['amount_minor'],r['currency'])}</option>" for r in available)
            body += f"<div class='card'><form method='get' action='/split-receipts'><label>Unconfirmed observation<select name='observation_id'><option value=''>Choose source observation</option>{options}</select></label><button type='submit'>Review payment and allocation</button></form></div>"
            oid = req.query.get('observation_id',[''])[0]
            if oid:
                info = observation_candidates(ledger, oid, ctx=web._owner_ctx(ledger))
                row = info['observation']
                if row['status'] != 'unallocated':
                    raise CashflowError('This observation has already been handled. Inspect the recorded payment below.')
                body += (f"<div class='card'><h2>Confirm observed payment — {esc(oid)}</h2><p>{money(row['amount_minor'],row['currency'])} on {esc(row['date'])}; "
                    f"source {web.evidence_link(ledger,row['evidence_id'])}. Imported reference {esc(row['invoice_reference'] or 'unknown')}.</p>"
                    f"<form method='post' action='/split-receipts/confirm'>{hidden(ledger)}<input type='hidden' name='observation_id' value='{esc(oid)}'>"
                    + allocation_inputs(ledger,row['currency']))
                if info['possible_duplicate_ids']:
                    body += '<p class="warn">Possible duplicate IDs: '+', '.join(esc(s) for s in info['possible_duplicate_ids'])+'</p><label><input type="checkbox" name="acknowledge_duplicates" value="yes" required> I checked these possible duplicates against the source.</label>'
                body += ("<label>Observed payment / allocation source checks<textarea name='note' maxlength='1000' required></textarea></label>" +
                    confirm('I verified this whole payment was observed and the chosen invoice allocations are correct. Record this cash once; retain any excess as unallocated.') +
                    "<button type='submit'>Confirm observed payment and allocations</button></form></div>")
            wanted = req.query.get('payment_id',[''])[0]
            if wanted and wanted not in ledger.split_receipts:
                raise CashflowError('Unknown split payment for this business.')
            for parent in ledger.split_receipts.values():
                if wanted and wanted != parent['id']:
                    continue
                free = split_unallocated(ledger,parent)
                current_parent = effective_split_payment(ledger,parent)
                body += (f"<div class='card' id='payment-{esc(parent['id'])}'><h2>Recorded payment — {esc(parent['id'])}</h2>"
                    f"<p>Original observed total {money(parent['amount_minor'],parent['currency'])} on {esc(parent['date'])}; source {web.evidence_link(ledger,parent['evidence_id'])}.</p>"
                    f"<div class='kv stack'><span>Unallocated existing cash (not another expected receipt)</span><b>{money(free,parent['currency'])}</b></div>"
                    f"<p>Current effective whole amount {money(current_parent['amount_minor'],parent['currency'])}. <a href='/split-payment-corrections'>Review whole-payment correction</a>. Allocation corrections below change no cash.</p>")
                for rid in parent['allocation_ids']:
                    original = ledger.receipts[rid]
                    current = effective_receipt(ledger,rid)
                    body += (f"<p>Allocation {esc(rid)}: original {money(original.amount_minor,parent['currency'])} → effective {money(current.amount_minor,parent['currency'])} "
                        f"to {esc(current.receivable_id)}; allocation effective {current.date_received.isoformat()}. "
                        f"<a href='/receipts#receipt-{quote(rid,safe='')}'>Review / reconcile existing allocation</a> · "
                        f"<a href='/corrections?receipt_id={quote(rid,safe='')}'>Correct allocation — no cash effect</a>.</p>")
                for op in ledger.owner_operations.values():
                    if op['kind'] in {'split_payment_observed','split_balance_allocated'} and (op['payload'].get('payment_id') or op['payload'].get('observation_id')) == parent['id']:
                        body += f"<p class='mut'>History: {esc(op['kind'].replace('_',' '))}; owner {esc(op['actor'])}; {esc(op['payload']['note'])}.</p>"
                if free > 0:
                    dates = [ledger.as_of,day(parent['date'])]+[ledger.receipts[r].date_received for r in parent['allocation_ids']]
                    dates += [day(c['date']) for c in ledger.receipt_adjustments.values() if c['receipt_id'] in parent['allocation_ids']]
                    dates += [day(c['date']) for c in ledger.split_adjustments.values() if c['payment_id']==parent['id']]
                    earliest = max(dates)
                    body += (f"<form method='post' action='/split-receipts/allocate'>{hidden(ledger)}<input type='hidden' name='payment_id' value='{esc(parent['id'])}'>"
                        + allocation_inputs(ledger,parent['currency']) +
                        f"<label>Allocation effective date<input type='date' name='date' min='{earliest.isoformat()}' value='{earliest.isoformat()}' required></label>"
                        f"<label>Allocation evidence checked<select name='evidence_id' required>{sources(ledger)}</select></label>"
                        "<label>Allocation source check<textarea name='note' maxlength='1000' required></textarea></label>" +
                        confirm('Allocate existing unallocated cash only. This creates no new observed payment, consent or reconciliation.') +
                        "<button type='submit'>Allocate existing balance — no cash effect</button></form>")
                body += '</div>'
            if not ledger.split_receipts:
                body += '<p>No split payment has been owner-confirmed yet.</p>'
            return web.page('Split receipts', body, active='/split-receipts')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/split-receipts/confirm')
    def split_confirm(handler, req):
        def apply(ledger):
            row = ledger.receipt_inbox.get(req.field('observation_id'))
            if row is None:
                raise CashflowError('Unknown source observation.')
            # Parse against all same-currency invoices, including ones settled by this very replay.
            # The engine authorizes and returns the original operation before re-executing it.
            allowed = {'allocate-'+r.id:r for r in ledger.receivables.values() if r.currency == row['currency']}
            fields(req, {'revision','observation_id','note','acknowledge_duplicates'}|set(allowed))
            assignments = {r.id:amount(req.field(name).strip(),row['currency']) for name,r in allowed.items() if req.field(name).strip()}
            assignments = {k:v for k,v in assignments.items() if v}
            return confirm_split_observation(ledger,row['id'],assignments,req.field('note'),req.field('acknowledge_duplicates')=='yes',req.field('revision'),ctx=web._owner_ctx(ledger))
        result = web.mutate_state(apply)
        return handler._redirect('/split-receipts?payment_id='+quote(result['payment_id'],safe=''))

    @web.route('POST', '/split-receipts/allocate')
    def split_allocate(handler, req):
        def apply(ledger):
            parent = ledger.split_receipts.get(req.field('payment_id'))
            if parent is None:
                raise CashflowError('Unknown existing split payment.')
            allowed = {'allocate-'+r.id:r for r in ledger.receivables.values() if r.currency == parent['currency']}
            fields(req, {'revision','payment_id','date','evidence_id','note'}|set(allowed))
            assignments = {r.id:amount(req.field(name).strip(),parent['currency']) for name,r in allowed.items() if req.field(name).strip()}
            assignments = {k:v for k,v in assignments.items() if v}
            return allocate_split_balance(ledger,parent['id'],assignments,day(req.field('date')),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger))
        result = web.mutate_state(apply)
        return handler._redirect('/split-receipts?payment_id='+quote(result['payment_id'],safe=''))
