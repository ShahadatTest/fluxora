"""Read-only payment-delay scenarios and reviewed plan-to-draft conversion.

All financial calculations use the existing deterministic engine. Scenario
dates and draft terms confer neither approval nor counterparty consent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone

from . import actions
from .authz import OP_DRAFT, OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .forecast import forecast
from .money import format_amount
from .plans import (
    build_advance_plan, build_plan_a, build_plan_b_full,
    build_plan_b_supplier_only, evaluate_plan,
)


@dataclass
class StressResult:
    receivable_id: str
    evidence_id: str
    recorded_date: date
    assumed_date: date
    delay_days: int
    remaining_minor: int
    currency: str
    baseline: object
    scenario: object


def stress_test(ledger, receivable_id: str, delay_days: int, *, ctx) -> StressResult:
    require_access(ctx, ledger, OP_READ)
    if type(delay_days) is not int or not 0 <= delay_days <= 30:
        raise CashflowError('Delay must be a whole number from 0 to 30 days.')
    rec = ledger.receivables.get(receivable_id)
    if rec is None:
        raise CashflowError('Choose an invoice from this business.')
    from .settlement import collection_held
    if collection_held(rec):raise CashflowError('Resolve/release this collection hold before testing a payment delay.')
    if rec.remaining_minor <= 0:
        raise CashflowError('This invoice has no remaining balance to stress test.')
    if rec.expected_date is None:
        raise CashflowError('The invoice has no expected collection date. Confirm it before testing a delay.')
    if not any(account.currency == rec.currency for account in ledger.accounts.values()):
        raise CashflowError('There is no cash account for this invoice currency. Confirm its opening balance before testing.')
    assumed = rec.expected_date + timedelta(days=delay_days)
    work = copy.copy(ledger)
    work.receivables = dict(ledger.receivables)
    work.receivables[rec.id] = replace(rec, expected_date=assumed)
    return StressResult(rec.id, rec.evidence_id, rec.expected_date, assumed,
                        delay_days, rec.remaining_minor, rec.currency,
                        forecast(ledger, ledger.as_of, ledger.horizon_days),
                        forecast(work, ledger.as_of, ledger.horizon_days))


def _encode(value):
    from decimal import Decimal
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Decimal):
        # FX rates are exact Decimal metadata, never spendable cash or float.
        # A typed representation keeps them distinct from ordinary strings.
        return {'decimal': str(value)}
    raise TypeError('Unsupported planning input')


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, default=_encode, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _catalogue(ledger, plan_id):
    from .adaptive import AUTO_PLANS
    if plan_id in AUTO_PLANS:
        return None  # generation happens after idempotent batch replay
    consent = ledger.consent_evidence_id('supplier-001')
    builders = {'plan-a': build_plan_a,
                'plan-b-supplier-only': lambda: build_plan_b_supplier_only(consent),
                'plan-b': lambda: build_plan_b_full(consent),
                'plan-advance-capped': lambda: build_advance_plan(400_000)}
    if plan_id not in builders:
        raise CashflowError('Unknown plan. Choose a plan from Plan Comparison.')
    return builders[plan_id]()


def _revision(ledger, plan_id, specs):
    # Actions/audit are excluded: draft creation itself changes no financial
    # input. Invoices, constraints, actual receipts and consent do invalidate.
    fields = ('business_id', 'base_currency', 'as_of', 'horizon_days',
              'accounts', 'receivables', 'outflows', 'receipts', 'constraints',
              'applied_event_ids', 'runtime_evidence_ids', 'consent_map', 'evidence', 'collection_date_sources', 'receipt_adjustments', 'duplicate_reviews', 'split_receipts',
              'split_adjustments','cash_movements','payment_acknowledgements','payment_pair_reviews','date_corrections','unallocated_refunds','credit_transfers','loans')
    from .treasury import STATE_FIELDS
    fields += STATE_FIELDS
    return _hash({'schema': 1, 'plan_id': plan_id, 'drafts': specs,
                  'ledger': {key: getattr(ledger, key) for key in fields}})


def plan_draft_preview(ledger, plan_id: str, *, ctx) -> dict:
    require_access(ctx, ledger, OP_READ)
    from .payment_review import payment_warnings
    if any(p['status'] in {'review_required','possible_duplicate'} for p in payment_warnings(ledger,ctx=ctx)):
        raise CashflowError('Review possible duplicate observed payments before relying on new plan drafts.')
    from .operations import duplicate_pairs
    if any(p['review'] and p['review']['verdict']=='confirmed_duplicate' for p in duplicate_pairs(ledger,ctx=ctx)):
        raise CashflowError('Correct/review the confirmed duplicate source before relying on another plan draft.')
    plan = _catalogue(ledger, plan_id)
    if plan is None:
        from .adaptive import generate_proposals, overlapping_actions
        proposal = next(p for p in generate_proposals(ledger, ctx=ctx) if p.id == plan_id)
        if proposal.blockers:
            raise CashflowError('Confirm the missing ledger facts before drafting: ' + '; '.join(proposal.blockers))
        if not proposal.drafts:
            raise CashflowError('This strategy has no new eligible draft requests. Review the current forecast and unresolved gap.')
        conflicts = overlapping_actions(ledger, proposal.drafts)
        if conflicts:
            raise CashflowError('Review the existing action(s) for these targets before creating another request: ' + ', '.join(conflicts))
        return {'plan_id': plan_id, 'name': proposal.name, 'drafts': proposal.drafts,
            'revision': _hash({'inputs': _revision(ledger, plan_id, proposal.drafts),
                'discarded': sorted(a.id for a in ledger.actions.values() if a.state == 'cancelled')}),
            'arithmetic_only': True,
            'feasible': proposal.financially_sufficient,
            'reasons': proposal.notes or ['All currency floors hold only if the proposed terms and recorded collection assumptions occur.'],
            'assumptions': proposal.pending_permissions + [
                'Conditional arithmetic is not authorization or a promise of payment.',
                'Bill extensions are limited to 14 days and a dated recovery inside the forecast window.'],
            'evidence_ids': proposal.evidence_ids,
            'note': 'Draft requests only. A draft is not approval, consent, a receipt or a payment.'}
    specs = []
    sources = {item.evidence_id for collection in
        (ledger.accounts, ledger.receivables, ledger.outflows, ledger.receipts)
        for item in collection.values() if item.evidence_id}
    totals = {}
    for pr in plan.projected_receipts:
        rec = ledger.receivables.get(pr.receivable_id)
        if rec is None:
            raise CashflowError('The plan invoice is unavailable. Review the imported ledger.')
        from .settlement import collection_held
        if collection_held(rec):raise CashflowError('This preset relies on a held/disputed invoice. Review/release its hold first.')
        if rec.currency != ledger.base_currency:
            raise CashflowError('This preset requires an invoice in the business base currency; no FX conversion is applied.')
        totals[rec.id] = totals.get(rec.id, 0) + pr.amount_minor
        if totals[rec.id] > rec.remaining_minor:
            raise CashflowError('This preset assumes more collection than the invoice remaining balance. Review the plan again.')
        if pr.amount_minor <= 0:
            raise CashflowError('The plan collection amount must be positive.')
        sources.add(rec.evidence_id)
    # Plan A's later residual is an assumption, not a second advance request.
    if plan.projected_receipts:
        advance = min(plan.projected_receipts, key=lambda pr: pr.date)
        rec = ledger.receivables[advance.receivable_id]
        cap = ledger.constraints.max_advance_minor
        if cap is not None and advance.amount_minor > cap:
            raise CashflowError('The requested advance exceeds the owner limit.')
        if advance.date < ledger.as_of:
            raise CashflowError('The preset collection date is before the ledger as-of date. Review a current plan.')
        specs.append({'kind': 'request_early_payment', 'receivable_id': rec.id,
            'amount_minor': advance.amount_minor, 'currency': rec.currency,
            'amount': format_amount(advance.amount_minor, rec.currency),
            'date': advance.date.isoformat()})
    for amendment in plan.amendments:
        out = ledger.outflows.get(amendment.outflow_id)
        if out is None:
            raise CashflowError('A plan bill is unavailable. Review the imported ledger.')
        if out.protected:
            raise CashflowError('A protected obligation cannot become a rescheduling draft.')
        if out.paid_minor or out.agreement_schedules:
            raise CashflowError('A paid or partially paid bill requires manual terms review, not a preset deferral.')
        sources.add(out.evidence_id)
        if amendment.new_date is not None:
            if not ledger.constraints.allow_supplier_date_change:
                raise CashflowError('Supplier date changes are not permitted by the owner constraints.')
            if amendment.new_date == out.date:
                continue  # already recorded; do not request the same change
            kind, amount, new_date = 'request_supplier_extension', out.amount_minor, amendment.new_date
        else:
            if not ledger.constraints.allow_ops_deferral:
                raise CashflowError('Operations deferral is not permitted by the owner constraints.')
            if list(out.recorded_splits or []) == list(amendment.splits or []):
                continue
            delayed = [(d, m) for d, m in (amendment.splits or []) if d > out.date]
            if len(delayed) != 1:
                raise CashflowError('This schedule needs manual review before creating a draft.')
            new_date, amount = delayed[0]
            if not 0 < amount <= out.maximum_deferrable_minor:
                raise CashflowError('The deferral exceeds the owner-designated limit.')
            kind = 'defer_ops_expense'
        if new_date < ledger.as_of:
            raise CashflowError('The proposed bill date is before the ledger as-of date.')
        specs.append({'kind': kind, 'outflow_id': out.id, 'amount_minor': amount,
            'currency': out.currency, 'amount': format_amount(amount, out.currency),
            'date': new_date.isoformat()})
    if not specs:
        raise CashflowError('These plan changes are already recorded. No new draft is needed.')
    if any(eid not in ledger.evidence for eid in sources):
        raise CashflowError('A source evidence record is missing. Confirm the inputs before drafting.')
    result = evaluate_plan(ledger, plan, ledger.constraints)
    return {'plan_id': plan_id, 'name': plan.name, 'drafts': specs,
        'revision': _revision(ledger, plan_id, specs),
        'feasible': result.feasible, 'reasons': result.reasons,
        'assumptions': plan.conditional_labels, 'evidence_ids': sorted(sources),
        'note': 'Draft requests only. A draft is not approval, consent, a receipt or a payment.'}


def create_plan_drafts(ledger, plan_id: str, revision: str, *, ctx) -> dict:
    require_access(ctx, ledger, OP_DRAFT)  # includes business check BEFORE replay
    _catalogue(ledger, plan_id)
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{64}', revision):
        raise CashflowError('Reopen the plan review before creating drafts.')
    prefix = f'act-plan-{_hash([ledger.business_id, plan_id, revision])[:24]}-'
    existing = [a for aid, a in ledger.actions.items() if aid.startswith(prefix)]
    if existing:
        manifests = [json.loads(e['detail']) for e in existing[0].audit
                     if e['event'] == 'plan_drafts_created']
        if len(manifests) != 1:
            raise IdempotencyConflict('The draft batch identifier is already in use.')
        record = manifests[0]
        ids = record.get('action_ids', [])
        if (record.get('revision') != revision or record.get('plan_id') != plan_id
                or not ids or set(ids) != {a.id for a in existing}
                or any(a.business_id != ledger.business_id for a in existing)):
            raise IdempotencyConflict('The stored draft batch is incomplete or conflicts with this review.')
        for action in existing:
            entries = [json.loads(e['detail']) for e in action.audit
                       if e['event'] == 'plan_drafts_created']
            if entries != [record]:
                raise IdempotencyConflict('The stored draft batch audit does not match.')
        # Return original IDs even after edits/approvals/receipts. Never reset
        # progressed actions or recreate removed members of a batch.
        return {'action_ids': ids, 'duplicate': True}
    preview = plan_draft_preview(ledger, plan_id, ctx=ctx)
    if preview['revision'] != revision:
        raise CashflowError('The ledger changed after review. Reopen the plan and review current terms.')
    ids = [f'{prefix}{i + 1}' for i in range(len(preview['drafts']))]
    record = {'plan_id': plan_id, 'revision': revision, 'action_ids': ids,
              'input_revision': _revision(ledger, 'financial-inputs', []),
              'evidence_ids': preview['evidence_ids']}
    pending = []
    for aid, spec in zip(ids, preview['drafts']):
        action = actions.create_action(aid, spec['kind'],
            dict(spec, source_plan_id=plan_id), actor=ctx.actor, ctx=ctx,
            business_id=ledger.business_id)
        action.audit.append({'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'event': 'plan_drafts_created', 'actor': ctx.actor,
            'detail': json.dumps(record, sort_keys=True), 'state': 'draft', 'version': 1})
        pending.append(action)
    ledger.actions.update({a.id: a for a in pending})
    ledger.log_audit('plan_drafts_created', ctx.actor,
                     f'{plan_id}: {", ".join(ids)}; drafts only')
    return {'action_ids': ids, 'duplicate': False}
