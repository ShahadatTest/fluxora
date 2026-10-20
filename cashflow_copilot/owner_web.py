"""Server-rendered owner review pages using the existing local web boundary."""
from datetime import date, timedelta
import re
from urllib.parse import quote

from .errors import CashflowError
from .owner_workflows import (close_day, confirm_collection_date, daily_preview,
    receipt_candidates, reconcile_existing_receipt, reopen_receipt_review, review_receipt, review_revision,
    import_observations, observation_candidates, allocate_observation, dismiss_observation)
from .money import format_amount
from .settlement import effective_receipt, split_parent


def mount(web):
    esc, money = web.esc, web.money

    def evidence_options(ledger, selected=''):
        return '<option value="">Choose source evidence</option>' + ''.join(
            f"<option value='{esc(e.id)}' {'selected' if e.id == selected else ''}>{esc(e.id)} — {esc(e.summary[:110])}</option>"
            for e in ledger.evidence.values())

    def required_fields(req, allowed):
        if set(req.form) - set(allowed) - {'csrf', 'confirmed'}:
            raise CashflowError('Unexpected review field. Reload the server review form.')
        if req.field('confirmed') != 'yes':
            raise CashflowError('Explicit owner confirmation is required. Source documents do not authorize this change.')

    def parse_day(value):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            raise CashflowError('Use a calendar date in YYYY-MM-DD form.')

    def confirm_box(text):
        return f"<label><input type='checkbox' name='confirmed' value='yes' required> {esc(text)}</label>"

    @web.route('GET', '/facts')
    def facts_page(handler, req):
        def build(ledger):
            eligible = [r for r in ledger.receivables.values() if r.remaining_minor > 0]
            selected = req.query.get('invoice_id', [''])[0]
            rec = ledger.receivables.get(selected) if selected else (eligible[0] if eligible else None)
            if selected and (rec is None or rec.remaining_minor <= 0):
                raise CashflowError('Choose an outstanding invoice from this business.')
            options = ''.join(f"<option value='{esc(r.id)}' {'selected' if rec and rec.id == r.id else ''}>{esc(r.id)} — {money(r.remaining_minor, r.currency)}</option>" for r in eligible)
            body = ("<h1>Resolve collection facts</h1><p>Confirm a missing, overdue or changed expected collection date. "
                "This is an expectation only: no receipt, cash, approval or counterparty consent is created. Bill and protected payroll dates stay unchanged.</p>"
                f"<div class='card'><form method='get' action='/facts'><label>Outstanding invoice<select name='invoice_id'>{options}</select></label>"
                "<button type='submit'>Review invoice</button></form></div>")
            if rec:
                source = ledger.collection_date_source(rec.id)
                body += (f"<div class='card'><h2>{esc(rec.id)}</h2><div class='kv'><span>Outstanding amount</span><b>{money(rec.remaining_minor, rec.currency)}</b></div>"
                    f"<p>Ledger snapshot: {ledger.as_of.isoformat()}. Current expectation: <b>{web.iso(rec.expected_date)}</b>.</p>"
                    f"<p>Original invoice source: {web.evidence_link(ledger, rec.evidence_id)}. Current date source: {web.evidence_link(ledger, source)}.</p>"
                    f"<form method='post' action='/facts/confirm'>{web.csrf_field()}"
                    f"<input type='hidden' name='revision' value='{review_revision(ledger)}'><input type='hidden' name='invoice_id' value='{esc(rec.id)}'>"
                    f"<label>Confirmed expected collection date<input type='date' name='expected_date' min='{ledger.as_of.isoformat()}' value='{rec.expected_date.isoformat() if rec.expected_date and rec.expected_date >= ledger.as_of else ''}' required></label>"
                    f"<label>Evidence for this expectation<select name='evidence_id' required>{evidence_options(ledger)}</select></label>"
                    "<label>Reason / what was confirmed<textarea name='reason' maxlength='1000' required></textarea></label>"
                    + confirm_box('I reviewed the source and confirm this expected date as the local owner; it is not a payment.') +
                    "<button class='primary' type='submit'>Confirm expected date and reforecast</button></form></div>")
                updates = [r for r in ledger.owner_operations.values() if r['kind'] == 'collection_date_confirmed' and r['payload']['invoice_id'] == rec.id]
                if updates:
                    body += "<div class='card'><h2>Confirmation history</h2>" + ''.join(
                        f"<p>{esc(r['payload']['old_expected_date'] or 'unknown')} → {esc(r['payload']['expected_date'])}; "
                        f"owner {esc(r['actor'])}; {esc(r['payload']['reason'])}. {web.evidence_link(ledger, r['payload']['confirmation_evidence'])}</p>"
                        for r in reversed(updates)) + '</div>'
            else:
                body += '<p>No outstanding invoices to update.</p>'
            body += "<p><a href='/ledger'>Upload evidence or inspect the ledger</a> · <a href='/today'>Review priorities</a></p>"
            return web.page('Resolve facts', body, active='/facts')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/facts/confirm')
    def facts_confirm(handler, req):
        required_fields(req, {'revision', 'invoice_id', 'expected_date', 'evidence_id', 'reason'})
        result = web.mutate_state(lambda ledger: confirm_collection_date(ledger, req.field('invoice_id'),
            parse_day(req.field('expected_date')), req.field('evidence_id'), req.field('reason'), req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/facts?invoice_id=' + quote(result['invoice_id'], safe=''))

    @web.route('GET', '/receipts')
    def receipts_page(handler, req):
        def build(ledger):
            cards = []
            for original in ledger.receipts.values():
                receipt = effective_receipt(ledger, original.id)
                if receipt.state != 'observed':
                    continue
                if receipt.amount_minor <= 0:
                    cards.append(f"<div class='card' id='receipt-{esc(receipt.id)}'><h2>{esc(receipt.id)} — fully reversed</h2><p>Original receipt retained. Current effective amount is zero; no new reconciliation of cash is implied.</p><a href='/corrections?receipt_id={quote(receipt.id, safe='')}'>Inspect corrections</a></div>")
                    continue
                info = receipt_candidates(ledger, receipt.id, ctx=web._owner_ctx(ledger))
                review = info['review']
                source = web.evidence_link(ledger, receipt.evidence_id)
                parent = split_parent(ledger,receipt.id)
                html = (f"<div class='card' id='receipt-{esc(receipt.id)}'><h2>{esc(receipt.id)}</h2>"
                    f"<div class='kv'><span>Existing observed receipt</span><b>{money(receipt.amount_minor, info['currency'])}</b></div>"
                    f"<p>Recorded invoice allocation: <b>{esc(info['invoice_id'])}</b>. Receipt date: {receipt.date_received.isoformat()}. Source: {source}.</p>")
                if parent:
                    html += (f"<p><b>Allocation of existing split payment {esc(parent['id'])}; this row supplies no additional cash.</b> "
                        f"The date above is the allocation effective date. <a href='/split-receipts?payment_id={quote(parent['id'],safe='')}'>Inspect original whole payment/date and unallocated balance</a>.</p>")
                html += f"<p><a href='/corrections?receipt_id={quote(receipt.id, safe='')}'>Review correction / reversal history</a></p>"
                if info['possible_duplicate_ids']:
                    html += "<p class='warn'>Possible duplicates with the same invoice, amount and date: " + ', '.join(esc(r) for r in info['possible_duplicate_ids']) + '. These may be separate payments. No automatic merge or deletion occurs.</p>'
                if review:
                    html += (f"<p>Owner review: <b>{esc(review['status'])}</b>; matched to {esc(review['action_id'] or 'recorded invoice only')}. "
                        f"{esc(review['note'])}. Evidence: {web.evidence_link(ledger, review['evidence_id'])}.</p>")
                    if review['action_id']:
                        html += f"<p><a href='/actions#{esc(review['action_id'])}'>Inspect linked action</a>. Matching does not create approval, send, consent or change an earlier action state.</p>"
                    if review['status'] == 'matched':
                        html += (f"<form method='post' action='/receipts/reconcile'>{web.csrf_field()}"
                            f"<input type='hidden' name='receipt_id' value='{esc(receipt.id)}'><input type='hidden' name='revision' value='{info['revision']}'>"
                            f"<label>Reconciliation evidence<select name='evidence_id' required>{evidence_options(ledger)}</select></label>"
                            "<label>Reconciliation note<textarea name='note' maxlength='1000' required></textarea></label>" +
                            confirm_box('I checked this existing receipt against the source and its allocation; reconcile without creating more cash.') +
                            "<button class='primary' type='submit'>Confirm receipt reconciliation</button></form>")
                        html += (f"<details><summary>Reopen this unreconciled match</summary><form method='post' action='/receipts/reopen'>{web.csrf_field()}"
                            f"<input type='hidden' name='receipt_id' value='{esc(receipt.id)}'><input type='hidden' name='revision' value='{info['revision']}'>"
                            "<label>Reason<textarea name='note' maxlength='1000' required></textarea></label>" +
                            confirm_box('Reopen the owner match only; keep the receipt, its allocation and audit history.') +
                            "<button type='submit'>Reopen match for review</button></form></details>")
                    else:
                        html += f"<p>Reconciliation source: {web.evidence_link(ledger, review['reconciliation_evidence_id'])}. {esc(review['reconciliation_note'])}.</p>"
                else:
                    opts = ''
                    if not info['linked_action_ids']:
                        opts += "<option value=''>Recorded invoice only — no payment-request action</option>"
                    for candidate in info['candidates']:
                        opts += f"<option value='{esc(candidate['action_id'])}'>{esc(candidate['action_id'])} — {'partial payment' if candidate['partial'] else 'fits remaining request'} ({esc(candidate['state'])})</option>"
                        html += f"<p><b>Candidate {esc(candidate['action_id'])}:</b> {esc(candidate['explanation'])} Available request amount: {money(candidate['remaining_request_minor'], info['currency'])}.</p>"
                    if info['linked_action_ids'] and not opts:
                        html += '<p class="warn">The historic action link is conflicting or its current terms are ineligible. Inspect Action History; this review cannot overwrite it.</p>'
                    else:
                        html += (f"<form method='post' action='/receipts/match'>{web.csrf_field()}"
                            f"<input type='hidden' name='receipt_id' value='{esc(receipt.id)}'><input type='hidden' name='revision' value='{info['revision']}'>"
                            f"<label>Confirm match<select name='action_id'>{opts}</select></label>"
                            f"<label>Source evidence checked<select name='evidence_id' required>{evidence_options(ledger)}</select></label>"
                            "<label>Why this match is correct<textarea name='note' maxlength='1000' required></textarea></label>"
                            + ("<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I reviewed the possible duplicate IDs; this confirmation does not remove them.</label>" if info['possible_duplicate_ids'] else '') +
                            confirm_box('I verified this existing receipt and its recorded invoice allocation as the owner.') +
                            "<button type='submit'>Confirm match only</button></form>")
                cards.append(html + '</div>')
            inbox_count = sum(r['status'] == 'unallocated' for r in ledger.receipt_inbox.values())
            import_box = ("<div class='card'><h2>Unallocated synthetic cash observations</h2>"
                f"<p><a class='btn' href='/receipts/inbox'>Review invoice candidates — {inbox_count} unallocated</a></p>"
                "<p>CSV headers: id, amount, currency, date, invoice_reference. The reference may be blank. "
                "Rows enter a review inbox and contribute no cash until the owner confirms an observed payment and exact invoice.</p>"
                f"<form method='post' action='/receipts/import' enctype='multipart/form-data'>{web.csrf_field()}"
                "<label>Synthetic observation CSV<input type='file' name='file' accept='.csv' required></label>"
                "<button type='submit'>Preview observation import — no cash effect</button></form></div>")
            return web.page('Receipt review', "<h1>Receipt matching &amp; reconciliation</h1>" + import_box +
                "<p>Review existing observed receipts below. The transaction importer already allocates each receipt to an invoice; "
                "for unknown allocations use the observation inbox above. This page suggests eligible approved payment requests by currency and unlinked amount. "
                "A suggestion is not proof. Matching never creates another receipt or reallocates cash.</p>"
                "<p>Confirm the match first, then reconcile separately. A receipt can also be reconciled against its invoice without a request. "
                "Partial payments do not imply the full invoice was settled.</p>" +
                (''.join(cards) or '<div class="card"><p>No observed receipts yet. Import synthetic receipt records in Evidence &amp; Ledger, or record an observed synthetic payment through Action History.</p></div>') +
                "<p><a href='/ledger'>Inspect/import ledger</a> · <a href='/today'>Review priorities</a></p>", active='/receipts')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/receipts/import')
    def observation_import_preview(handler, req):
        import csv, io, hashlib
        up = req.files.get('file')
        if not up or not up['filename'].lower().endswith('.csv') or len(up['data']) > web.MAX_UPLOAD_BYTES:
            raise CashflowError('Choose a synthetic CSV file up to 1 MiB.')
        try:
            reader = csv.DictReader(io.StringIO(up['data'].decode('utf-8-sig')))
            if reader.fieldnames is None or set(reader.fieldnames) != {'id', 'amount', 'currency', 'date', 'invoice_reference'} or len(reader.fieldnames) != 5:
                raise CashflowError('Use exactly these CSV headers: id,amount,currency,date,invoice_reference (reference cells may be blank).')
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise CashflowError('Observation CSV has incomplete or extra columns.')
                raw = row['amount'].strip()
                ccy = row['currency'].strip().upper()
                if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?', raw):
                    raise CashflowError('Observation amount must be a positive money amount.')
                rows.append(dict(id=row['id'].strip(), amount_minor=web._parse_major_to_minor(raw, ccy),
                    currency=ccy, date=parse_day(row['date'].strip()).isoformat(), invoice_reference=row['invoice_reference'].strip()))
                if len(rows) > 100:
                    raise CashflowError('Import at most 100 synthetic observation rows at a time.')
        except (UnicodeError, csv.Error):
            raise CashflowError('Use a valid UTF-8 synthetic CSV file.')
        digest = hashlib.sha256(up['data']).hexdigest()
        revision = web.read_state(review_revision)
        # Validation on a local copy does not persist evidence, rows or cash.
        import copy
        def validate(ledger):
            return import_observations(copy.deepcopy(ledger), rows, digest, revision, ctx=web._owner_ctx(ledger))
        web.read_state(validate)
        token = web._stage(dict(kind='cash-observations', rows=rows, file_digest=digest, revision=revision))
        cards = ''.join(f"<div class='kv stack'><span>{esc(r['id'])} · {r['date']} · reference {esc(r['invoice_reference'] or 'unknown')}</span><b>{money(r['amount_minor'], r['currency'])}</b></div>" for r in rows)
        body = ("<h1>Preview unallocated observations</h1><div class='card'>" + cards +
            "<p>These are source observations for owner review, not approved receipts. Importing this inbox does not change cash or invoice balances.</p>"
            f"<form method='post' action='/receipts/import/confirm'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'>" +
            confirm_box('Import these synthetic source rows into the review inbox only; no payment or allocation is authorized yet.') +
            "<button type='submit'>Confirm inbox import — no cash effect</button></form></div>")
        return handler._send_html(200, web.page('Preview observations', body, active='/receipts'))

    @web.route('POST', '/receipts/import/confirm')
    def observation_import_confirm(handler, req):
        required_fields(req, {'token'})
        staged = web.staged().get(req.field('token'))
        if staged is None or staged.get('kind') != 'cash-observations':
            raise CashflowError('Observation preview expired; upload and review the file again.')
        web.mutate_state(lambda ledger: import_observations(ledger, staged['rows'], staged['file_digest'],
            staged['revision'], ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts/inbox')

    @web.route('GET', '/receipts/inbox')
    def observation_inbox(handler, req):
        def build(ledger):
            cards = []
            for oid in ledger.receipt_inbox:
                info = observation_candidates(ledger, oid, ctx=web._owner_ctx(ledger))
                row = info['observation']
                html = (f"<div class='card'><h2>{esc(oid)} <span class='pill'>{esc(row['status'])}</span></h2>"
                    f"<div class='kv'><span>{row['date']}</span><b>{money(row['amount_minor'], row['currency'])}</b></div>"
                    f"<p>Imported invoice reference: {esc(row['invoice_reference'] or 'unknown')}. Source: {web.evidence_link(ledger, row['evidence_id'])}.</p>")
                if row['status'] == 'split_recorded':
                    html += f"<p>This whole payment was already owner-confirmed once. <a href='/split-receipts?payment_id={quote(oid,safe='')}'>Review allocations / unallocated balance</a>; do not record the payment again.</p>"
                elif row['status'] == 'recorded':
                    html += f"<p>Owner-confirmed allocation: {esc(row['invoice_id'])}. <a href='/receipts#receipt-{esc(oid)}'>Review the existing receipt</a>; do not record it again.</p>"
                elif row['status'] == 'dismissed':
                    html += f"<p>Owner dismissed this source row without recording cash: {esc(row['owner_note'])}. Its source and audit are retained.</p>"
                else:
                    html += f"<p><a class='btn' href='/split-receipts?observation_id={quote(oid,safe='')}'>Split this payment across invoices or retain excess</a></p>"
                    for candidate in info['candidates']:
                        html += f"<p><b>{esc(candidate['invoice_id'])}</b> — {esc(candidate['explanation'])} Remaining: {money(candidate['remaining_minor'], row['currency'])}; {'partial payment' if candidate['partial'] else 'full remaining amount'}.</p>"
                    if info['possible_duplicate_ids']:
                        html += "<p class='warn'>Possible duplicate observations/receipts: " + ', '.join(esc(i) for i in info['possible_duplicate_ids']) + '. Check the source; never record the same cash twice under different IDs.</p>'
                    if info['candidates']:
                        options = ''.join(f"<option value='{esc(c['invoice_id'])}'>{esc(c['invoice_id'])} — {'exact reference' if c['exact_reference'] else 'identity to confirm'}</option>" for c in info['candidates'])
                        html += (f"<form method='post' action='/receipts/allocate'>{web.csrf_field()}"
                            f"<input type='hidden' name='observation_id' value='{esc(oid)}'><input type='hidden' name='revision' value='{info['revision']}'>"
                            f"<label>Verified invoice<select name='invoice_id' required><option value=''>Choose the verified invoice</option>{options}</select></label>"
                            "<label>Source checks / allocation reason<textarea name='note' maxlength='1000' required></textarea></label>" +
                            ("<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I checked the possible duplicate IDs against the source.</label>" if info['possible_duplicate_ids'] else '') +
                            confirm_box('I verified this synthetic payment was observed and belongs to the selected invoice. Record it once as an observed receipt; no counterparty consent is inferred.') +
                            "<button class='primary' type='submit'>Confirm observed receipt and invoice allocation</button></form>")
                    else:
                        html += '<p>No invoice of the same currency has enough outstanding balance. Confirm the source/ledger; no allocation or cash is invented.</p>'
                    html += (f"<details><summary>Dismiss this unallocated source row</summary><form method='post' action='/receipts/dismiss'>{web.csrf_field()}"
                        f"<input type='hidden' name='observation_id' value='{esc(oid)}'><input type='hidden' name='revision' value='{info['revision']}'>"
                        "<label>Reason (for example, duplicate source row or not a customer receipt)<textarea name='note' maxlength='1000' required></textarea></label>" +
                        confirm_box('Dismiss this source observation without recording any receipt or cash; retain its audit.') +
                        "<button type='submit'>Dismiss source row — no cash effect</button></form></details>")
                cards.append(html + '</div>')
            return web.page('Observation inbox', "<h1>Unallocated observation inbox</h1><p>These imported rows have no cash effect until an explicit owner confirmation. "
                "Candidates use recorded reference, currency and remaining amount; identity must be checked by the owner. "
                "No model guesses the allocation. Subsequent request matching and reconciliation remain separate.</p>" +
                (''.join(cards) or '<p>No imported observations. Upload a synthetic CSV in Receipt Review.</p>') +
                "<p><a href='/receipts'>Receipt Review and import</a></p>", active='/receipts')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/receipts/allocate')
    def observation_allocate(handler, req):
        required_fields(req, {'revision','observation_id','invoice_id','note','acknowledge_duplicates'})
        result = web.mutate_state(lambda ledger: allocate_observation(ledger, req.field('observation_id'), req.field('invoice_id'),
            req.field('note'), req.field('acknowledge_duplicates') == 'yes', req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts#receipt-' + quote(result['receipt_id'], safe=''))

    @web.route('POST', '/receipts/dismiss')
    def observation_dismiss(handler, req):
        required_fields(req, {'revision','observation_id','note'})
        web.mutate_state(lambda ledger: dismiss_observation(ledger, req.field('observation_id'),
            req.field('note'), req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts/inbox')

    @web.route('POST', '/receipts/match')
    def receipt_match(handler, req):
        required_fields(req, {'revision', 'receipt_id', 'action_id', 'evidence_id', 'note', 'acknowledge_duplicates'})
        result = web.mutate_state(lambda ledger: review_receipt(ledger, req.field('receipt_id'), req.field('action_id'),
            req.field('evidence_id'), req.field('note'), req.field('acknowledge_duplicates') == 'yes', req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts#receipt-' + quote(result['receipt_id'], safe=''))

    @web.route('POST', '/receipts/reconcile')
    def receipt_reconcile(handler, req):
        required_fields(req, {'revision', 'receipt_id', 'evidence_id', 'note'})
        result = web.mutate_state(lambda ledger: reconcile_existing_receipt(ledger, req.field('receipt_id'),
            req.field('evidence_id'), req.field('note'), req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts#receipt-' + quote(result['receipt_id'], safe=''))

    @web.route('POST', '/receipts/reopen')
    def receipt_reopen(handler, req):
        required_fields(req, {'revision', 'receipt_id', 'note'})
        result = web.mutate_state(lambda ledger: reopen_receipt_review(ledger, req.field('receipt_id'),
            req.field('note'), req.field('revision'), ctx=web._owner_ctx(ledger)))
        return handler._redirect('/receipts#receipt-' + quote(result['receipt_id'], safe=''))

    @web.route('GET', '/daily')
    def daily_page(handler, req):
        def build(ledger):
            raw = req.query.get('new_as_of', [''])[0]
            day = parse_day(raw) if raw else ledger.as_of + timedelta(days=1)
            preview = daily_preview(ledger, day, ctx=web._owner_ctx(ledger))
            body = ("<h1>Daily cash update</h1>"
                "<p>Review a new <b>start-of-day</b> opening snapshot. Only observed receipts and paid bills dated before the new date are absorbed. "
                "Expected collections, unapproved requests and unpaid scheduled bills never become settled cash.</p>"
                f"<div class='card'><p>Current snapshot: <b>{ledger.as_of.isoformat()}</b>. Forecast horizon: {ledger.horizon_days} days.</p>"
                f"<form method='get' action='/daily'><label>New snapshot date<input type='date' name='new_as_of' min='{(ledger.as_of + timedelta(days=1)).isoformat()}' value='{day.isoformat()}' required></label>"
                "<button type='submit'>Preview settled cash and unresolved items</button></form></div>")
            if preview['unpaid_before_snapshot']:
                body += ("<div class='card'><h2>Unpaid obligations remain unresolved</h2><p class='warn'>" +
                    ', '.join(esc(i) for i in preview['unpaid_before_snapshot']) +
                    "</p><p>Their original dates stay unchanged. The forecast reserves overdue unpaid amounts at the new opening date for risk analysis. "
                    "Confirm actual payment status separately; cash review does not grant a deferral or consent.</p><a href='/ledger'>Review bills and payment status</a></div>")
            body += f"<form method='post' action='/daily/confirm'>{web.csrf_field()}<input type='hidden' name='revision' value='{preview['revision']}'><input type='hidden' name='new_as_of' value='{day.isoformat()}'>"
            for index, row in enumerate(preview['accounts']):
                ccy = row['currency']
                value = format_amount(row['calculated_opening_minor'], ccy).split(' ', 1)[1].replace(',', '')
                body += (f"<div class='card'><h2>{esc(ccy)} — {esc(row['account_id'])}</h2>"
                    f"<div class='kv'><span>Current opening</span><b>{money(row['old_opening_minor'], ccy)}</b></div>"
                    f"<div class='kv'><span>Observed receipts and correction net absorbed</span><b>{money(row['observed_inflow_minor'], ccy)}</b></div>"
                    f"<p>Receipt IDs: {esc(', '.join(row['observed_receipt_ids']) or 'none')}.</p>"
                    f"<div class='kv'><span>Recorded bills and payment-correction net absorbed</span><b>{money(row['observed_outflow_minor'], ccy)}</b></div>"
                    f"<p>Paid bill IDs: {esc(', '.join(row['paid_bill_ids']) or 'none')}.</p>"
                    f"<div class='kv'><span>Other observed cash and corrections absorbed (signed)</span><b>{money(row['other_cash_minor'],ccy)}</b></div><p>Other cash IDs: {esc(', '.join(row['other_cash_ids']) or 'none')}.</p>"
                    f"<div class='kv'><span>Calculated new opening — settled records only</span><b>{money(row['calculated_opening_minor'], ccy)}</b></div>"
                    f"<label>Owner-confirmed opening cash — {esc(ccy)}<input name='balance_{index}' inputmode='decimal' value='{value}' required></label>"
                    f"<label>Evidence for this cash snapshot<select name='evidence_{index}' required>{evidence_options(ledger)}</select></label>"
                    "<p>If the confirmed amount differs, the difference is recorded explicitly as a snapshot variance. It is not a new receipt or payment.</p></div>")
            body += ("<div class='card'><label>Cash review note / reason for any variance<textarea name='note' maxlength='1000' required></textarea></label>" +
                confirm_box('I reviewed every currency opening balance, its source and unresolved items as the owner. These are observed cash snapshots, not forecast balances.') +
                "<button class='primary' type='submit'>Confirm new cash snapshot and reforecast</button></div></form>"
                "<p>Prior receipts and bills remain in history and are not counted twice. A changed snapshot invalidates new stale plan/review submissions. "
                "New backdated receipts/payments before this snapshot are refused; reconcile older records without importing them again.</p>"
                "<p><a href='/audit/export'>Export audit and snapshot history</a> · <a href='/today'>Review priorities</a></p>")
            return web.page('Daily cash update', body, active='/daily')
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/daily/confirm')
    def daily_confirm(handler, req):
        def apply(ledger):
            accounts = list(ledger.accounts.values())
            required_fields(req, {'revision', 'new_as_of', 'note'} | {f'{prefix}_{i}' for i in range(len(accounts)) for prefix in ['balance', 'evidence']})
            confirmations = {}
            for index, account in enumerate(accounts):
                raw = req.field(f'balance_{index}').strip()
                if not re.fullmatch(r'-?[0-9]{1,12}(\.[0-9]{1,3})?', raw):
                    raise CashflowError('Confirm a valid money amount for every separate currency snapshot.')
                confirmations[account.id] = dict(opening_minor=web._parse_major_to_minor(raw, account.currency), evidence_id=req.field(f'evidence_{index}'))
            return close_day(ledger, parse_day(req.field('new_as_of')), confirmations,
                req.field('note'), req.field('revision'), ctx=web._owner_ctx(ledger))
        web.mutate_state(apply)
        return handler._redirect('/today')
