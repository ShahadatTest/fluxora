"""English owner preview/confirmation forms for party and treasury records."""
import copy
import re
from datetime import date

from . import treasury as t
from .errors import CashflowError
from .owner_workflows import review_revision
from .payment_review import observed_movements


def mount(web):
    esc, money = web.esc, web.money
    links = "<p><a href='/entries'>Owner entries</a> · <a href='/party-register'>Party register</a> · <a href='/cash-accounts'>Cash accounts / transfers</a> · <a href='/cash-availability'>Cash availability</a></p>"
    # Owner-entry navigation is composed before the shared workspace layout.
    web.CSS += '\n.treasury{overflow-wrap:anywhere}.treasury .kv b{white-space:normal}.treasury select{max-width:100%}.treasury label{display:block;margin:10px 0}.treasury label>input:not([type=checkbox]),.treasury label>select,.treasury textarea{display:block;box-sizing:border-box;width:100%;max-width:100%}\n'

    def page(title, body):
        return web.page(title, "<div class='treasury'><h1>" + esc(title) + '</h1>' + links + body + '</div>', active='/entries')
    def options(rows):
        return ''.join(f"<option value='{esc(value)}'>{esc(label)}</option>" for value, label in rows)
    def select(name, label, rows):
        return f"<label>{esc(label)}<select name='{name}' required>" + options(rows) + '</select></label>'
    def input_(name, label, value='', type_='text'):
        return f"<label>{esc(label)}<input name='{name}' type='{type_}' value='{esc(value)}' required></label>"
    def form(ledger, op):
        return "<form method='post' action='/treasury/preview'>" + web.csrf_field() + f"<input type='hidden' name='operation' value='{op}'><input type='hidden' name='revision' value='{review_revision(ledger)}'>"
    def source(ledger):
        return select('evidence_id', 'Checked source evidence', ((e.id, e.id + ' — ' + e.summary[:60]) for e in ledger.evidence.values())) + "<label>Source checks / reason<textarea name='note' maxlength='1000' required></textarea></label>"
    def end(): return '<button type="submit">Preview exact terms</button></form>'
    def hidden(name, value): return f"<input type='hidden' name='{esc(name)}' value='{esc(value)}'>"
    def amounts(value, ccy):
        if not re.fullmatch(r'[0-9]{1,13}(\.[0-9]{1,3})?', value): raise CashflowError('Use an ordinary positive money amount without thousands separators.')
        return web._parse_major_to_minor(value, ccy)
    def fields(req, names):
        allowed = set(names) | {'csrf', 'confirmed', 'operation', 'revision', 'evidence_id', 'note'}
        if set(req.form) - allowed or any(len(v) != 1 for v in req.form.values()): raise CashflowError('Unexpected or repeated treasury form field.')
    def parse(ledger, req):
        op = req.field('operation'); ev = req.field('evidence_id'); note = req.field('note'); rev = req.field('revision'); ctx = web._owner_ctx(ledger)
        if op == 'party':
            fields(req, {'party_id', 'role', 'name'})
            return t.register_party(ledger, req.field('party_id'), req.field('role'), req.field('name'), ev, note, rev, ctx=ctx), '/party-register'
        if op == 'identity':
            fields(req, {'party_id', 'step', 'name'})
            return t.update_party(ledger, req.field('party_id'), req.field('step'), req.field('name'), ev, note, rev, ctx=ctx), '/party-register'
        if op == 'link':
            fields(req, {'kind', 'item_id', 'party_id'})
            return t.link_party(ledger, req.field('kind'), req.field('item_id'), req.field('party_id'), ev, note, rev, ctx=ctx), '/party-register'
        if op == 'account':
            fields(req, {'account_id', 'currency', 'name'})
            return t.add_account(ledger, req.field('account_id'), req.field('currency'), req.field('name'), ev, note, rev, ctx=ctx), '/cash-accounts'
        if op == 'assignment':
            fields(req, {'movement_key', 'account_id'})
            return t.assign_movement(ledger, req.field('movement_key'), req.field('account_id'), ev, note, rev, ctx=ctx), '/cash-accounts'
        if op == 'statement':
            fields(req, {'batch_id', 'account_id'})
            return t.bind_statement(ledger, req.field('batch_id'), req.field('account_id'), ev, note, rev, ctx=ctx), '/cash-accounts'
        if op == 'transfer':
            fields(req, {'transfer_id', 'source_id', 'target_id', 'amount', 'date', 'acknowledge_duplicates'})
            a = ledger.accounts.get(req.field('source_id'))
            if a is None: raise CashflowError('Choose a source account.')
            try: day = date.fromisoformat(req.field('date'))
            except ValueError: raise CashflowError('Use the current business snapshot date.')
            return t.observed_transfer(ledger, req.field('transfer_id'), a.id, req.field('target_id'), amounts(req.field('amount'), a.currency), day, ev, note, rev, ctx=ctx, acknowledge_duplicates=req.field('acknowledge_duplicates')=='yes'), '/cash-accounts'
        if op == 'reverse_transfer':
            fields(req, {'transfer_id'})
            return t.reverse_transfer(ledger, req.field('transfer_id'), ev, note, rev, ctx=ctx), '/cash-accounts'
        if op == 'restriction':
            fields(req, {'restriction_id', 'account_id', 'amount', 'purpose'})
            a = ledger.accounts.get(req.field('account_id'))
            if a is None: raise CashflowError('Choose an account.')
            return t.restrict_cash(ledger, req.field('restriction_id'), a.id, amounts(req.field('amount'), a.currency), req.field('purpose'), ev, note, rev, ctx=ctx), '/cash-availability'
        if op == 'release':
            fields(req, {'restriction_id'})
            return t.release_cash(ledger, req.field('restriction_id'), ev, note, rev, ctx=ctx), '/cash-availability'
        raise CashflowError('Choose a listed owner operation.')

    def account_options(ledger):
        return [(a.id, a.id + ' · ' + a.currency + ' · ' + ledger.account_details.get(a.id, {}).get('name', a.id)) for a in ledger.accounts.values()]
    def balances(ledger):
        report = t.treasury_report(ledger, ctx=web._owner_ctx(ledger)); body = ''
        for r in report['accounts']:
            c = r['currency']
            body += f"<section class='card'><h2>{esc(r['name'])} · {esc(r['account_id'])}</h2><p>{esc(c)}{' · Default for unassigned ordinary observations' if r['default'] else ''}.</p>"
            for label, field in [('Book cash', 'book_minor'), ('Restricted cash', 'restricted_minor'), ('Available for decisions', 'available_minor'), ('Floor buffer', 'floor_minor')]:
                body += f"<div class='kv stack'><span>{label}</span><b>{money(r[field], c)}</b></div>"
            body += web.evidence_link(ledger, r['evidence_id']) + '</section>'
        return body + '<p>' + esc(report['note']) + '</p>'

    @web.route('GET', '/party-register')
    def parties(handler, req):
        def build(ledger):
            body = "<p>Stable customer/supplier IDs with source-reviewed names and aliases. Registering or renaming never merges records or transfers credit. Link each invoice/bill explicitly. Original source names and history remain. These are owner assertions, not independently verified legal identities.</p><p><a href='/parties'>Original source-label statements</a> · <a href='/credits'>Separate credit application</a></p>"
            body += '<section class="card"><h2>Register party</h2>' + form(ledger, 'party') + input_('party_id', 'Stable party ID') + select('role', 'Role', [('customer', 'Customer'), ('supplier', 'Supplier')]) + input_('name', 'Checked name') + source(ledger) + end() + '</section>'
            for p in ledger.party_register.values():
                r = t.party_summary(ledger, p['id'], ctx=web._owner_ctx(ledger))
                body += f"<section class='card'><h2>{esc(p['name'])} · {esc(p['id'])}</h2><p>{esc(p['role'])}; reviewed aliases: {esc(', '.join(p['aliases']) or 'none')}.</p>"
                for ccy, totals in r['currencies'].items(): body += f"<p>{esc(ccy)} remaining {money(totals['remaining_minor'],ccy)}; separate credit {money(totals['credit_minor'],ccy)}.</p>"
                for row in r['records']: body += f"<p>Linked {esc(row['id'])} · original name {esc(row['source_name'])} · {web.evidence_link(ledger,row['evidence_id'])}.</p>"
                body += form(ledger, 'identity') + hidden('party_id', p['id']) + select('step', 'Identity change', [('alias', 'Add reviewed alias'), ('rename', 'Rename; keep previous name as alias'), ('retire_alias', 'Retire an unused alias; retain history')]) + input_('name', 'Checked name / exact alias') + source(ledger) + end()
                kind, group = ('invoice', ledger.receivables) if p['role'] == 'customer' else ('bill', ledger.outflows)
                if group:
                    body += form(ledger, 'link') + hidden('party_id', p['id']) + hidden('kind', kind) + select('item_id', 'Explicitly link a source record', ((i.id, i.id + ' · ' + i.counterparty + ' · ' + i.currency) for i in group.values())) + source(ledger) + end()
                if r['records']:
                    body += form(ledger, 'link') + hidden('party_id', '') + hidden('kind', kind) + select('item_id', 'Correct a mistaken link: unlink record', ((i['id'],i['id']+' · '+i['source_name']) for i in r['records'])) + source(ledger) + end()
                body += '<details><summary>Identity history / evidence</summary>'
                for h in p['history']: body += f"<p>{esc(h['step'])}: {esc(h['name'])}; {esc(h['note'])}; {web.evidence_link(ledger,h['evidence_id'])}.</p>"
                body += '</details></section>'
            return page('Customer / supplier identity register', body)
        return handler._send_html(200, web.read_state(build))

    @web.route('GET', '/cash-accounts')
    def accounts(handler, req):
        def build(ledger):
            body = '<p>Own bank/cash account records, separate by currency. Record only synthetic observations that already happened; no transfer is executed. A new account starts at zero. Use Daily Cash Update to confirm source-backed opening balances; never add the same money to two accounts.</p>' + balances(ledger)
            body += '<section class="card"><h2>Add another own account</h2>' + form(ledger, 'account') + input_('account_id', 'Unique account ID') + input_('name', 'Account label') + select('currency', 'Existing currency', ((c,c) for c in dict.fromkeys(a.currency for a in ledger.accounts.values()))) + source(ledger) + end() + '</section>'
            body += '<section class="card"><h2>Record already-observed own transfer</h2><p>Current snapshot date only. Both accounts must share a currency. Source must have sufficient unrestricted cash. Fees are separately observed cash expenses.</p>' + form(ledger, 'transfer') + input_('transfer_id', 'Unique transfer observation ID') + select('source_id', 'From own account', account_options(ledger)) + select('target_id', 'To own account', account_options(ledger)) + input_('amount', 'Observed amount') + input_('date', 'Observed date', str(ledger.as_of), 'date') + source(ledger) + "<label><input type='checkbox' name='acknowledge_duplicates' value='yes'> I checked any matching same-amount/date observations and confirm this is a distinct observed transfer.</label>" + end() + '</section>'
            for tr in ledger.internal_transfers.values():
                body += f"<section class='card'><h2>{esc(tr['id'])} · {'reversed with compensating history' if tr['reversal'] else 'observed'}</h2><p>{esc(tr['source_id'])} → {esc(tr['target_id'])} · {money(tr['amount_minor'],tr['currency'])} · {esc(tr['date'])}. Business cash change: zero.</p>" + web.evidence_link(ledger,tr['evidence_id'])
                if not tr['reversal']: body += form(ledger, 'reverse_transfer') + hidden('transfer_id', tr['id']) + source(ledger) + end()
                else: body += '<p>Reversal ' + esc(tr['reversal']['date']) + ': ' + web.evidence_link(ledger,tr['reversal']['evidence_id']) + '</p>'
                body += '</section>'
            movements = [r for r in observed_movements(ledger) if not r['kind'].startswith('internal_transfer')]
            if movements:
                body += '<section class="card"><h2>Confirm account for an existing observation</h2><p>Unassigned ordinary observations use the labelled default account, including corrections. Reassignment changes account attribution only, never business cash. Existing matches become stale when their movement scope changes. Already-absorbed historical movements cannot be reassigned.</p>' + form(ledger, 'assignment') + select('movement_key', 'Observed movement', ((r['key'],r['key']+' · '+r['currency']+' · '+r.get('account_id',t.default_account(ledger,r['currency']))) for r in movements)) + select('account_id', 'Checked own account', account_options(ledger)) + source(ledger) + end() + '</section>'
            for batch in ledger.statement_batches.values():
                binding = t.statement_account(ledger,batch['id'])
                body += f"<section class='card'><h2>Statement {esc(batch['id'])}</h2><p>Account scope: {esc(binding or 'not explicitly confirmed')}. <a href='/statements'>Transaction matching</a> · <a href='/statement-balances'>Boundary checks</a>.</p>"
                if not binding: body += form(ledger, 'statement') + hidden('batch_id',batch['id']) + select('account_id', 'Immutable statement account', account_options(ledger)) + source(ledger) + end()
                body += '</section>'
            return page('Cash accounts / observed own transfers', body)
        return handler._send_html(200, web.read_state(build))

    @web.route('GET', '/cash-availability')
    def availability(handler, req):
        def build(ledger):
            body = '<p>Book cash remains recorded cash. Source-backed externally restricted cash is excluded once from available decision balances and forecasts until explicit source-backed release. No automatic release date is assumed. Do not enter payroll or the cash-floor buffer here: they already have separate protection in the forecast. Restriction/release creates no cash, receipt or consent.</p>' + balances(ledger)
            body += '<section class="card"><h2>Record externally restricted funds</h2>' + form(ledger, 'restriction') + input_('restriction_id', 'Unique restriction ID') + select('account_id', 'Account holding these funds', account_options(ledger)) + input_('amount', 'Currently restricted amount') + select('purpose', 'Source-backed restriction', [('restricted_deposit','Restricted deposit'),('legal_hold','Legal hold'),('unavailable_funds','Other externally unavailable funds')]) + source(ledger) + end() + '</section>'
            for r in ledger.cash_restrictions.values():
                ccy = ledger.accounts[r['account_id']].currency
                body += f"<section class='card'><h2>{esc(r['id'])} · {'released' if r['release'] else 'active restriction'}</h2><p>{esc(r['account_id'])} · {money(r['amount_minor'],ccy)} · {esc(r['purpose'])} · {esc(r['date'])}.</p>" + web.evidence_link(ledger,r['evidence_id'])
                if not r['release']: body += form(ledger, 'release') + hidden('restriction_id',r['id']) + source(ledger) + end()
                else: body += '<p>Released ' + esc(r['release']['date']) + ': ' + web.evidence_link(ledger,r['release']['evidence_id']) + '</p>'
                body += '</section>'
            return page('Book cash / restricted cash / availability', body)
        return handler._send_html(200, web.read_state(build))

    @web.route('POST', '/treasury/preview')
    def preview(handler, req):
        def build(ledger):
            candidate = copy.deepcopy(ledger); result, _ = parse(candidate,req)
            body = '<p>No saved state changed. Review these exact inputs, then explicitly confirm the synthetic owner operation.</p><section class="card"><h2>Exact proposed terms</h2>'
            for name, values in req.form.items():
                if name not in {'csrf','revision','confirmed'}: body += f"<p><b>{esc(name.replace('_',' '))}</b>: {esc(values[0])}</p>"
            body += '</section><section class="card"><h2>Book / available cash before → after</h2>'
            before, after = t.treasury_report(ledger,ctx=web._owner_ctx(ledger)), t.treasury_report(candidate,ctx=web._owner_ctx(candidate))
            for ccy, r in after['currencies'].items():
                b = before['currencies'][ccy]
                body += f"<p>{esc(ccy)} book {money(b['book_minor'],ccy)} → {money(r['book_minor'],ccy)}; available {money(b['available_minor'],ccy)} → {money(r['available_minor'],ccy)}.</p>"
            body += '</section>' + balances(candidate) + "<form method='post' action='/treasury/confirm'>" + web.csrf_field()
            for name, values in req.form.items():
                if name not in {'csrf','confirmed'}: body += hidden(name, values[0])
            body += "<label><input type='checkbox' name='confirmed' value='yes' required> I checked these exact sources and explicitly confirm this owner operation. No real payment or message is executed.</label><button type='submit'>Confirm exact operation</button></form>"
            return page('Review identity / account / restriction operation',body)
        return handler._send_html(200,web.read_state(build))

    @web.route('POST', '/treasury/confirm')
    def confirm(handler,req):
        if req.field('confirmed') != 'yes': raise CashflowError('Review and explicitly confirm the owner operation.')
        _, destination = web.mutate_state(lambda ledger: parse(ledger,req))
        return handler._redirect(destination)
