"""Explicit owner corrections, existing-receipt review and daily cash close.

Documents are cited evidence. Only an authorized owner operation confirms
facts/reconciliation. An imported observation becomes a receipt only after
explicit owner verification of observed payment and invoice; no consent is granted.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import actions
from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .models import Evidence, Receipt
from .planning import _hash, _revision
from .settlement import effective_receipt, receipt_fingerprint, cash_events, bill_schedule


def review_revision(ledger):
    from .treasury import STATE_FIELDS
    return _hash({'treasury': {name: getattr(ledger, name) for name in STATE_FIELDS},
        'financial': _revision(ledger, 'owner-review', []),
        'actions': ledger.actions, 'reviews': ledger.receipt_reviews,
        'receipt_inbox': ledger.receipt_inbox,
        'split_receipts': ledger.split_receipts, 'fact_checks': ledger.fact_checks,
        'statement_batches': ledger.statement_batches, 'statement_reviews': ledger.statement_reviews,
        'entry_records': ledger.entry_records, 'instalment_proposals':ledger.instalment_proposals, 'scenarios':ledger.scenarios,
        'forecast_snapshots': ledger.forecast_snapshots,
        'recurring_bills': ledger.recurring_bills, 'recurring_occurrences': ledger.recurring_occurrences,
        'duplicate_reviews': ledger.duplicate_reviews, 'invoice_due_sources': ledger.invoice_due_sources,
        'setup_receipts':ledger.setup_receipts,'payment_promises':ledger.payment_promises,
        'attachments':{k:{f:v for f,v in a.items() if f!='payload_base64'} for k,a in ledger.evidence_attachments.items()},
        'attachment_import_receipts':ledger.attachment_import_receipts,
        'statement_balance_checks':ledger.statement_balance_checks,
        'owner_operation_ids': sorted(ledger.owner_operations)})


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 1000:
        raise CashflowError(f'{label} must contain 1–1000 characters.')
    return value.strip()


def _evidence(ledger, evidence_id):
    if evidence_id not in ledger.evidence:
        raise CashflowError('Choose an existing source evidence record. A document never grants authority.')


def _begin(ledger, ctx, kind, revision, payload):
    require_access(ctx, ledger)  # authorization before replay and before any work
    key = _hash([ledger.business_id, kind, revision, payload])
    old = ledger.owner_operations.get(key)
    if old is not None:
        return key, dict(old['result'], duplicate=True)
    if revision != review_revision(ledger):
        raise CashflowError('The ledger changed after review. Reload and review the current facts before confirming.')
    from .obligations import guard_credit_operation
    guard_credit_operation(ledger, kind, payload)
    return key, None


def _finish(ledger, key, kind, payload, result, ctx):
    ledger.owner_operations[key] = {'kind': kind, 'actor': ctx.actor, 'payload': payload, 'result': result}
    ledger.log_audit(kind, ctx.actor, __import__('json').dumps({'operation': key, **payload}, sort_keys=True))
    return dict(result, duplicate=False)


def import_observations(ledger, rows, file_digest, revision, *, ctx):
    payload = dict(rows=rows, file_digest=file_digest)
    key, duplicate = _begin(ledger, ctx, 'cash_observations_imported', revision, payload)
    if duplicate:
        return duplicate
    if not isinstance(file_digest, str) or not __import__('re').fullmatch('[a-f0-9]{64}', file_digest):
        raise CashflowError('Observation import requires its source file SHA-256 digest.')
    if not rows or len(rows) > 100:
        raise CashflowError('Import 1–100 synthetic observation rows at a time.')
    pending, ids = [], []
    for row in rows:
        if set(row) != {'id', 'amount_minor', 'currency', 'date', 'invoice_reference'}:
            raise CashflowError('Unexpected observation fields.')
        if not isinstance(row['invoice_reference'], str) or len(row['invoice_reference']) > 128:
            raise CashflowError('Invoice reference must contain at most 128 characters.')
        if not __import__('re').fullmatch('[A-Za-z0-9_-]{1,64}', row['id']):
            raise CashflowError('Observation IDs must use 1–64 letters, digits, dashes or underscores.')
        if row['id'] in ids:
            raise CashflowError('Observation IDs must be unique inside the file.')
        ids.append(row['id'])
        if type(row['amount_minor']) is not int or row['amount_minor'] <= 0:
            raise CashflowError('Observation amounts must be positive integer minor units.')
        if row['currency'] not in {a.currency for a in ledger.accounts.values()}:
            raise CashflowError('An observation currency needs its own cash account; no FX conversion is applied.')
        try: date.fromisoformat(row['date'])
        except (TypeError, ValueError): raise CashflowError('Each observation needs a valid date.')
        prior = ledger.receipt_inbox.get(row['id'])
        if prior:
            if any(prior[k] != row[k] for k in row):
                raise IdempotencyConflict('Observation ID already has different data; no merge or overwrite occurred.')
            continue
        from .extension import refund_entries
        if row['id'] in ledger.unallocated_refunds or any(r['id']==row['id'] for _,_,r in refund_entries(ledger)):
            raise IdempotencyConflict('Observation ID already belongs to a recorded refund.')
        if row['id'] in ledger.receipts:
            raise IdempotencyConflict('Observation ID already belongs to a recorded receipt; do not import the same movement under another path.')
        eid = 'ev-observation-' + _hash([ledger.business_id, row, file_digest])[:24]
        pending.append((row, eid))
    for row, eid in pending:
        ledger.evidence[eid] = Evidence(eid, 'synthetic_imported_cash_observation',
            f'Imported synthetic observation {row["id"]}: {row["currency"]} {row["amount_minor"]} minor, '
            f'date {row["date"]}, invoice reference {row["invoice_reference"] or "unknown"}; file sha256 {file_digest}. '
            'Unallocated observation only until the owner confirms an observed payment and invoice; this import itself creates no cash.')
        ledger.receipt_inbox[row['id']] = dict(row, evidence_id=eid, file_digest=file_digest, status='unallocated')
    return _finish(ledger, key, 'cash_observations_imported', payload,
        dict(observation_ids=ids, new_count=len(pending)), ctx)


def observation_candidates(ledger, observation_id, *, ctx):
    require_access(ctx, ledger, OP_READ)
    row = ledger.receipt_inbox.get(observation_id)
    if row is None:
        raise CashflowError('Unknown cash observation for this business.')
    candidates = []
    if row['status'] == 'unallocated':
        for rec in sorted(ledger.receivables.values(), key=lambda r: r.id):
            if rec.currency != row['currency'] or row['amount_minor'] > rec.remaining_minor:
                continue
            exact = bool(row['invoice_reference'] and row['invoice_reference'] == rec.id)
            candidates.append(dict(invoice_id=rec.id, remaining_minor=rec.remaining_minor,
                partial=row['amount_minor'] < rec.remaining_minor, exact_reference=exact,
                explanation='Exact invoice reference; currency and outstanding amount fit.' if exact else
                    'Currency and outstanding amount fit; invoice identity is unverified. Owner must check source/reference.'))
        candidates.sort(key=lambda c: (not c['exact_reference'], c['invoice_id']))
    duplicates = sorted(r.id for r in (effective_receipt(ledger, rid) for rid in ledger.receipts) if r.state == 'observed' and r.amount_minor > 0
        and r.date_received.isoformat() == row['date'] and r.amount_minor == row['amount_minor']
        and ledger.receivables[r.receivable_id].currency == row['currency'])
    pending_duplicates = sorted(rid for rid, other in ledger.receipt_inbox.items()
        if rid != observation_id and other['date'] == row['date'] and other['amount_minor'] == row['amount_minor']
        and other['currency'] == row['currency'])
    return dict(observation=dict(row), candidates=candidates, possible_duplicate_ids=sorted(set(duplicates + pending_duplicates)),
        revision=review_revision(ledger))


def allocate_observation(ledger, observation_id, invoice_id, note, acknowledge_duplicates, revision, *, ctx):
    payload = dict(observation_id=observation_id, invoice_id=invoice_id, note=note, acknowledge_duplicates=acknowledge_duplicates)
    key, duplicate = _begin(ledger, ctx, 'observation_allocated_as_observed_receipt', revision, payload)
    if duplicate:
        return duplicate
    info = observation_candidates(ledger, observation_id, ctx=ctx)
    row = info['observation']
    if row['status'] != 'unallocated':
        raise IdempotencyConflict('This observation was already recorded. Review the existing receipt without recording it again.')
    if invoice_id not in {c['invoice_id'] for c in info['candidates']}:
        raise CashflowError('Choose an outstanding invoice of the same currency with capacity for this observed amount.')
    if info['possible_duplicate_ids'] and acknowledge_duplicates is not True:
        raise CashflowError('Review and acknowledge the possible duplicate observation/receipt IDs before recording.')
    _text(note, 'Observation allocation note')
    _evidence(ledger, row['evidence_id'])
    observed = Receipt(observation_id, invoice_id, row['amount_minor'], date.fromisoformat(row['date']), evidence_id=row['evidence_id'], actor=ctx.actor)
    ledger.record_receipt(observed, actor=ctx.actor, ctx=ctx, acknowledge_duplicates=acknowledge_duplicates)
    ledger.receipt_inbox[observation_id].update(status='recorded', invoice_id=invoice_id,
        owner_note=note.strip(), duplicate_ids_reviewed=info['possible_duplicate_ids'])
    return _finish(ledger, key, 'observation_allocated_as_observed_receipt', payload,
        dict(receipt_id=observation_id, status='observed'), ctx)


def dismiss_observation(ledger, observation_id, note, revision, *, ctx):
    payload = dict(observation_id=observation_id, note=note)
    key, duplicate = _begin(ledger, ctx, 'unallocated_observation_dismissed', revision, payload)
    if duplicate:
        return duplicate
    row = ledger.receipt_inbox.get(observation_id)
    if row is None or row['status'] != 'unallocated':
        raise CashflowError('Only an unallocated source observation may be dismissed. Recorded receipts cannot be reversed here.')
    _text(note, 'Reason for dismissing this source row')
    row.update(status='dismissed', owner_note=note.strip())
    return _finish(ledger, key, 'unallocated_observation_dismissed', payload,
        dict(observation_id=observation_id, status='dismissed'), ctx)


def confirm_collection_date(ledger, invoice_id, expected_date, evidence_id, reason, revision, *, ctx):
    payload = dict(invoice_id=invoice_id, expected_date=str(expected_date), evidence_id=evidence_id, reason=reason)
    key, duplicate = _begin(ledger, ctx, 'collection_date_confirmed', revision, payload)
    if duplicate:
        return duplicate
    rec = ledger.receivables.get(invoice_id)
    if rec is None or rec.remaining_minor <= 0:
        raise CashflowError('Choose an outstanding invoice from this business.')
    if type(expected_date) is not date or expected_date < ledger.as_of:
        raise CashflowError('Confirm an expected collection date on or after the ledger snapshot. No payment is recorded.')
    _evidence(ledger, evidence_id)
    reason = _text(reason, 'Reason')
    old = rec.expected_date.isoformat() if rec.expected_date else None
    eid = 'ev-collection-date-' + key[:24]
    ledger.evidence[eid] = Evidence(eid, 'owner_confirmed_collection_expectation',
        f'Owner {ctx.actor} confirmed {invoice_id} expected date {old or "unknown"} → {expected_date.isoformat()}; '
        f'source {evidence_id}; reason: {reason}. Expectation only; not consent or an observed payment.')
    rec.expected_date = expected_date
    ledger.collection_date_sources[rec.id] = eid
    from .business_tools import _save_fact_check
    _save_fact_check(ledger, 'collection', rec.id, evidence_id, reason, ctx=ctx)
    payload.update(old_expected_date=old, confirmation_evidence=eid)
    return _finish(ledger, key, 'collection_date_confirmed', payload,
        dict(invoice_id=invoice_id, confirmation_evidence=eid), ctx)


def receipt_links(ledger):
    """receipt ID -> action IDs from explicit historic observations/reviews."""
    links = {}
    for action in ledger.actions.values():
        for event in action.audit:
            if event['event'] == 'payment_observed' and event.get('detail', '').startswith('receipt='):
                rid = event['detail'][8:]
                if not any(c['receipt_id'] == rid for c in ledger.receipt_adjustments.values()):
                    links.setdefault(rid, set()).add(action.id)
    for rid, review in ledger.receipt_reviews.items():
        if review.get('action_id'):
            links.setdefault(rid, set()).add(review['action_id'])
    return links


def receipt_candidates(ledger, receipt_id, *, ctx):
    require_access(ctx, ledger, OP_READ)
    receipt = effective_receipt(ledger, receipt_id) if receipt_id in ledger.receipts else None
    if receipt is None or receipt.state != 'observed':
        raise CashflowError('Choose an existing observed receipt. Matching never creates a payment.')
    invoice = ledger.receivables.get(receipt.receivable_id)
    if invoice is None:
        raise CashflowError('This receipt has no valid recorded invoice allocation; correct the import before reviewing.')
    linked = receipt_links(ledger)
    existing = linked.get(receipt_id, set())
    duplicates = sorted(r.id for r in (effective_receipt(ledger, rid) for rid in ledger.receipts) if r.id != receipt_id
        and r.state == 'observed' and r.receivable_id == receipt.receivable_id
        and r.amount_minor == receipt.amount_minor and r.date_received == receipt.date_received)
    candidates = []
    for action in sorted(ledger.actions.values(), key=lambda a: a.id):
        if (action.business_id and action.business_id != ledger.business_id) or action.kind != 'request_early_payment':
            continue
        if action.terms.get('receivable_id') != invoice.id or action.terms.get('currency', invoice.currency) != invoice.currency:
            continue
        if action.approved_version != action.version or action.state not in {'owner_approved', 'sent', 'counterparty_accepted', 'payment_observed', 'reconciled'}:
            continue
        amount = action.terms.get('amount_minor')
        if type(amount) is not int or amount <= 0:
            continue
        used = sum(effective_receipt(ledger, rid).amount_minor for rid, aids in linked.items()
            if action.id in aids and rid != receipt_id and rid in ledger.receipts)
        available = amount - used
        if 0 < receipt.amount_minor <= available and (not existing or existing == {action.id}):
            candidates.append(dict(action_id=action.id, state=action.state,
                remaining_request_minor=available, partial=receipt.amount_minor < available,
                explanation='Same recorded invoice reference and currency; amount fits the unlinked request balance. '
                    + ('Receipt date matches the requested date.' if action.terms.get('date') == receipt.date_received.isoformat()
                       else 'Receipt date differs from the requested date; owner review required.')))
    return dict(receipt_id=receipt_id, invoice_id=invoice.id, currency=invoice.currency,
        amount_minor=receipt.amount_minor, evidence_id=receipt.evidence_id,
        linked_action_ids=sorted(existing), candidates=candidates, possible_duplicate_ids=duplicates,
        review=ledger.receipt_reviews.get(receipt_id), revision=review_revision(ledger))


def review_receipt(ledger, receipt_id, action_id, evidence_id, note, acknowledge_duplicates, revision, *, ctx):
    payload = dict(receipt_id=receipt_id, action_id=action_id, evidence_id=evidence_id,
        note=note, acknowledge_duplicates=acknowledge_duplicates)
    key, duplicate = _begin(ledger, ctx, 'existing_receipt_matched', revision, payload)
    if duplicate:
        return duplicate
    details = receipt_candidates(ledger, receipt_id, ctx=ctx)
    if details['amount_minor'] <= 0:
        raise CashflowError('This receipt was fully reversed. Inspect the correction history; do not match it as new cash.')
    if receipt_id in ledger.receipt_reviews:
        raise IdempotencyConflict('This receipt already has an owner review. Its allocation/review cannot be silently overwritten.')
    _evidence(ledger, evidence_id)
    _text(note, 'Review note')
    if details['possible_duplicate_ids'] and acknowledge_duplicates is not True:
        raise CashflowError('Review the possible duplicate IDs and explicitly acknowledge them. No receipt will be merged or deleted.')
    links = details['linked_action_ids']
    if len(links) > 1 or (links and links != [action_id]):
        raise CashflowError('This receipt is already linked to another action. Preserve that link and review its audit.')
    if action_id and action_id not in {a['action_id'] for a in details['candidates']}:
        raise CashflowError('Choose an eligible payment request for this invoice/currency and unlinked amount. A draft or unapproved action cannot be matched.')
    ledger.receipt_reviews[receipt_id] = dict(receipt_id=receipt_id, invoice_id=details['invoice_id'],
        action_id=action_id, evidence_id=evidence_id, note=note.strip(), actor=ctx.actor, status='matched',
        duplicate_ids_reviewed=details['possible_duplicate_ids'],
        receipt_fingerprint=receipt_fingerprint(ledger, receipt_id), action_version=ledger.actions[action_id].version if action_id else None)
    return _finish(ledger, key, 'existing_receipt_matched', payload, dict(receipt_id=receipt_id, status='matched'), ctx)


def reconcile_existing_receipt(ledger, receipt_id, evidence_id, note, revision, *, ctx):
    payload = dict(receipt_id=receipt_id, evidence_id=evidence_id, note=note)
    key, duplicate = _begin(ledger, ctx, 'existing_receipt_reconciled', revision, payload)
    if duplicate:
        return duplicate
    review = ledger.receipt_reviews.get(receipt_id)
    receipt = ledger.receipts.get(receipt_id)
    if review is None or receipt is None or review['status'] != 'matched':
        raise CashflowError('First review and match this existing receipt, then reconcile separately.')
    current_fingerprint = receipt_fingerprint(ledger, receipt_id)
    legacy_fingerprint = _hash(receipt) if not any(c['receipt_id'] == receipt_id for c in ledger.receipt_adjustments.values()) else None
    if review['receipt_fingerprint'] not in {current_fingerprint, legacy_fingerprint}:
        raise CashflowError('The receipt changed since matching; reconciliation is refused.')
    _evidence(ledger, evidence_id)
    _text(note, 'Reconciliation note')
    if review['action_id']:
        action = ledger.actions[review['action_id']]
        if action.version != review['action_version'] or action.approved_version != action.version:
            raise CashflowError('The linked action terms changed after matching; review the audit before reconciling.')
        # Only an already-observed receipt event can reconcile the action.
        # Matching never manufactures acceptance or changes an earlier state.
        observed_here = any(e['event'] == 'payment_observed' and e.get('detail') == 'receipt=' + receipt_id for e in action.audit)
        if action.state == 'payment_observed' and observed_here:
            actions.reconcile(action, actor=ctx.actor, ctx=ctx)
    review.update(status='reconciled', reconciliation_evidence_id=evidence_id, reconciliation_note=note.strip())
    return _finish(ledger, key, 'existing_receipt_reconciled', payload, dict(receipt_id=receipt_id, status='reconciled'), ctx)


def reopen_receipt_review(ledger, receipt_id, note, revision, *, ctx):
    payload = dict(receipt_id=receipt_id, note=note)
    key, duplicate = _begin(ledger, ctx, 'receipt_match_reopened', revision, payload)
    if duplicate:
        return duplicate
    _text(note, 'Reason for reopening')
    review = ledger.receipt_reviews.get(receipt_id)
    if review is None or review['status'] != 'matched':
        raise CashflowError('Only a matched, unreconciled receipt review may be reopened. Reconciled reviews remain in history.')
    payload['previous_review'] = dict(review)
    del ledger.receipt_reviews[receipt_id]
    return _finish(ledger, key, 'receipt_match_reopened', payload, dict(receipt_id=receipt_id, status='review_required'), ctx)


def daily_preview(ledger, new_as_of, *, ctx):
    require_access(ctx, ledger, OP_READ)
    if type(new_as_of) is not date or not 1 <= (new_as_of - ledger.as_of).days <= 366:
        raise CashflowError('Choose a later snapshot date, at most 366 days ahead.')
    currencies = [a.currency for a in ledger.accounts.values()]
    if not currencies:
        raise CashflowError('Daily close requires opening cash accounts.')
    rows = []
    events = list(cash_events(ledger, include_internal=True))
    for account in ledger.accounts.values():
        selected = [e for e in events if e['account_id'] == account.id and ledger.as_of <= e['date'] < new_as_of]
        receipts = [e for e in selected if e['kind'] in {'observed_receipt','receipt_correction'}]
        paid = [e for e in selected if e['kind'] in {'observed_bill_payment','bill_payment_correction'}]
        other = [e for e in selected if e['kind'] in {'other_cash_observed','other_cash_correction','refund_observed','refund_correction','unallocated_refund_observed','unallocated_refund_correction'} or e['kind'].startswith('internal_transfer')]
        calculated = account.opening_minor + sum(e['amount_minor'] for e in selected)
        rows.append(dict(account_id=account.id, currency=account.currency, old_opening_minor=account.opening_minor,
            old_evidence_id=account.evidence_id,
            observed_receipt_ids=sorted(e['id'] for e in receipts), paid_bill_ids=sorted(e['outflow_id'] for e in paid),
            observed_inflow_minor=sum(e['amount_minor'] for e in receipts), observed_outflow_minor=-sum(e['amount_minor'] for e in paid),
            other_cash_minor=sum(e['amount_minor'] for e in other), other_cash_ids=[e['id'] for e in other],
            calculated_opening_minor=calculated))
    overdue = sorted(o.id for o in ledger.outflows.values() if o.status == 'scheduled'
        and any(d < new_as_of and m > 0 for d, m in bill_schedule(o)))
    return dict(old_as_of=ledger.as_of.isoformat(), new_as_of=new_as_of.isoformat(), accounts=rows,
        unpaid_before_snapshot=overdue, revision=review_revision(ledger))


def close_day(ledger, new_as_of, confirmations, note, revision, *, ctx):
    payload = dict(new_as_of=str(new_as_of), confirmations=confirmations, note=note)
    key, duplicate = _begin(ledger, ctx, 'daily_cash_snapshot_confirmed', revision, payload)
    if duplicate:
        return duplicate
    preview = daily_preview(ledger, new_as_of, ctx=ctx)
    _text(note, 'Cash snapshot note')
    if set(confirmations) != {a.id for a in ledger.accounts.values()}:
        raise CashflowError('Explicitly confirm every separate currency account.')
    for account_id, values in confirmations.items():
        if set(values) != {'opening_minor', 'evidence_id'} or type(values['opening_minor']) is not int:
            raise CashflowError('Each cash snapshot needs an integer amount and source evidence.')
        _evidence(ledger, values['evidence_id'])
    # Validate all fields before changing any snapshot.
    ledger.roll_forward(new_as_of, ctx=ctx)
    adjustments = []
    for row in preview['accounts']:
        account = ledger.accounts[row['account_id']]
        values = confirmations[account.id]
        variance = values['opening_minor'] - row['calculated_opening_minor']
        eid = 'ev-cash-snapshot-' + _hash([key, account.id])[:24]
        ledger.evidence[eid] = Evidence(eid, 'owner_confirmed_cash_snapshot',
            f'Owner {ctx.actor} confirmed opening {account.currency} {values["opening_minor"]} minor at {new_as_of.isoformat()}; '
            f'calculated from settled records {row["calculated_opening_minor"]}; explicit snapshot variance {variance}; '
            f'source {values["evidence_id"]}; note: {note.strip()}. No new receipt or payment was created.')
        account.opening_minor, account.evidence_id = values['opening_minor'], eid
        from .business_tools import _save_fact_check
        _save_fact_check(ledger, 'cash', account.id, values['evidence_id'], note, ctx=ctx)
        adjustments.append(dict(account_id=account.id, variance_minor=variance, evidence_id=eid))
    payload.update(preview=preview, adjustments=adjustments)
    return _finish(ledger, key, 'daily_cash_snapshot_confirmed', payload,
        dict(as_of=ledger.as_of.isoformat(), adjustments=adjustments), ctx)
