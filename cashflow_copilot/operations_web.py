"""Server-rendered local owner pages for collection/recurrence/source review."""
from datetime import date, timedelta
import re
from urllib.parse import quote

from .errors import CashflowError
from .owner_workflows import review_revision
from .operations import (item_identity, collection_priorities, duplicate_pairs, review_duplicate,
    create_recurring, pause_recurring, recurring_preview, confirm_recurring)


def mount(web):
    esc,money=web.esc,web.money
    def source_options(ledger):
        return '<option value="">Choose source checked</option>'+''.join(f"<option value='{esc(e.id)}'>{esc(e.id)} — {esc(e.summary[:90])}</option>" for e in ledger.evidence.values())
    def hidden(ledger):return web.csrf_field()+f"<input type='hidden' name='revision' value='{review_revision(ledger)}'>"
    def confirm(text):return f"<label><input type='checkbox' name='confirmed' value='yes' required> {esc(text)}</label>"
    def fields(req,names):
        if set(req.form)-set(names)-{'csrf','confirmed'} or req.field('confirmed')!='yes':
            raise CashflowError('Review the owner form, supply only its fields and explicitly confirm. Documents never authorize changes.')
    def parse_day(value,optional=False):
        if optional and not value:return None
        try:return date.fromisoformat(value)
        except (ValueError,TypeError):raise CashflowError('Use a calendar date in YYYY-MM-DD format.')
    def boolean(value):
        if value not in {'yes','no'}:raise CashflowError('Choose an explicit yes/no option.')
        return value=='yes'
    def detail_link(kind,ident):return '/records?kind='+kind+'&item_id='+quote(ident,safe='')

    @web.route('GET','/collections')
    def collections_page(handler,req):
        def build(ledger):
            report=collection_priorities(ledger,ctx=web._owner_ctx(ledger))
            body=(f"<h1>Receivables ageing &amp; collection priorities</h1><p>As of ledger snapshot <b>{esc(report['as_of'])}</b>. "
                "Ageing uses source-backed contractual due dates. Expected collection dates remain separate; missing due dates stay unknown. "
                "Each possible collection is a separate hypothetical calculation, not money on hand or a combined plan.</p>"
                '<p><a href="/duplicates">Review possible duplicate invoices first</a> · <a href="/plans">Compare plans and owner-reviewed drafts</a></p>')
            labels={'not_due':'Not overdue (includes due today)','1_30':'1–30 days overdue','31_60':'31–60 days overdue','61_90':'61–90 days overdue','over_90':'Over 90 days overdue','unknown':'Due date unknown'}
            for ccy,group in report['currencies'].items():
                body+=f"<div class='card'><h2>{esc(ccy)} — outstanding balances</h2>"
                body+=''.join(f"<div class='kv'><span>{label}</span><b>{money(group['buckets'][key],ccy)}</b></div>" for key,label in labels.items())+'</div>'
                for rank,row in enumerate(group['invoices'],1):
                    impact=("<p class='warn'>HELD — remaining collection excluded; gap relief is not evaluated while held. <a href='/holds'>Review source hold</a>.</p>" if row['collection_held'] else
                        f"<div class='kv stack'><span>If the entire remaining amount arrived on {esc(report['as_of'])}: worst floor shortfall, baseline → conditional</span><b>{money(row['baseline_shortfall_minor'],ccy)} → {money(row['conditional_shortfall_minor'],ccy)}</b></div>"
                        f"<div class='kv'><span>Conditional cash-gap reduction</span><b>{money(row['gap_reduction_minor'],ccy)}</b></div>")
                    body+=(f"<div class='card'><h2>{rank}. {esc(row['id'])}</h2><p>{esc(row['counterparty'] or 'Counterparty unknown')}; external reference {esc(row['invoice_reference'] or 'unknown')}.</p>"
                        f"<div class='kv'><span>Outstanding</span><b>{money(row['remaining_minor'],ccy)}</b></div>"
                        f"<p>Contractual due: {esc(row['due_date'] or 'unknown')}; {esc(labels[row['bucket']])}. "
                        f"Expected collection: {esc(row['expected_date'] or 'unknown')}. Due evidence: {web.evidence_link(ledger,row['due_evidence_id'])}; invoice source: {web.evidence_link(ledger,row['evidence_id'])}.</p>"
                        f"{impact}"
                        f"<p><a href='{detail_link('invoice',row['id'])}'>Review contractual due date / party / reference</a> · <a href='/facts?invoice_id={quote(row['id'],safe='')}'>Review expected collection date</a></p></div>")
            body+="<div class='card'><h2>Ranking assumptions</h2><p>"+esc(report['assumption'])+"</p><p>Order: conditional gap reduction, days overdue, outstanding amount, then stable invoice ID. This does not infer customer willingness or authorize messages.</p></div>"
            return web.page('Collection priorities',body,active='/collections')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/records')
    def records_page(handler,req):
        def build(ledger):
            kind=req.query.get('kind',['invoice'])[0];collection=ledger.receivables if kind=='invoice' else ledger.outflows if kind=='bill' else {}
            ident=req.query.get('item_id',[next(iter(collection),'')])[0];item=collection.get(ident)
            if item is None:raise CashflowError('Choose an invoice/bill source detail link from the ledger or duplicate review.')
            due=(f"<label>Contractual invoice due date (blank = unknown)<input type='date' name='due_date' value='{item.due_date.isoformat() if item.due_date else ''}'></label>"
                f"<p>Expected collection {esc(item.expected_date.isoformat() if item.expected_date else 'unknown')} stays unchanged. A due date alone does not promise payment.</p>") if kind=='invoice' else ''
            body=(f"<h1>Review record source details</h1><div class='card'><h2>{esc(kind)}: {esc(ident)}</h2>"
                "<p>Confirm the identity/contractual source fields. This changes no original money, cash, protected bill date, approval or consent. Clearing a due date marks it unknown.</p>"
                f"<form method='post' action='/records/confirm'>{hidden(ledger)}<input type='hidden' name='kind' value='{esc(kind)}'><input type='hidden' name='item_id' value='{esc(ident)}'>"
                f"<label>Customer / supplier (blank = unknown)<input name='counterparty' maxlength='200' value='{esc(item.counterparty)}'></label>"
                f"<label>External invoice reference (blank = unknown)<input name='invoice_reference' maxlength='200' value='{esc(item.invoice_reference)}'></label>"+due+
                f"<label>Checked source evidence<select name='evidence_id' required>{source_options(ledger)}</select></label>"
                "<label>Source/reason note<textarea name='note' maxlength='1000' required></textarea></label>"+
                confirm('I checked the source and authorize these record details as local owner; no payment, consent or legal conclusion is created.')+
                "<button type='submit'>Confirm source details</button></form></div><p><a href='/collections'>Collection priorities</a> · <a href='/duplicates'>Duplicate review</a></p>")
            return web.page('Record source details',body,active='/collections' if kind=='invoice' else '/duplicates')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/records/confirm')
    def records_confirm(handler,req):
        fields(req,{'kind','item_id','counterparty','invoice_reference','due_date','evidence_id','note','revision'})
        kind=req.field('kind')
        web.mutate_state(lambda ledger:item_identity(ledger,kind,req.field('item_id'),req.field('counterparty'),req.field('invoice_reference'),
            req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger),due_date=parse_day(req.field('due_date'),True)))
        return handler._redirect('/collections' if kind=='invoice' else '/duplicates')

    @web.route('GET','/duplicates')
    def duplicates_page(handler,req):
        def build(ledger):
            pairs=duplicate_pairs(ledger,ctx=web._owner_ctx(ledger))
            body=("<h1>Invoice &amp; bill duplicate review</h1><p>Suggestions compare same-kind, same-currency records with different IDs. "
                "Same party/reference is stronger evidence than equal amount/date; neither proves duplication. Unknown parties reduce confidence. "
                "Reviewing never deletes, merges or excludes financial entries. Confirmed duplicates remain included and block new sufficient-plan claims until the source is corrected or the review is revised.</p>"
                "<p>CSV import also checks existing records and rows within its preview. The owner can explicitly skip a potential duplicate before creation. Source links and original records are retained.</p>")
            for pair in pairs:
                body+=f"<div class='card' id='pair-{pair['id'][:24]}'><h2>Possible duplicate {esc(pair['kind'])}</h2><p>{esc(pair['reason'])}</p>"
                for side in ['left','right']:
                    row=pair[side]
                    body+=(f"<h3>{esc(row['id'])}</h3><p>{money(row['amount_minor'],row['currency'])}; recorded expected/bill date {esc(row['date'] or 'unknown')}; party {esc(row['counterparty'] or 'unknown')}; external reference {esc(row['invoice_reference'] or 'unknown')}. "
                        f"Original: {web.evidence_link(ledger,row['evidence_id'])}; identity: {web.evidence_link(ledger,row['identity_evidence_id'])} · <a href='{detail_link(pair['kind'],row['id'])}'>Review source details</a></p>")
                review=pair['review']
                if review:body+=f"<p class='warn'>Owner verdict: {esc(review['verdict'])}. {esc(review['note'])}. {web.evidence_link(ledger,review['evidence_id'])}. Changed pair terms require a fresh review.</p>"
                body+=(f"<form method='post' action='/duplicates/review'>{hidden(ledger)}<input type='hidden' name='pair_id' value='{pair['id']}'>"
                    "<label>Owner verdict<select name='verdict'><option value='distinct'>Distinct legitimate records</option><option value='confirmed_duplicate'>Confirmed duplicate — source correction required</option></select></label>"
                    f"<label>Checked evidence<select name='evidence_id' required>{source_options(ledger)}</select></label>"
                    "<label>Source comparison / reason<textarea name='note' maxlength='1000' required></textarea></label>"+
                    confirm('I compared both sources; record my review without deleting/merging or silently changing balances.')+
                    "<button type='submit'>Record duplicate review</button></form></div>")
            if not pairs:body+="<div class='card'><p>No pairs match these limited rules. This is not proof that the ledger contains no duplicates.</p></div>"
            body+="<div class='card'><h2>Add or correct source identity fields</h2>"
            for kind,collection in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
                body+='<p>'+esc(kind)+': '+', '.join(f"<a href='{detail_link(kind,item.id)}'>{esc(item.id)}</a>" for item in collection.values())+'</p>'
            return web.page('Duplicate review',body+'</div>',active='/duplicates')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/duplicates/review')
    def duplicates_review(handler,req):
        fields(req,{'pair_id','verdict','evidence_id','note','revision'})
        web.mutate_state(lambda ledger:review_duplicate(ledger,req.field('pair_id'),req.field('verdict'),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/duplicates')

    @web.route('GET','/recurring')
    def recurring_page(handler,req):
        def build(ledger):
            body=("<h1>Recurring bill schedules</h1><p>Save a source-backed weekly/monthly template, then preview and explicitly confirm future occurrences. "
                "Templates alone add no bill or forecast expense. Confirmed occurrences are scheduled obligations, never observed payments. Payroll protection and supplier-consent requirements remain separate.</p>")
            for template in ledger.recurring_bills.values():
                body+=(f"<div class='card'><h2>{esc(template['label'])}</h2><p>{esc(template['id'])}; {esc(template['frequency'])}, {money(template['amount_minor'],template['currency'])}; "
                    f"anchor {esc(template['start'])}, end {esc(template['end'] or 'open')}; {'active' if template['active'] else 'paused'}. "
                    f"Protected: {template['protected']}; consent required for change: {template['consent_required']}. {web.evidence_link(ledger,template['evidence_id'])}.</p>"
                    f"<a class='btn' href='/recurring/{quote(template['id'],safe='')}/preview'>Preview future occurrences</a>"
                    f"<form method='post' action='/recurring/status'>{hidden(ledger)}<input type='hidden' name='template_id' value='{esc(template['id'])}'>"
                    f"<input type='hidden' name='active' value='{'no' if template['active'] else 'yes'}'>"+
                    confirm('Change only future generation status; existing bills/payments remain unchanged.')+
                    f"<button type='submit'>{'Pause' if template['active'] else 'Resume'} future generation</button></form></div>")
            currencies=''.join(f"<option>{esc(c)}</option>" for c in sorted({a.currency for a in ledger.accounts.values()}))
            body+=(f"<div class='card'><h2>Create a template — no bills yet</h2><form method='post' action='/recurring/create'>{hidden(ledger)}"
                "<label>Unique template ID<input name='template_id' pattern='[A-Za-z0-9_-]{1,64}' required></label><label>Label<input name='label' maxlength='120' required></label>"
                f"<label>Currency<select name='currency'>{currencies}</select></label><label>Amount per occurrence<input name='amount' inputmode='decimal' required></label>"
                f"<label>First occurrence / monthly anchor<input type='date' name='start' min='{ledger.as_of.isoformat()}' value='{ledger.as_of.isoformat()}' required></label>"
                "<label>Optional final date<input type='date' name='end'></label><label>Frequency<select name='frequency'><option value='monthly'>Monthly</option><option value='weekly'>Weekly</option></select></label>"
                "<label>Protected obligation (choose yes for payroll)<select name='protected'><option value='yes'>Yes — keep protected dates</option><option value='no'>No</option></select></label>"
                "<label>Counterparty consent required to change terms<select name='consent_required'><option value='yes'>Yes</option><option value='no'>No</option></select></label>"
                "<label>Supplier / counterparty<input name='counterparty' maxlength='200'></label>"
                f"<label>Checked source<select name='evidence_id' required>{source_options(ledger)}</select></label>"
                "<label>Template source/reason<textarea name='note' maxlength='1000' required></textarea></label>"+
                confirm('Save only this template as owner. I will review actual occurrence dates/amounts before scheduled bills are created; no payment or consent is granted.')+
                "<button type='submit'>Save template and review occurrences</button></form></div>")
            return web.page('Recurring bills',body,active='/recurring')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/recurring/create')
    def recurring_create(handler,req):
        fields(req,{'template_id','label','currency','amount','start','end','frequency','protected','consent_required','counterparty','evidence_id','note','revision'})
        raw=req.field('amount')
        if not re.fullmatch(r'[0-9]{1,12}(\.[0-9]{1,3})?',raw):raise CashflowError('Enter a positive ordinary money amount in the chosen currency.')
        result=web.mutate_state(lambda ledger:create_recurring(ledger,req.field('template_id'),req.field('label'),req.field('currency'),web._parse_major_to_minor(raw,req.field('currency')),
            parse_day(req.field('start')),parse_day(req.field('end'),True),req.field('frequency'),boolean(req.field('protected')),boolean(req.field('consent_required')),
            req.field('counterparty'),req.field('evidence_id'),req.field('note'),req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/recurring/'+quote(result['template_id'],safe='')+'/preview')

    @web.route('POST','/recurring/status')
    def recurring_status(handler,req):
        fields(req,{'template_id','active','revision'})
        web.mutate_state(lambda ledger:pause_recurring(ledger,req.field('template_id'),boolean(req.field('active')),req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/recurring')

    @web.route('GET','/recurring/<template_id>/preview')
    def recurring_preview_page(handler,req,template_id):
        def build(ledger):
            through=parse_day(req.query.get('through',[(ledger.as_of+timedelta(days=min(90,ledger.horizon_days-1))).isoformat()])[0])
            report=recurring_preview(ledger,template_id,through,ctx=web._owner_ctx(ledger));template=report['template']
            body=(f"<h1>Review recurring occurrences</h1><div class='card'><h2>{esc(template['label'])}</h2><p>{esc(report['note'])}</p>"
                f"<form method='get' action='/recurring/{quote(template_id,safe='')}/preview'><label>Preview through<input type='date' name='through' min='{ledger.as_of.isoformat()}' max='{(ledger.as_of+timedelta(days=366)).isoformat()}' value='{through.isoformat()}' required></label><button type='submit'>Refresh preview</button></form></div>"
                "<div class='card'><table><thead><tr><th>Date</th><th>Amount</th><th>Occurrence ID</th><th>Status / possible duplicate</th></tr></thead><tbody>")
            for row in report['rows']:
                warnings='; '.join(w['id']+': '+w['reason'] for w in row['duplicate_warnings'])
                body+=f"<tr><td>{row['date']}</td><td>{money(row['amount_minor'],row['currency'])}</td><td>{esc(row['id'])}</td><td>{'Already created — kept as recorded' if row['already_created'] else 'New scheduled bill'} {esc(warnings)}</td></tr>"
            body+='</tbody></table>'
            new=[r for r in report['rows'] if not r['already_created']]
            if template['active'] and new:
                body+=(f"<form method='post' action='/recurring/{quote(template_id,safe='')}/confirm'>{hidden(ledger)}<input type='hidden' name='through' value='{through.isoformat()}'>")
                if any(r['duplicate_warnings'] for r in new):body+="<label><input type='checkbox' name='acknowledge_duplicates' value='yes' required> I checked possible existing bills; these new occurrences are distinct obligations. If they duplicate existing bills, cancel and correct the template/source.</label>"
                body+=confirm(f'I reviewed these {len(new)} new occurrence dates/amounts and authorize their scheduled bills. This creates no observed payment or consent.')+f"<button type='submit'>Create {len(new)} reviewed scheduled bills</button></form>"
            else:body+='<p>No new occurrences in this window, or the template is paused. Existing generated bills remain unchanged.</p>'
            return web.page('Recurring preview',body+'</div>',active='/recurring')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/recurring/<template_id>/confirm')
    def recurring_confirm(handler,req,template_id):
        fields(req,{'through','acknowledge_duplicates','revision'})
        through=parse_day(req.field('through'))
        web.mutate_state(lambda ledger:confirm_recurring(ledger,template_id,through,req.field('acknowledge_duplicates')=='yes',req.field('revision'),ctx=web._owner_ctx(ledger)))
        return handler._redirect('/recurring/'+quote(template_id,safe='')+'/preview?through='+through.isoformat())
