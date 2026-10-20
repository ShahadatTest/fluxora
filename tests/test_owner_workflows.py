"""Owner-only fact review, existing receipt reconciliation and settled cash close."""
import copy
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from cashflow_copilot import actions, web
from cashflow_copilot.attention import attention_items, stale_plan
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import AuthorizationError, CashflowError, IdempotencyConflict
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account, Evidence, Receipt
from cashflow_copilot.owner_workflows import (close_day, confirm_collection_date, daily_preview,
    receipt_candidates, reconcile_existing_receipt, reopen_receipt_review, review_receipt, review_revision)
from cashflow_copilot.owner_workflows import import_observations, observation_candidates, allocate_observation, dismiss_observation
from cashflow_copilot.planning import create_plan_drafts, plan_draft_preview
from cashflow_copilot.store import SQLiteStore
from tests.test_adaptive_priorities import own
from tests.test_web import live_server, _get, _load, _post, _mpost


def ctx(ledger, role='owner', business=None):
    return RequestContext(role, role, business or ledger.business_id)


def save(db, ledger):
    store = SQLiteStore(db)
    store.save_ledger(ledger)
    store.close()


def receipt(ledger, rid='existing-r', amount=10000, day=None, invoice='custom-x', acknowledge_duplicates=False):
    source = ledger.receivables[invoice].evidence_id
    ledger.record_receipt(Receipt(rid, invoice, amount, day or ledger.as_of, evidence_id=source), ctx=ctx(ledger),acknowledge_duplicates=acknowledge_duplicates)
    return ledger.receipts[rid]


def request(ledger, aid='request-x', amount=20000, invoice='custom-x', state='owner_approved'):
    action = actions.create_action(aid, 'request_early_payment', dict(receivable_id=invoice,
        currency=ledger.receivables[invoice].currency, amount_minor=amount, date='2027-01-03'),
        ctx=ctx(ledger), business_id=ledger.business_id)
    ledger.actions[aid] = action
    if state != 'draft':
        actions.approve(action, ctx=ctx(ledger))
    if state in {'sent', 'counterparty_accepted', 'payment_observed'}:
        actions.send(action, ctx=ctx(ledger))
    if state in {'counterparty_accepted', 'payment_observed'}:
        actions.counterparty_respond(action, True, 'customer-x', ctx=ctx(ledger))
    return action


def confirmations(ledger, day, variance=0):
    preview = daily_preview(ledger, day, ctx=ctx(ledger, 'viewer'))
    return {r['account_id']: dict(opening_minor=r['calculated_opening_minor'] + variance,
        evidence_id=next(iter(ledger.evidence))) for r in preview['accounts']}


def match(ledger, rid='existing-r', aid='', ack=False):
    return review_receipt(ledger, rid, aid, 'customer-x', 'Checked imported allocation and source.', ack,
        review_revision(ledger), ctx=ctx(ledger))


def test_confirm_missing_date_keeps_invoice_money_bill_dates_and_authority_distinct(own):
    own.receivables['custom-x'].expected_date = None
    before = copy.deepcopy(vars(own))
    result = confirm_collection_date(own, 'custom-x', date(2027, 1, 11), 'customer-x',
        'Owner checked the customer expectation.', review_revision(own), ctx=ctx(own))
    assert own.receivables['custom-x'].expected_date == date(2027, 1, 11)
    assert own.receivables['custom-x'].evidence_id == 'customer-x'
    assert own.receivables['custom-x'].original_minor == 65000
    assert own.collection_date_sources['custom-x'] == result['confirmation_evidence']
    assert own.evidence[result['confirmation_evidence']].kind == 'owner_confirmed_collection_expectation'
    assert own.outflows == before['outflows'] and own.accounts == before['accounts']
    assert own.receipts == own.consent_map == {} and not own.runtime_evidence_ids
    assert not any(i['code'] == 'confirm_input' and i['target'] == 'custom-x' for i in attention_items(own, ctx=ctx(own)))
    assert 'unknown' in own.evidence[result['confirmation_evidence']].summary


def test_date_confirmation_invalidates_pending_plan_but_exact_retry_survives_restart(own, tmp_path):
    p = plan_draft_preview(own, 'auto-collections', ctx=ctx(own))
    create_plan_drafts(own, 'auto-collections', p['revision'], ctx=ctx(own))
    revision = review_revision(own)
    args = ('custom-x', date(2027, 1, 13), 'customer-x', 'Reconfirmed collection date.', revision)
    first = confirm_collection_date(own, *args, ctx=ctx(own))
    assert stale_plan(own, next(iter(own.actions.values())))
    db = str(tmp_path / 'facts.sqlite')
    save(db, own)
    loaded = _load(db)
    before = copy.deepcopy(vars(loaded))
    assert confirm_collection_date(loaded, *args, ctx=ctx(loaded)) == dict(first, duplicate=True)
    assert vars(loaded) == before
    with pytest.raises(AuthorizationError):
        confirm_collection_date(loaded, *args, ctx=ctx(loaded, 'agent'))
    assert loaded.collection_date_sources == own.collection_date_sources


@pytest.mark.parametrize('invalid', ['past', 'unknown_source', 'blank_reason', 'other_invoice', 'stale', 'viewer', 'other_business'])
def test_invalid_fact_change_has_no_effect(own, invalid):
    args = dict(invoice_id='custom-x', expected_date=date(2027, 1, 13), evidence_id='customer-x',
        reason='Reviewed expectation.', revision=review_revision(own), ctx=ctx(own))
    if invalid == 'past': args['expected_date'] = date(2026, 12, 31)
    if invalid == 'unknown_source': args['evidence_id'] = 'missing'
    if invalid == 'blank_reason': args['reason'] = ' '
    if invalid == 'other_invoice': args['invoice_id'] = 'not-here'
    if invalid == 'stale': args['revision'] = '0' * 64
    if invalid == 'viewer': args['ctx'] = ctx(own, 'viewer')
    if invalid == 'other_business': args['ctx'] = ctx(own, business='wrong')
    before = copy.deepcopy(vars(own))
    with pytest.raises(CashflowError): confirm_collection_date(own, **args)
    assert vars(own) == before


def test_matching_partial_existing_receipt_has_zero_cash_allocation_or_authority_effect(own):
    receipt(own)
    action = request(own)
    before = copy.deepcopy((own.receipts, own.receivables, own.accounts, own.outflows, own.actions))
    info = receipt_candidates(own, 'existing-r', ctx=ctx(own, 'viewer'))
    assert info['candidates'][0]['partial']
    assert 'date differs' in info['candidates'][0]['explanation']
    match(own, aid=action.id)
    assert (own.receipts, own.receivables, own.accounts, own.outflows, own.actions) == before
    assert own.receipt_reviews['existing-r']['status'] == 'matched'
    assert any(i['code'] == 'receipt_review_reconcile' for i in attention_items(own, ctx=ctx(own)))
    assert not own.consent_map


@pytest.mark.parametrize('mismatch', ['unapproved', 'other_invoice', 'currency', 'amount', 'other_business', 'changed_approval'])
def test_receipt_candidates_reject_wrong_or_unauthorized_request(own, mismatch):
    receipt(own)
    action = request(own, state='draft' if mismatch == 'unapproved' else 'owner_approved')
    if mismatch == 'other_invoice': action.terms['receivable_id'] = 'custom-y'
    if mismatch == 'currency': action.terms['currency'] = 'EUR'
    if mismatch == 'amount': action.terms['amount_minor'] = 9999
    if mismatch == 'other_business': action.business_id = 'foreign'
    if mismatch == 'changed_approval': action.version += 1
    assert receipt_candidates(own, 'existing-r', ctx=ctx(own))['candidates'] == []
    before = copy.deepcopy(vars(own))
    with pytest.raises(CashflowError): match(own, aid=action.id)
    assert vars(own) == before


def test_possible_duplicate_is_flagged_requires_ack_and_is_never_deleted(own):
    receipt(own, 'r-one')
    receipt(own, 'r-two', acknowledge_duplicates=True)
    info = receipt_candidates(own, 'r-one', ctx=ctx(own))
    assert info['possible_duplicate_ids'] == ['r-two']
    with pytest.raises(CashflowError, match='duplicate'): match(own, 'r-one')
    match(own, 'r-one', ack=True)
    assert len(own.receipts) == 2 and len(own.receivables['custom-x'].allocations) == 2
    assert own.receipt_reviews['r-one']['duplicate_ids_reviewed'] == ['r-two']


def test_total_partial_matches_cannot_exceed_request_or_link_to_multiple_actions(own):
    request(own, amount=15000)
    receipt(own, 'r-one', amount=10000)
    receipt(own, 'r-two', amount=6000, day=own.as_of + timedelta(days=1))
    match(own, 'r-one', 'request-x')
    assert receipt_candidates(own, 'r-two', ctx=ctx(own))['candidates'] == []
    with pytest.raises(CashflowError): match(own, 'r-two', 'request-x')
    request(own, 'another-request')
    with pytest.raises(IdempotencyConflict): match(own, 'r-one', 'another-request')


def test_invoice_only_reconciliation_no_action_required_clears_queue_and_replays(own, tmp_path):
    receipt(own)
    match(own)
    rev = review_revision(own)
    before = copy.deepcopy((own.accounts, own.receivables, own.receipts))
    args = ('existing-r', 'customer-x', 'Verified invoice allocation and recorded source.', rev)
    first = reconcile_existing_receipt(own, *args, ctx=ctx(own))
    assert own.receipt_reviews['existing-r']['status'] == 'reconciled'
    assert not any(i['target'] == 'existing-r' for i in attention_items(own, ctx=ctx(own)))
    assert (own.accounts, own.receivables, own.receipts) == before
    db = str(tmp_path / 'review.sqlite')
    save(db, own)
    loaded = _load(db)
    assert reconcile_existing_receipt(loaded, *args, ctx=ctx(loaded)) == dict(first, duplicate=True)
    with pytest.raises(AuthorizationError): reconcile_existing_receipt(loaded, *args, ctx=ctx(loaded, 'agent'))


def test_matching_never_manufactures_acceptance_or_advances_approved_action(own):
    action = request(own)
    receipt(own)
    match(own, aid=action.id)
    reconcile_existing_receipt(own, 'existing-r', 'customer-x', 'Receipt verified.', review_revision(own), ctx=ctx(own))
    assert action.state == 'owner_approved' and action.counterparty_response is None
    assert not own.consent_map


def test_reconciliation_of_original_observed_receipt_also_reconciles_its_action(own):
    action = request(own, state='counterparty_accepted')
    receipt(own)
    actions.observe_payment(action, 'existing-r', ctx=ctx(own))
    match(own, aid=action.id)
    reconcile_existing_receipt(own, 'existing-r', 'customer-x', 'Original observed receipt checked.', review_revision(own), ctx=ctx(own))
    assert action.state == 'reconciled' and len(own.receipts) == 1


def test_matching_refuses_to_overwrite_existing_historic_action_link(own):
    action = request(own, state='counterparty_accepted')
    receipt(own)
    actions.observe_payment(action, 'existing-r', ctx=ctx(own))
    request(own, 'wrong-request')
    with pytest.raises(CashflowError, match='already linked'): match(own, aid='wrong-request')
    with pytest.raises(CashflowError, match='already linked'): match(own, aid='')


def test_reconcile_requires_match_and_current_approval(own):
    receipt(own)
    with pytest.raises(CashflowError, match='First review'):
        reconcile_existing_receipt(own, 'existing-r', 'customer-x', 'Checked', review_revision(own), ctx=ctx(own))
    action = request(own)
    match(own, aid=action.id)
    actions.update_terms(action, dict(action.terms, amount_minor=15000), 'owner', ctx=ctx(own))
    with pytest.raises(CashflowError, match='terms changed'):
        reconcile_existing_receipt(own, 'existing-r', 'customer-x', 'Checked', review_revision(own), ctx=ctx(own))


def test_roll_forward_never_absorbs_expected_collection_or_unpaid_bill(own):
    before = copy.deepcopy((own.receivables, own.receipts, own.outflows))
    own.roll_forward(date(2027, 1, 15), ctx=ctx(own))
    assert own.accounts['operating'].opening_minor == 100000  # not forecast net cash
    assert own.actual_cash('USD') == 100000
    assert (own.receivables, own.receipts, own.outflows) == before
    curve = forecast(own, own.as_of, own.horizon_days).currencies['USD']
    assert curve.days[0].outflow_minor == 140000  # overdue unpaid liabilities remain visible
    assert curve.days[0].intraday_low_minor == -40000
    assert own.outflows['staff'].date == date(2027, 1, 5)  # no payroll date change


def test_paid_bill_and_observed_receipt_in_forecast_and_successive_snapshots_count_once(own):
    receipt(own, amount=30000, day=date(2027, 1, 2))
    own.mark_paid('staff', date(2027, 1, 5), 'owner', 'pay', ctx=ctx(own))
    curve = forecast(own, own.as_of, 30).currencies['USD']
    assert curve.days[4].outflow_minor == 70000
    assert own.actual_cash('USD', date(2027, 1, 6)) == 60000
    own.roll_forward(date(2027, 1, 6), ctx=ctx(own))
    assert own.accounts['operating'].opening_minor == 60000
    assert own.actual_cash('USD') == 60000
    curve = forecast(own, own.as_of, 30).currencies['USD']
    assert sum(d.inflow_minor for d in curve.days) == 80000  # only remaining invoices
    assert sum(d.outflow_minor for d in curve.days) == 70000  # no paid payroll counted twice
    own.roll_forward(date(2027, 1, 7), ctx=ctx(own))
    assert own.actual_cash('USD') == own.accounts['operating'].opening_minor == 60000


def test_events_on_new_snapshot_date_remain_that_days_flows(own):
    receipt(own, amount=10000, day=date(2027, 1, 2))
    own.mark_paid('overhead', date(2027, 1, 2), 'owner', 'ops-source', ctx=ctx(own))
    own.roll_forward(date(2027, 1, 2), ctx=ctx(own))
    assert own.accounts['operating'].opening_minor == 100000
    assert own.actual_cash('USD') == 90000
    curve = forecast(own, own.as_of, 30).currencies['USD']
    assert curve.days[0].inflow_minor == 10000 and curve.days[0].outflow_minor == 20000


def test_backdated_new_receipts_and_paid_records_refused_but_original_retries_survive(own):
    original = receipt(own, amount=10000, day=date(2027, 1, 2))
    own.mark_paid('overhead', date(2027, 1, 2), 'owner', 'ops-source', ctx=ctx(own))
    own.roll_forward(date(2027, 1, 3), ctx=ctx(own))
    assert own.record_receipt(copy.deepcopy(original), ctx=ctx(own))['duplicate']
    assert not own.mark_paid('overhead', date(2027, 1, 2), 'owner', 'ops-source', ctx=ctx(own))['changed']
    before = copy.deepcopy(vars(own))
    with pytest.raises(CashflowError, match='predate'): receipt(own, 'new-backdated', day=date(2027, 1, 2))
    with pytest.raises(CashflowError, match='predate'): own.mark_paid('staff', date(2027, 1, 2), 'owner', 'pay', ctx=ctx(own))
    assert vars(own) == before


def test_daily_preview_read_only_and_explicit_variance_has_no_invented_receipt(own):
    own.accounts['eur'] = Account('eur', 'EUR', 7000, 1000)
    receipt(own, amount=10000, day=date(2027, 1, 2))
    day = date(2027, 1, 3)
    before = copy.deepcopy(vars(own))
    preview = daily_preview(own, day, ctx=ctx(own, 'viewer'))
    assert vars(own) == before
    assert preview['accounts'][0]['calculated_opening_minor'] == 110000
    values = confirmations(own, day)
    values['operating']['opening_minor'] = 108000
    values['eur']['opening_minor'] = -500
    close_day(own, day, values, 'Owner checked statement: explicit balance corrections.', preview['revision'], ctx=ctx(own))
    assert own.accounts['operating'].opening_minor == 108000 and own.accounts['eur'].opening_minor == -500
    assert len(own.receipts) == 1 and own.receivables == before['receivables'] and own.outflows == before['outflows']
    assert own.actual_cash('USD') == 108000
    assert any('variance -2000' in e.summary for e in own.evidence.values())
    assert any('variance -7500' in e.summary for e in own.evidence.values())
    assert own.consent_map == {} and not own.runtime_evidence_ids


def test_daily_close_exact_retry_survives_restart_and_later_receipt(own, tmp_path):
    day = date(2027, 1, 3)
    values = confirmations(own, day)
    args = (day, values, 'Cash reviewed per source.', review_revision(own))
    first = close_day(own, *args, ctx=ctx(own))
    receipt(own, day=day)
    db = str(tmp_path / 'close.sqlite')
    save(db, own)
    loaded = _load(db)
    before = copy.deepcopy(vars(loaded))
    assert close_day(loaded, *args, ctx=ctx(loaded)) == dict(first, duplicate=True)
    assert vars(loaded) == before
    with pytest.raises(AuthorizationError): close_day(loaded, *args, ctx=ctx(loaded, 'agent'))
    assert loaded.actual_cash('USD') == 110000


@pytest.mark.parametrize('invalid', ['backwards', 'same_day', 'too_far', 'missing_account', 'float_amount', 'unknown_source', 'blank_note', 'stale', 'agent', 'foreign'])
def test_daily_invalid_confirmation_does_not_partially_change_any_account(own, invalid):
    day = own.as_of + timedelta(days=1)
    values = confirmations(own, day)
    args = dict(new_as_of=day, confirmations=values, note='Review source.', revision=review_revision(own), ctx=ctx(own))
    if invalid == 'backwards': args['new_as_of'] = own.as_of - timedelta(days=1)
    if invalid == 'same_day': args['new_as_of'] = own.as_of
    if invalid == 'too_far': args['new_as_of'] = own.as_of + timedelta(days=367)
    if invalid == 'missing_account': args['confirmations'] = {}
    if invalid == 'float_amount': values['operating']['opening_minor'] = 10.1
    if invalid == 'unknown_source': values['operating']['evidence_id'] = 'not-here'
    if invalid == 'blank_note': args['note'] = ' '
    if invalid == 'stale': args['revision'] = '0' * 64
    if invalid == 'agent': args['ctx'] = ctx(own, 'agent')
    if invalid == 'foreign': args['ctx'] = ctx(own, business='foreign')
    before = copy.deepcopy(vars(own))
    with pytest.raises(CashflowError): close_day(own, **args)
    assert vars(own) == before


def test_http_new_pages_csrf_owner_confirmation_and_no_overrides(live_server, own):
    base, db = live_server
    own.receivables['custom-x'].expected_date = None
    receipt(own)
    save(db, own)
    before = copy.deepcopy(vars(_load(db)))
    for path in ['/facts', '/receipts', '/daily', '/today', '/ledger']:
        code, body = _get(base, path)
        assert code == 200 and 'SYNTHETIC' in body
    assert vars(_load(db)) == before
    revision = review_revision(_load(db))
    fields = dict(revision=revision, invoice_id='custom-x', expected_date='2027-01-12', evidence_id='customer-x', reason='Confirmed with source.')
    assert _post(base, '/facts/confirm', fields)[0] == 403
    fields['csrf'] = web.CSRF_TOKEN
    assert _post(base, '/facts/confirm', fields)[0] == 400  # document evidence is not explicit owner confirmation
    fields['confirmed'] = 'yes'
    assert _post(base, '/facts/confirm', dict(fields, state='paid'))[0] == 400
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: _post(base, '/facts/confirm', fields)[0], range(3))) == [200] * 3
    loaded = _load(db)
    assert len(loaded.owner_operations) == 1
    assert loaded.receivables['custom-x'].expected_date == date(2027, 1, 12)
    assert len(loaded.receipts) == 1


def test_http_existing_receipt_match_then_separate_reconciliation(live_server, own):
    base, db = live_server
    receipt(own)
    action = request(own)
    save(db, own)
    fields = dict(csrf=web.CSRF_TOKEN, confirmed='yes', receipt_id='existing-r', action_id=action.id,
        evidence_id='customer-x', note='Owner checked original receipt.', revision=review_revision(own))
    assert _post(base, '/receipts/match', dict(fields, amount_minor='999999'))[0] == 400
    assert _post(base, '/receipts/match', fields)[0] == 200
    loaded = _load(db)
    assert loaded.receipt_reviews['existing-r']['status'] == 'matched'
    assert len(loaded.receipts) == 1 and loaded.receivables['custom-x'].remaining_minor == 55000
    assert _post(base, '/receipts/match', fields)[0] == 200  # exact replay
    fields.pop('action_id')
    fields.update(revision=review_revision(loaded), note='Allocation/source reconciled.')
    assert _post(base, '/receipts/reconcile', fields)[0] == 200
    assert _load(db).receipt_reviews['existing-r']['status'] == 'reconciled'
    assert _load(db).actions[action.id].state == 'owner_approved'


def test_http_daily_money_precision_stale_review_atomicity_and_retry(live_server, own):
    base, db = live_server
    own.accounts['jpy'] = Account('jpy', 'JPY', 1200, 0)
    save(db, own)
    fields = dict(csrf=web.CSRF_TOKEN, confirmed='yes', revision=review_revision(own), new_as_of='2027-01-02',
        balance_0='1000.00', evidence_0='customer-x', balance_1='1200', evidence_1='customer-y', note='Reviewed separate balances.')
    assert _post(base, '/daily/confirm', dict(fields, balance_1='1200.1'))[0] == 400
    assert _load(db).as_of == own.as_of
    assert _post(base, '/daily/confirm', dict(fields, revision='0' * 64))[0] == 400
    assert _post(base, '/daily/confirm', dict(fields, balance_1='NaN'))[0] == 400
    assert _post(base, '/daily/confirm', fields)[0] == 200
    loaded = _load(db)
    assert loaded.as_of == date(2027, 1, 2) and loaded.accounts['jpy'].opening_minor == 1200
    before = copy.deepcopy(vars(loaded))
    assert _post(base, '/daily/confirm', fields)[0] == 200
    assert vars(_load(db)) == before
    code, export = _get(base, '/audit/export')
    assert code == 200 and 'daily_cash_snapshot_confirmed' in export and 'owner_operations' in export


def test_legacy_database_defaults_for_owner_metadata(own, tmp_path):
    db = str(tmp_path / 'old.sqlite')
    save(db, own)
    store = SQLiteStore(db)
    store.conn.execute("DELETE FROM meta WHERE key IN ('receipt_reviews','owner_operations','collection_date_sources')")
    store.conn.commit()
    loaded = store.load_ledger()
    store.close()
    assert loaded.receipt_reviews == loaded.owner_operations == loaded.collection_date_sources == {}


def test_reopen_unreconciled_match_can_be_rematched_without_cash_or_stale_retry_resurrection(own):
    receipt(own)
    request(own)
    original = ('existing-r', 'request-x', 'customer-x', 'Original match.', False, review_revision(own))
    review_receipt(own, *original, ctx=ctx(own))
    before = copy.deepcopy((own.receipts, own.receivables, own.accounts, own.actions))
    revision = review_revision(own)
    with pytest.raises(AuthorizationError):
        reopen_receipt_review(own, 'existing-r', 'Wrong match choice.', revision, ctx=ctx(own, 'agent'))
    reopen_receipt_review(own, 'existing-r', 'Wrong match choice.', revision, ctx=ctx(own))
    assert not own.receipt_reviews
    assert review_receipt(own, *original, ctx=ctx(own))['duplicate']
    assert not own.receipt_reviews  # old replay cannot resurrect an abandoned match
    match(own, aid='')
    assert own.receipt_reviews['existing-r']['action_id'] == ''
    assert (own.receipts, own.receivables, own.accounts, own.actions) == before
    reconcile_existing_receipt(own, 'existing-r', 'customer-x', 'Checked invoice.', review_revision(own), ctx=ctx(own))
    with pytest.raises(CashflowError):
        reopen_receipt_review(own, 'existing-r', 'Reopen settled review.', review_revision(own), ctx=ctx(own))


def test_model_tool_results_trace_new_date_evidence_and_overdue_or_paid_causes(own):
    from cashflow_copilot.agent import _tool_get_evidence, _forecast_payload
    confirm_collection_date(own, 'custom-x', date(2027, 1, 20), 'customer-x', 'Changed expectation.', review_revision(own), ctx=ctx(own))
    data = _tool_get_evidence(ctx(own), own, {}, 'mock')
    row = next(r for r in data['receivables'] if r['id'] == 'custom-x')
    assert row['expected_date_evidence_id'] == own.collection_date_sources['custom-x']
    own.mark_paid('staff', date(2027, 1, 5), 'owner', 'pay', ctx=ctx(own))
    payload = _forecast_payload(own, forecast(own, own.as_of, 30))
    assert any(r['id'] == 'staff' and r['kind'] == 'paid_outflow' for r in payload['currencies']['USD']['prior_outflows'])
    own.roll_forward(date(2027, 1, 9), ctx=ctx(own))
    payload = _forecast_payload(own, forecast(own, own.as_of, 30))
    causes = payload['currencies']['USD']['breach_causes']
    assert {r['id'] for r in causes} >= {'materials', 'overhead'}
    assert all(r['date'] == '2027-01-09' for r in causes)


@pytest.mark.parametrize('invoice,currency', [('', 'USD'), ('not-an-invoice', 'USD'), ('custom-x', 'EUR')])
def test_csv_receipt_never_guesses_invoice_or_relabels_wrong_currency(live_server, own, invoice, currency):
    base, db = live_server
    save(db, own)
    csv = f'record_type,reference,amount,currency,date,counterparty\nreceipt,bad-row,10.00,{currency},2027-01-02,{invoice}\n'
    status, html = _mpost(base, '/ledger/import/csv', dict(csrf=web.CSRF_TOKEN), {
        'file': ('receipt.csv', csv.encode(), 'text/csv')})
    assert status == 200
    token = re.search('name="token" value="([a-f0-9]+)"', html).group(1)
    fields = dict(csrf=web.CSRF_TOKEN, token=token,
        **{f'col_{i}': field for i, field in enumerate(['record_type', 'reference', 'amount_major', 'currency', 'date', 'counterparty'])})
    status, html = _post(base, '/ledger/import/csv/preview', fields)
    assert status == 200 and 'explicit existing invoice' in html
    token = re.search('name="token" value="([a-f0-9]+)"', html).group(1)
    assert _post(base, '/ledger/import/confirm', dict(csrf=web.CSRF_TOKEN, token=token))[0] == 200
    assert not _load(db).receipts


def test_csv_imported_receipt_can_be_matched_and_reconciled_without_creating_a_second_receipt(live_server, own):
    base, db = live_server
    save(db, own)
    csv = 'record_type,reference,amount,currency,date,counterparty\nreceipt,statement-one,100.00,USD,2027-01-02,custom-x\n'
    status, html = _mpost(base, '/ledger/import/csv', dict(csrf=web.CSRF_TOKEN), {
        'file': ('receipt.csv', csv.encode(), 'text/csv')})
    token = re.search('name="token" value="([a-f0-9]+)"', html).group(1)
    fields = dict(csrf=web.CSRF_TOKEN, token=token,
        **{f'col_{i}': field for i, field in enumerate(['record_type', 'reference', 'amount_major', 'currency', 'date', 'counterparty'])})
    status, html = _post(base, '/ledger/import/csv/preview', fields)
    token = re.search('name="token" value="([a-f0-9]+)"', html).group(1)
    assert _post(base, '/ledger/import/confirm', dict(csrf=web.CSRF_TOKEN, token=token))[0] == 200
    ledger = _load(db)
    evidence = ledger.receipts['statement-one'].evidence_id
    original_amount = ledger.actual_cash('USD', date(2027, 1, 3))
    fields = dict(csrf=web.CSRF_TOKEN, confirmed='yes', receipt_id='statement-one', evidence_id=evidence,
        note='Checked imported statement row.', revision=review_revision(ledger))
    assert _post(base, '/receipts/match', fields)[0] == 200
    fields['revision'] = review_revision(_load(db))
    assert _post(base, '/receipts/reconcile', fields)[0] == 200
    ledger = _load(db)
    assert len(ledger.receipts) == 1 and ledger.receivables['custom-x'].remaining_minor == 55000
    assert ledger.actual_cash('USD', date(2027, 1, 3)) == original_amount


def observation_rows(reference=''):
    return [dict(id='unallocated-001', currency='USD', amount_minor=10000, date='2027-01-02', invoice_reference=reference)]


def test_unallocated_observation_is_not_cash_and_invoice_identity_is_not_guessed(own):
    before = copy.deepcopy((own.accounts, own.receipts, own.receivables))
    revision = review_revision(own)
    import_observations(own, observation_rows(), '0' * 64, revision, ctx=ctx(own))
    assert (own.accounts, own.receipts, own.receivables) == before
    info = observation_candidates(own, 'unallocated-001', ctx=ctx(own, 'viewer'))
    assert [c['invoice_id'] for c in info['candidates']] == ['custom-x', 'custom-y']
    assert not any(c['exact_reference'] for c in info['candidates'])
    assert any(i['code'] == 'observation_unallocated' for i in attention_items(own, ctx=ctx(own)))
    assert import_observations(own, observation_rows(), '0' * 64, revision, ctx=ctx(own))['duplicate']
    allocate_observation(own, 'unallocated-001', 'custom-y', 'Owner verified source reference for client Y.', False,
        info['revision'], ctx=ctx(own))
    assert len(own.receipts) == 1 and own.receipts['unallocated-001'].receivable_id == 'custom-y'
    assert own.receivables['custom-y'].remaining_minor == 35000
    assert own.actual_cash('USD', date(2027, 1, 3)) == 110000
    assert own.receipt_inbox['unallocated-001']['status'] == 'recorded'
    assert not own.actions and not own.consent_map


def test_observation_exact_reference_ranks_first_and_currency_capacity_filter_is_enforced(own):
    import_observations(own, observation_rows('custom-y'), '0' * 64, review_revision(own), ctx=ctx(own))
    info = observation_candidates(own, 'unallocated-001', ctx=ctx(own))
    assert info['candidates'][0]['invoice_id'] == 'custom-y' and info['candidates'][0]['exact_reference']
    own.receivables['custom-y'].currency = 'EUR'
    assert [c['invoice_id'] for c in observation_candidates(own, 'unallocated-001', ctx=ctx(own))['candidates']] == ['custom-x']
    with pytest.raises(CashflowError):
        allocate_observation(own, 'unallocated-001', 'custom-y', 'Checked', False, review_revision(own), ctx=ctx(own))


@pytest.mark.parametrize('failure', ['changed_id_payload', 'duplicate_id_file', 'amount', 'currency', 'stale', 'agent', 'foreign'])
def test_invalid_observation_import_is_atomic_and_business_scoped(own, failure):
    rows = observation_rows()
    if failure == 'changed_id_payload':
        import_observations(own, rows, '0' * 64, review_revision(own), ctx=ctx(own))
        rows[0]['amount_minor'] = 9999
    if failure == 'duplicate_id_file': rows.append(dict(rows[0]))
    if failure == 'amount': rows[0]['amount_minor'] = True
    if failure == 'currency': rows[0]['currency'] = 'EUR'
    before = copy.deepcopy(vars(own))
    context = ctx(own, 'agent') if failure == 'agent' else ctx(own, business='wrong') if failure == 'foreign' else ctx(own)
    with pytest.raises(CashflowError):
        import_observations(own, rows, '0' * 64, 'bad' if failure == 'stale' else review_revision(own), ctx=context)
    assert vars(own) == before


def test_observation_possible_duplicates_need_ack_and_retry_is_restart_safe(own, tmp_path):
    receipt(own, 'existing-r', day=date(2027, 1, 2))
    import_observations(own, observation_rows('custom-y'), '0' * 64, review_revision(own), ctx=ctx(own))
    info = observation_candidates(own, 'unallocated-001', ctx=ctx(own))
    assert info['possible_duplicate_ids'] == ['existing-r']
    with pytest.raises(CashflowError, match='duplicate'):
        allocate_observation(own, 'unallocated-001', 'custom-y', 'Different observed payment checked.', False, info['revision'], ctx=ctx(own))
    args = ('unallocated-001', 'custom-y', 'Different observed payment checked.', True, info['revision'])
    first = allocate_observation(own, *args, ctx=ctx(own))
    own.roll_forward(date(2027, 1, 3), ctx=ctx(own))
    db = str(tmp_path / 'observation.sqlite')
    save(db, own)
    loaded = _load(db)
    before = copy.deepcopy(vars(loaded))
    assert allocate_observation(loaded, *args, ctx=ctx(loaded)) == dict(first, duplicate=True)
    assert vars(loaded) == before and len(loaded.receipts) == 2
    with pytest.raises(AuthorizationError): allocate_observation(loaded, *args, ctx=ctx(loaded, 'agent'))


def test_http_unallocated_csv_preview_confirm_invoice_choice_and_duplicate_post(live_server, own):
    base, db = live_server
    save(db, own)
    csv = 'id,amount,currency,date,invoice_reference\nunallocated-001,100.00,USD,2027-01-02,\n'
    code, html = _mpost(base, '/receipts/import', dict(csrf=web.CSRF_TOKEN), {'file': ('observations.csv', csv.encode(), 'text/csv')})
    assert code == 200 and 'no payment or allocation' in html
    assert not _load(db).receipt_inbox and not _load(db).receipts
    token = re.search("name='token' value='([a-f0-9]+)'", html).group(1)
    fields = dict(csrf=web.CSRF_TOKEN, confirmed='yes', token=token)
    assert _post(base, '/receipts/import/confirm', fields)[0] == 200
    assert not _load(db).receipts and _load(db).receipt_inbox['unallocated-001']['status'] == 'unallocated'
    _, body = _get(base, '/receipts/inbox')
    assert 'custom-x' in body and 'custom-y' in body and 'identity is unverified' in body
    fields = dict(csrf=web.CSRF_TOKEN, confirmed='yes', observation_id='unallocated-001', invoice_id='custom-y',
        note='Owner checked source identity.', revision=review_revision(_load(db)))
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: _post(base, '/receipts/allocate', fields)[0], range(3))) == [200] * 3
    assert len(_load(db).receipts) == 1
    assert _load(db).receivables['custom-y'].remaining_minor == 35000


def test_owner_can_dismiss_duplicate_source_without_recording_cash_or_reimport_reviving_it(own):
    revision = review_revision(own)
    import_observations(own, observation_rows(), '0' * 64, revision, ctx=ctx(own))
    before = copy.deepcopy((own.accounts, own.receipts, own.receivables))
    args = ('unallocated-001', 'Duplicate statement source; payment is already accounted for.', review_revision(own))
    with pytest.raises(AuthorizationError): dismiss_observation(own, *args, ctx=ctx(own, 'agent'))
    dismiss_observation(own, *args, ctx=ctx(own))
    assert dismiss_observation(own, *args, ctx=ctx(own))['duplicate']
    assert import_observations(own, observation_rows(), '0' * 64, revision, ctx=ctx(own))['duplicate']
    assert own.receipt_inbox['unallocated-001']['status'] == 'dismissed'
    assert (own.accounts, own.receipts, own.receivables) == before
    assert not any(i['code'] == 'observation_unallocated' for i in attention_items(own, ctx=ctx(own)))
    with pytest.raises(CashflowError):
        allocate_observation(own, 'unallocated-001', 'custom-x', 'Wrongly try again.', False, review_revision(own), ctx=ctx(own))


def test_pending_observation_cannot_be_recorded_through_an_unrelated_receipt_source(own):
    import_observations(own, observation_rows(), '0' * 64, review_revision(own), ctx=ctx(own))
    with pytest.raises(CashflowError, match='inbox'):
        own.record_receipt(Receipt('unallocated-001', 'custom-x', 10000, date(2027,1,2), evidence_id='customer-x'), ctx=ctx(own))
    assert not own.receipts
