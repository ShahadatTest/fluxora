"""Read-only work queue derived from the ledger snapshot, never wall time."""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import quote

from .adaptive import input_problems
from .authz import OP_READ, require_access
from .errors import CashflowError
from .forecast import forecast
from .planning import _revision
from .owner_workflows import receipt_links
from .settlement import effective_receipt, bill_schedule, collection_held


def stale_plan(ledger, action) -> bool:
    for entry in action.audit:
        if entry['event'] == 'plan_drafts_created':
            record = json.loads(entry['detail'])
            original = record.get('input_revision')
            return bool(original and original != _revision(ledger, 'financial-inputs', []))
    return False


def attention_items(ledger, *, ctx) -> list[dict]:
    require_access(ctx, ledger, OP_READ)
    items = []
    def add(code, target, title, why, step, href, *, day=None, currency=None,
            amount=None, sources=(), urgency='review'):
        items.append(dict(id=f'{code}:{target}', code=code, target=target, title=title,
            why=why, next_step=step, href=href, date=day.isoformat() if day else None,
            currency=currency, amount_minor=amount,
            evidence_ids=sorted({s for s in sources if s}), urgency=urgency))

    fc = forecast(ledger, ledger.as_of, ledger.horizon_days)
    problems = input_problems(ledger)
    from .settlement import split_unallocated
    from .business_tools import data_freshness
    for parent in ledger.split_receipts.values():
        remaining = split_unallocated(ledger,parent)
        if remaining:
            add('split_unallocated',parent['id'],'Observed payment has unallocated cash',
                'This whole payment already contributes cash once; its remainder is not another expected collection or revenue.',
                'Check invoice identities and allocate only the existing balance.', '/split-receipts?payment_id='+quote(parent['id'],safe=''),
                currency=parent['currency'],amount=remaining,sources=[parent['evidence_id']])
    for row in data_freshness(ledger,ctx=ctx)['rows']:
        if row['kind']=='cash' and row['status']!='fresh':
            add('cash_source_review',row['id'],f"Review {row['currency']} opening cash source — {row['id']}",
                'Current opening source review is '+row['status'].replace('_',' ')+'. This is not a live bank balance.',
                'Check the source or use Daily Cash Update if the opening needs correction.', '/freshness',
                currency=row['currency'],amount=row['amount_minor'],sources=[row['evidence_id']])
    for rec in ledger.receivables.values():
        if collection_held(rec):
            add('collection_hold',rec.id,'Review a held/disputed collection',
                'Remaining receivable is retained but excluded from forecast and collection requests.',
                'Check dispute evidence; only explicit owner resolution releases the hold.', '/holds?invoice_id='+quote(rec.id,safe=''),
                currency=rec.currency,amount=rec.remaining_minor,sources=[rec.evidence_id,rec.collection_hold['evidence_id']],urgency='high')
    from .operations import duplicate_pairs
    for pair in duplicate_pairs(ledger,ctx=ctx):
        if not pair['review'] or pair['review']['verdict']=='confirmed_duplicate':
            add('duplicate_source',pair['id'],'Review a possible duplicate '+pair['kind'],
                pair['reason']+' Both financial entries remain included until source correction.',
                'Compare source records and explicitly review; do not record another payment.', '/duplicates#pair-'+pair['id'][:24],
                currency=pair['left']['currency'],sources=[pair['left']['evidence_id'],pair['right']['evidence_id']],urgency='high')
    for observation in ledger.receipt_inbox.values():
        if observation['status'] == 'unallocated':
            add('observation_unallocated', observation['id'], 'Verify an unallocated cash observation',
                'The imported row has not been owner-confirmed as a receipt and contributes no cash.',
                'Check its source and confirm the exact invoice only if the payment was observed.', '/receipts/inbox',
                day=date.fromisoformat(observation['date']), currency=observation['currency'],
                amount=observation['amount_minor'], sources=[observation['evidence_id']], urgency='high')
    for index, problem in enumerate(problems):
        target_id = problem.split(':', 1)[0]
        target = ledger.receivables.get(target_id) or ledger.outflows.get(target_id)
        resolve_url = '/facts?invoice_id=' + quote(target.id, safe='') if target_id in ledger.receivables else '/ledger'
        if target_id in ledger.receivables and ledger.receivables[target_id].credit_balance_minor:
            resolve_url='/credits'
        elif target_id in ledger.outflows and ledger.outflows[target_id].overpaid_minor:
            resolve_url='/credits'
        elif 'possible duplicate observed payment' in problem:
            resolve_url='/payment-warnings'
        add('confirm_input', target_id if target else str(index), 'Confirm a ledger input', problem,
            'Review the source and confirm the ledger facts before relying on a plan.', resolve_url,
            sources=[target.evidence_id] if target else [], currency=target.currency if target else None,
            amount=(target.remaining_minor if hasattr(target, 'remaining_minor') else target.amount_minor) if target else None,
            urgency='high')
    for ccy, curve in fc.currencies.items():
        if curve.first_breach_date:
            sources = [i.evidence_id for group in (ledger.accounts, ledger.receivables, ledger.outflows)
                       for i in group.values() if i.currency == ccy]
            add('cash_breach', ccy, f'{ccy} cash floor at risk',
                f'First projected breach: {curve.first_breach_date.isoformat()}; worst shortfall is against the recorded cash floor. Intraday outflows precede inflows.',
                'Compare the data-derived plans; confirm assumptions before creating drafts.', '/plans',
                day=curve.first_breach_date, currency=ccy, amount=curve.worst_shortfall_minor,
                sources=sources, urgency='critical' if curve.first_breach_date <= ledger.as_of + timedelta(days=7) else 'high')
    for out in ledger.outflows.values():
        if out.protected and out.status == 'scheduled':
            for day, amount in bill_schedule(out):
                if ledger.as_of <= day <= ledger.as_of + timedelta(days=7) and amount > 0:
                    add('protected_due', out.id, 'Protected obligation due within 7 days',
                        f'{out.id} keeps its recorded date. A plan cannot defer protected payroll or another protected obligation.',
                        'Check cash coverage and source evidence; keep this payment date protected.', '/ledger',
                        day=day, currency=out.currency, amount=amount, sources=[out.evidence_id], urgency='high')
    linked_receipts = set()
    for action in ledger.actions.values():
        if action.business_id and action.business_id != ledger.business_id:
            continue
        rec = ledger.receivables.get(action.terms.get('receivable_id'))
        out = ledger.outflows.get(action.terms.get('outflow_id'))
        target = rec or out
        sources = [target.evidence_id] if target else []
        if action.counterparty_response:
            sources.append(action.counterparty_response.get('evidence_id'))
        if action.state in {'payment_observed', 'reconciled'}:
            linked_receipts.update(e['detail'].removeprefix('receipt=') for e in action.audit
                if e['event'] == 'payment_observed' and e.get('detail', '').startswith('receipt='))
        href = '/actions#' + quote(action.id, safe='')
        awaiting_review = action.state == 'draft' or (action.state == 'owner_approved' and action.kind != 'defer_ops_expense')
        if awaiting_review and stale_plan(ledger, action):
            add('stale_plan', action.id, 'Ledger changed since this plan was reviewed',
                'A receipt, constraint, consent or other financial input changed. The existing action has not been silently updated.',
                'Recalculate plans and review this action against the latest facts before proceeding.', href,
                sources=sources, urgency='high')
        messages = {
            'draft': ('approval_needed', 'Draft awaiting owner review', 'The draft has no approval and changes no cash or consent.', 'Review its amount, date, evidence and current feasibility before approval.'),
            'owner_approved': ('approved_pending', 'Approved request needs the next owner step', 'Approval is separate from counterparty acceptance and payment.', 'Review the approved terms in Action History; any send uses the local sandbox only.'),
            'sent': ('response_pending', 'Waiting for counterparty response', 'A sandbox send is not acceptance or payment.', 'Review the pending request; record a response only through the explicit sandbox workflow.'),
            'counterparty_rejected': ('request_rejected', 'Request rejected — review alternatives', 'The rejection supplies no consent or cash.', 'Recalculate alternatives with the recorded constraints.'),
            'payment_observed': ('reconcile_needed', 'Observed receipt awaits reconciliation', 'Payment observation and reconciliation remain separate.', 'Review the recorded receipt and reconcile this action.'),
        }
        if action.state == 'counterparty_accepted' and action.kind == 'request_early_payment':
            message = ('payment_pending', 'Accepted request has no observed receipt', 'Counterparty acceptance is not spendable cash.', 'Record a receipt only when payment is actually observed in this synthetic sandbox.')
            if any(action.id in aids for aids in receipt_links(ledger).values()):
                message = None  # existing receipt work is represented in the review queue
        else:
            message = messages.get(action.state)
        if action.kind == 'defer_ops_expense' and action.state == 'owner_approved':
            message = None  # approval already recorded the expense schedule; no external response is needed
        if message:
            code, title, why, step = message
            try:
                day = date.fromisoformat(action.terms.get('date', ''))
            except (ValueError, TypeError):
                day = None
            add(code, action.id, title, why, step, href, day=day,
                currency=target.currency if target else action.terms.get('currency'),
                amount=action.terms.get('amount_minor'), sources=sources,
                urgency='high' if action.state == 'payment_observed' else 'review')
    linked_receipts = set(receipt_links(ledger))
    for original in ledger.receipts.values():
        receipt = effective_receipt(ledger, original.id)
        if receipt.amount_minor <= 0:
            continue
        review = ledger.receipt_reviews.get(receipt.id)
        if review and review['status'] == 'reconciled':
            continue
        if review and review['status'] == 'matched':
            add('receipt_review_reconcile', receipt.id, 'Matched receipt awaits owner reconciliation',
                'The receipt already exists; matching changes neither cash nor its invoice allocation.',
                'Review the source and confirm reconciliation separately.', '/receipts#receipt-' + quote(receipt.id, safe=''),
                day=receipt.date_received, currency=ledger.receivables[receipt.receivable_id].currency,
                amount=receipt.amount_minor, sources=[receipt.evidence_id, review['evidence_id']], urgency='high')
        elif receipt.state == 'observed' and receipt.id not in linked_receipts:
            rec = ledger.receivables.get(receipt.receivable_id)
            add('unlinked_receipt', receipt.id, 'Observed receipt has no reconciled action link',
                'The receipt is recorded; no linked payment-observed/reconciled action audit was found. This does not justify recording it again.',
                'Review this existing receipt and its recorded invoice allocation; match or reconcile without recording it again.', '/receipts#receipt-' + quote(receipt.id, safe=''),
                day=receipt.date_received, currency=rec.currency if rec else None,
                amount=receipt.amount_minor, sources=[receipt.evidence_id], urgency='high')
    from .completion import statement_report
    for batch in ledger.statement_batches.values():
        report=statement_report(ledger,batch['id'],ctx=ctx)
        pending=[r for r in report['rows'] if r['status'] in {'unreviewed','needs_review','unmatched'}]
        if pending:
            add('statement_review',batch['id'],'Review statement transaction matches',
                str(len(pending))+' source rows remain unmatched/unreviewed or changed after a correction. A statement import supplies no cash.',
                'Check source identity and current observations; no bank-balance verification is implied.',
                '/statements?batch_id='+quote(batch['id'],safe=''),sources=[batch['evidence_id']])
    from .workbench import promise_report
    from .obligations import statement_balance_report
    latest={c['batch_id']:c for c in ledger.statement_balance_checks.values()}
    for check in latest.values():
        result=statement_balance_report(ledger,check['id'],ctx=ctx)
        if result['problems']:
            add('statement_boundary',check['id'],'Review statement balance / continuity',
                ' '.join(result['problems']),
                'Review exact signed differences and source boundaries. Checking never adjusts cash or verifies the bank.',
                '/statement-balances?check_id='+quote(check['id'],safe=''),
                currency=check['currency'],sources=[check['evidence_id']],
                urgency='high' if result['arithmetic_delta_minor'] or result['closing_delta_minor'] else 'review')
    checked_batches=set(latest)
    for batch in ledger.statement_batches.values():
        if batch['id'] not in checked_batches:
            add('statement_boundary_missing',batch['id'],'Statement boundaries not reviewed',
                'Transaction matching alone supplies no opening/closing or period-continuity check.',
                'Enter source-attested account, period and opening/closing balances; no cash is changed.',
                '/statement-balances',sources=[batch['evidence_id']])
    from .operations import recurring_preview
    for template in ledger.recurring_bills.values():
        if not template['active']:continue
        try:
            preview=recurring_preview(ledger,template['id'],ledger.as_of+timedelta(days=min(119,ledger.horizon_days-1)),ctx=ctx)
        except CashflowError:
            add('recurring_pending',template['id'],'Recurring source state needs review',
                'An occurrence conflicts with its saved source state. No bills were generated.',
                'Inspect the recurring source/recovery history before confirming more occurrences.',
                '/recurring',currency=template['currency'],sources=[template['evidence_id']],urgency='high')
            continue
        pending=[r for r in preview['rows'] if not r['already_created']]
        if pending:
            add('recurring_pending',template['id'],'Recurring costs need owner confirmation',
                str(len(pending))+' proposed occurrences are not scheduled bills and are excluded from the forecast.',
                'Review sources and explicitly confirm occurrences; this queue never creates obligations.',
                '/recurring',currency=template['currency'],sources=[template['evidence_id']])
    for promise in promise_report(ledger,ctx=ctx):
        if promise['status'] in {'missed','needs_review'}:
            add('customer_promise_review',promise['id'],'Review customer payment promise',
                'Promise '+promise['status'].replace('_',' ')+'. A promise is not observed cash; changed receipt links need explicit review.',
                'Check the customer source and link only an already-observed receipt.', '/promises#promise-'+quote(promise['id'],safe=''),
                day=date.fromisoformat(promise['due_date']),currency=promise['currency'],amount=promise['remaining_minor'],sources=[promise['evidence_id']],urgency='high')
    from .treasury import treasury_report, statement_account, currency_accounts
    treasury = treasury_report(ledger, ctx=ctx)
    for row in treasury['accounts']:
        if row['available_minor'] < 0:
            add('account_cash_review',row['account_id'],'Account available cash is negative',
                'Recorded book cash is below source-backed restricted funds. Other accounts do not resolve this account automatically.',
                'Check observations, account assignments and restriction evidence. Never invent a balancing payment.',
                '/cash-accounts',currency=row['currency'],amount=-row['available_minor'],sources=[row['evidence_id']],urgency='critical')
    for batch in ledger.statement_batches.values():
        if not statement_account(ledger,batch['id']) and any(len(currency_accounts(ledger,r['currency']))>1 for r in batch['rows']):
            add('statement_account_required',batch['id'],'Confirm statement account scope',
                'Several accounts share this currency; currency alone cannot identify a bank account.',
                'Review and bind the imported source to one account before matching.', '/cash-accounts',sources=[batch['evidence_id']])
    if ledger.forecast_routes or any(len(currency_accounts(ledger,a.currency))>1 for a in ledger.accounts.values()):
        from .funding import account_funding
        funding=account_funding(ledger,ctx=ctx)
        for row in funding['unassigned']:
            add('future_account_review',row['kind']+':'+row['item_id'],'Review future cash account',
                'Expected account is '+row['status']+'. A pooled currency balance cannot prove this bill/collection is funded in the right account.',
                'Review the same-currency source-backed future account assumption.', '/account-forecast',currency=row['currency'],sources=[row['source_evidence_id']])
        for row in funding['accounts']:
            curve=row['forecast']
            if curve.first_breach_date:
                add('future_account_shortfall',row['account_id'],'Account funding shortfall among assigned flows',
                    'Conservative per-account forecast breaches its floor; unresolved routes/timing can change this result.',
                    'Review dates, account assumptions and already-observed cash; no balancing transfer is assumed.', '/account-forecast',
                    currency=row['currency'],day=curve.first_breach_date,amount=curve.worst_shortfall_minor,sources=[row['evidence_id']],urgency='high')
    for job in ledger.agent_workflows.values():
        if job['business_id']==ledger.business_id and job['status']=='pending_approval':
            add('workflow_approval_needed',job['id'],'Review pending workflow: '+job['label'],
                'Pending operations have no financial effect. Review exact terms; approval alone proves no payment or counterparty acceptance.',
                'Open the exact workflow and approve or reject it as owner.',
                '/agent/workflows/'+quote(job['id'],safe=''))
    order = {'critical': 0, 'high': 1, 'review': 2}
    return sorted(items, key=lambda i: (order[i['urgency']], i['date'] or '9999', i['id']))
