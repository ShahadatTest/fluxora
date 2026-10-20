"""Read-only daily work guide; statuses come from existing saved facts."""
from .authz import require_access,OP_READ
from .attention import attention_items
from .business_tools import data_freshness


def daily_review(ledger,*,ctx):
    require_access(ctx,ledger,OP_READ)
    items=attention_items(ledger,ctx=ctx)
    groups=[
        ('sources','Check sources and duplicates',{'duplicate_source','observation_unallocated','split_unallocated','recurring_pending'},'/ledger',
            'Import evidence and check source identities. Proposed recurring occurrences are excluded until explicitly confirmed.'),
        ('matching','Match observations and check statement boundaries',{'statement_review','statement_boundary','statement_boundary_missing','statement_account_required'},'/statements',
            'Match existing observed movements. Review opening/closing differences and linked periods; matching never creates cash.'),
        ('cash','Review cash snapshot',{'cash_source_review','account_cash_review','future_account_review','future_account_shortfall'},'/daily',
            'Check the visible business date and cash sources. Only an explicit reviewed owner operation updates the snapshot.'),
        ('plans','Compare plans and assumptions',{'cash_breach','confirm_input','collection_hold','customer_promise_review','protected_due'},'/plans',
            'Use currency-separated deterministic results. A proposed plan is not approval, counterparty acceptance or observed payment.'),
        ('drafts','Review drafts and pending decisions',{'stale_plan','approval_needed','approved_pending','response_pending','request_rejected','payment_pending','workflow_approval_needed'},'/actions',
            'Owner review and recorded external acceptance remain separate. The agent can only read, draft and prepare pending workflows.'),
        ('receipts','Review observed payment and reconciliation',{'unlinked_receipt','receipt_review_reconcile','reconcile_needed'},'/receipts',
            'Record only a genuinely observed synthetic payment once, then match/reconcile it separately. Never add another receipt to clear a warning.'),
    ]
    stages=[]
    for ident,title,codes,url,note in groups:
        selected=[i for i in items if i['code'] in codes]
        stages.append(dict(id=ident,title=title,items=selected,count=len(selected),href=selected[0]['href'] if selected else url,
            default_href=url,status='needs_review' if selected else 'no_flagged_items',note=note))
    return dict(business_id=ledger.business_id,business_name=ledger.business_name,as_of=str(ledger.as_of),stages=stages,
        total_flags=sum(s['count'] for s in stages),statement_count=len(ledger.statement_batches),
        cash_checks=data_freshness(ledger,ctx=ctx)['rows'],
        note='No flagged items means only that these saved-state checks found no issue. It does not prove completeness, bank truth, consent or that the daily work was performed. Refresh after each owner operation. No background monitoring or automatic authorization.')


def mount(web):
    esc=web.esc
    web.CSS+="\n.skip-link{position:absolute;left:8px;top:-100px;z-index:1000;background:#fff;padding:10px}.skip-link:focus{top:8px}a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid #1264a3;outline-offset:3px}\n"
    @web.route('GET','/review')
    def review_page(handler,req):
        def build(ledger):
            report=daily_review(ledger,ctx=web._owner_ctx(ledger))
            body=f"<h1>Daily Review</h1><p>{esc(report['business_name'])} · business snapshot <strong>{esc(report['as_of'])}</strong>. Follow the six steps, then return here to refresh the current saved-state checks.</p><p><a class='btn' href='/review'>Refresh checks</a> <a href='/usability'>Try the guided task session</a></p>"
            if not report['statement_count']:body+="<div class='card'><h2>No statement imported</h2><p>If you have a synthetic statement to review, import it before checking its balances. No statement/bank completeness is implied.</p><a href='/statements'>Import a statement</a></div>"
            body+="<nav aria-label='Daily review steps'><ol>"+''.join(f"<li><a href='#step-{s['id']}'>{esc(s['title'])}</a> — {s['count']} flags</li>" for s in report['stages'])+'</ol></nav>'
            for number,s in enumerate(report['stages'],1):
                body+=f"<section class='card' id='step-{s['id']}' aria-labelledby='heading-{s['id']}'><h2 id='heading-{s['id']}'>{number}. {esc(s['title'])}</h2><p>{esc(s['note'])}</p><p>"
                body+=f"<strong>{s['count']} items need review</strong>" if s['count'] else 'No flagged items in these saved-state checks'
                body+='.</p><ul>'+''.join(f"<li><a href='{esc(i['href'])}'>{esc(i['title'])}</a> — {esc(i['why'])}</li>" for i in s['items'][:8])+'</ul>'
                if s['count']>8:body+="<p>More items are listed in <a href='/today'>Today's Priorities</a>.</p>"
                body+=f"<p><a class='btn' href='{esc(s['href'])}'>Open step {number}</a> <a href='{s['default_href']}'>Open all records for this step</a></p></section>"
                if s['id']=='drafts':body+="<p><a href='/agent/workflows'>Open workflow queue</a> · <a href='/actions'>Open action history</a></p>"
            body+='<p>'+esc(report['note'])+'</p>'
            return web.page('Daily Review',body,active='/review')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/usability')
    def usability_page(handler,req):
        body="""<h1>Guided task session</h1><p>Use synthetic data only. A human observer records completion, errors and feedback separately. This page does not submit feedback or mark any financial step complete.</p>
<ol><li>Open Daily Review. Identify the business snapshot date and find the most urgent source or cash issue.</li>
<li>Follow its evidence link. Explain what is recorded and what still needs checking. Return to Daily Review.</li>
<li>Find the statement balance view. Explain a mismatch and its effect on cash; do not invent a balancing payment.</li>
<li>Compare plans. Explain whether a sufficient result is conditional and what remains unapproved or unaccepted.</li>
<li>Ask a labelled mock question. Review the question, provider destination and minimized/full data preview before confirming. Identify the unverified commentary and the separate deterministic financial result. Refresh the saved request link; unknown provider usage is not zero.</li>
<li>Inspect Action History and Receipt Review. Explain the difference between a draft, approval, counterparty acceptance, observed money and reconciliation.</li></ol>
<p>Record where you got stuck and what you expected to happen. Use a fresh synthetic test database for tasks that intentionally create/correct records. Do not use reset/restore on someone else's active session.</p>
<h2>Current optional tasks on a separate synthetic session</h2>
<ul><li>Open <a href='/scenarios'>Scenarios</a>. Compare partial/zero collection of an invoice's current unpaid remainder. Explain why the excluded cash stays owed and the percentage is not a probability.</li>
<li>Open <a href='/statement-mapping'>Statement mapping</a>. Review the profile, own account, inclusive period, parsing rules, ignored columns and signed rows. A header-only file is a zero-activity source assertion, not a verified bank balance.</li>
<li>Open <a href='/account-forecast'>Account funding</a> and <a href='/cash-availability'>Cash availability</a>. Explain why pooled cash can hold while the payroll account is short; do not assume a transfer or restriction release.</li>
<li>Open <a href='/weekly-cashflow'>13-week cash view</a> and <a href='/decision-brief'>Decision Brief</a>. Find a daily minimum and source; distinguish proposals, approvals, acceptance, observed money and reconciliation.</li></ul>
<p>Use tools/prepare_usability.py from the source package to prepare a NEW isolated session folder and blank observation sheet. Choose a short task subset; record skips and hints honestly. Actual fresh-user testing stays pending until a real person performs it. Automated or agent-led checks do not count as participants.</p>
<p><a class='btn' href='/review'>Start Daily Review</a> <a href='/agent'>Ask Agent</a> <a href='/today'>Today's Priorities</a></p>"""
        return handler._send_html(200,web.page('Guided task session',body,active='/review'))
