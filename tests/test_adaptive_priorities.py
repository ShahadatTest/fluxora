"""Own-data planning and the read-only priorities queue (no live provider)."""
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from cashflow_copilot import actions, web
from cashflow_copilot.adaptive import generate_proposals, input_problems
from cashflow_copilot.agent import _tool_compare_plans, _tool_missing_facts
from cashflow_copilot.attention import attention_items, stale_plan
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import AuthorizationError, CashflowError
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Account, Evidence, Outflow, Receivable, Receipt
from cashflow_copilot.planning import create_plan_drafts, plan_draft_preview
from cashflow_copilot.store import SQLiteStore
from tests.test_web import live_server, _get, _load, _mpost, _post


def context(ledger, role='owner', business=None):
    return RequestContext(role, role, business or ledger.business_id)


@pytest.fixture
def own():
    ledger = Ledger()
    ledger.business_id, ledger.business_name = 'own-synthetic', 'Synthetic Workshop'
    ledger.as_of = date(2027, 1, 1)
    ledger.accounts['operating'] = Account('operating', 'USD', 100000, 20000)
    for eid in ['pay', 'supplier-source', 'ops-source', 'customer-x', 'customer-y']:
        ledger.evidence[eid] = Evidence(eid, 'synthetic', f'Synthetic source: {eid}')
    ledger.receivables['custom-x'] = Receivable('custom-x', 'USD', 65000, date(2027, 1, 12), 'customer-x')
    ledger.receivables['custom-y'] = Receivable('custom-y', 'USD', 45000, date(2027, 1, 14), 'customer-y')
    ledger.outflows['staff'] = Outflow('staff', 'USD', 70000, date(2027, 1, 5), True, 'pay')
    ledger.outflows['materials'] = Outflow('materials', 'USD', 50000, date(2027, 1, 7), False, 'supplier-source', True)
    ledger.outflows['overhead'] = Outflow('overhead', 'USD', 20000, date(2027, 1, 8), False, 'ops-source', False, 10000)
    return ledger


def proposal(own, name='auto-collections'):
    return next(p for p in generate_proposals(own, ctx=context(own)) if p.id == name)


def items(own):
    return attention_items(own, ctx=context(own, 'viewer'))


def save(db, ledger):
    store = SQLiteStore(db)
    try:
        store.save_ledger(ledger)
    finally:
        store.close()


def test_own_identifiers_dates_exact_required_advance_and_no_mutation(own):
    before = copy.deepcopy(vars(own))
    p = proposal(own)
    assert p.financially_sufficient
    assert p.drafts == [dict(kind='request_early_payment', receivable_id='custom-x',
        amount_minor=60000, currency='USD', date='2027-01-06', amount='USD 600.00')]
    assert p.forecast.currencies['USD'].min_balance_minor == 20000
    assert vars(own) == before
    assert 'counterparty' not in json.dumps(own.runtime_evidence_ids, default=list)


def test_deferral_proposals_are_numerical_scenarios_not_consent(own):
    before = copy.deepcopy(vars(own))
    p = proposal(own, 'auto-deferrals')
    assert p.financially_sufficient
    assert {s.get('outflow_id') for s in p.drafts} == {'materials', 'overhead'}
    assert all(s['date'] == '2027-01-13' for s in p.drafts)
    assert next(s for s in p.drafts if s['outflow_id'] == 'overhead')['amount_minor'] == 10000
    assert any('counterparty acceptance' in s for s in p.pending_permissions)
    assert vars(own) == before


def test_prior_acceptance_is_not_reused_for_a_different_proposed_date(own):
    own.record_counterparty_acceptance('materials', date(2027, 1, 15), 'old-request', actor='owner')
    before = copy.deepcopy(vars(own))
    p = proposal(own, 'auto-deferrals')
    assert p.financially_sufficient
    assert next(s for s in p.drafts if s.get('outflow_id') == 'materials')['date'] == '2027-01-13'
    assert any('this exact proposed date' in text for text in p.pending_permissions)
    assert vars(own) == before


def test_combined_covers_gap_when_neither_single_strategy_can(own):
    own.constraints.max_advance_minor = 50000
    own.outflows['materials'].amount_minor = 40000
    own.outflows['overhead'].amount_minor = 50000
    own.outflows['overhead'].maximum_deferrable_minor = 10000
    # Defer suppliers only: cash is still short for the nondeferrable ops share.
    assert not proposal(own, 'auto-collections').financially_sufficient
    assert not proposal(own, 'auto-deferrals').financially_sufficient
    p = proposal(own, 'auto-combined')
    assert p.financially_sufficient
    assert {s['kind'] for s in p.drafts} == {'request_supplier_extension', 'defer_ops_expense', 'request_early_payment'}


def test_money_conservation_after_partial_receipt(own):
    own.record_receipt(Receipt('already-paid', 'custom-x', 20000, date(2027, 1, 2), evidence_id='customer-x'))
    p = proposal(own)
    total = sum(d.inflow_minor for d in p.forecast.currencies['USD'].days)
    assert total == 110000  # observed 20k plus remaining 90k, no double counting
    assert all(s['amount_minor'] <= own.receivables[s['receivable_id']].remaining_minor for s in p.drafts)


def test_cap_is_aggregate_across_invoices_not_per_invoice(own):
    own.receivables['custom-x'].original_minor = 35000
    own.constraints.max_advance_minor = 50000
    p = proposal(own)
    assert sum(s['amount_minor'] for s in p.drafts) == 50000
    assert len(p.drafts) == 2
    assert not p.financially_sufficient
    assert p.forecast.currencies['USD'].worst_shortfall_minor == 10000


@pytest.mark.parametrize('cap', [0, 10000, 40000, 59999])
def test_insufficient_cap_never_reports_sufficient(own, cap):
    own.constraints.max_advance_minor = cap
    p = proposal(own)
    assert not p.financially_sufficient
    assert sum(s['amount_minor'] for s in p.drafts) <= cap


def test_zero_actions_when_recorded_schedule_already_covers_floors(own):
    own.accounts['operating'].opening_minor = 300000
    for p in generate_proposals(own, ctx=context(own)):
        assert p.financially_sufficient and p.drafts == []


def test_opening_breach_cannot_be_fixed_by_same_day_invented_inflow(own):
    own.accounts['operating'].opening_minor = 0
    p = proposal(own)
    assert not p.financially_sufficient and not p.drafts
    assert p.forecast.currencies['USD'].first_breach_date == own.as_of


def test_collection_due_on_breach_day_requested_previous_day(own):
    own.receivables['custom-x'].expected_date = date(2027, 1, 7)
    p = proposal(own)
    assert p.financially_sufficient
    assert p.drafts[0]['date'] == '2027-01-06'


def test_currencies_separate_and_fx_does_not_cover_gap(own):
    own.accounts['euro'] = Account('euro', 'EUR', 20000, 5000)
    own.receivables['eu-client'] = Receivable('eu-client', 'EUR', 20000, date(2027, 1, 15), 'customer-y')
    own.outflows['eu-protected'] = Outflow('eu-protected', 'EUR', 25000, date(2027, 1, 10), True, 'pay')
    p = proposal(own)
    assert p.financially_sufficient
    assert {s['currency'] for s in p.drafts} == {'USD', 'EUR'}
    assert next(s for s in p.drafts if s['currency'] == 'EUR')['amount_minor'] == 10000
    del own.receivables['eu-client']
    assert not proposal(own).financially_sufficient
    assert proposal(own).forecast.currencies['EUR'].worst_shortfall_minor == 10000


@pytest.mark.parametrize('change', ['missing_date', 'past_date', 'past_bill', 'missing_source', 'missing_account', 'negative_account'])
def test_missing_facts_block_sufficient_claim_and_drafts(own, change):
    if change == 'missing_date':
        own.receivables['custom-x'].expected_date = None
    elif change == 'past_date':
        own.receivables['custom-x'].expected_date = own.as_of - timedelta(days=1)
    elif change == 'past_bill':
        own.outflows['materials'].date = own.as_of - timedelta(days=1)
    elif change == 'missing_source':
        del own.evidence['customer-x']
    elif change == 'missing_account':
        own.receivables['custom-x'].currency = 'JPY'
    else:
        own.accounts['another'] = Account('another', 'USD', -1, 0)
    assert input_problems(own)
    for p in generate_proposals(own, ctx=context(own)):
        assert p.blockers and not p.financially_sufficient and not p.drafts
    with pytest.raises(CashflowError, match='Confirm the missing'):
        plan_draft_preview(own, 'auto-collections', ctx=context(own))


def test_protection_owner_flags_and_deferral_caps(own):
    own.constraints.allow_supplier_date_change = False
    own.constraints.allow_ops_deferral = False
    assert not proposal(own, 'auto-deferrals').drafts
    own.constraints.allow_supplier_date_change = True
    own.constraints.allow_ops_deferral = True
    own.outflows['materials'].protected = True
    own.outflows['overhead'].maximum_deferrable_minor = 0
    assert not proposal(own, 'auto-deferrals').drafts


def test_deferrals_never_hide_gap_outside_window_or_beyond_14_days(own):
    for rec in own.receivables.values():
        rec.expected_date = date(2027, 1, 29)
    assert not proposal(own, 'auto-deferrals').drafts
    own.receivables['custom-x'].expected_date = date(2027, 3, 1)
    own.receivables['custom-y'].expected_date = date(2027, 3, 2)
    assert not proposal(own, 'auto-deferrals').drafts


def test_previously_split_or_amended_bills_require_manual_review(own):
    own.outflows['materials'].amendments.append({'kind': 'old-change'})
    own.outflows['overhead'].recorded_splits = [(date(2027, 1, 8), 10000), (date(2027, 1, 9), 10000)]
    assert not proposal(own, 'auto-deferrals').drafts


def test_bound_work_and_require_business_context_before_search(own):
    own.horizon_days = 121
    with pytest.raises(AuthorizationError):
        generate_proposals(own, ctx=context(own, business='other'))
    with pytest.raises(CashflowError, match='1–120'):
        generate_proposals(own, ctx=context(own))


def test_generic_plan_creates_only_drafts_replay_survives_changed_inputs_and_restart(own, tmp_path):
    before = copy.deepcopy(vars(own))
    preview = plan_draft_preview(own, 'auto-deferrals', ctx=context(own, 'viewer'))
    result = create_plan_drafts(own, 'auto-deferrals', preview['revision'], ctx=context(own, 'agent'))
    assert len(result['action_ids']) == 2
    assert all(a.state == 'draft' and a.actor == 'agent' and a.business_id == own.business_id for a in own.actions.values())
    for key in before:
        if key not in {'actions', 'audit'}:
            assert vars(own)[key] == before[key]
    own.constraints.max_advance_minor = 10
    db = str(tmp_path / 'restart.sqlite')
    save(db, own)
    loaded = _load(db)
    assert create_plan_drafts(loaded, 'auto-deferrals', preview['revision'], ctx=context(loaded)) == dict(action_ids=result['action_ids'], duplicate=True)
    with pytest.raises(AuthorizationError):
        create_plan_drafts(loaded, 'auto-deferrals', preview['revision'], ctx=context(loaded, business='other'))
    assert all(stale_plan(loaded, a) for a in loaded.actions.values())


def test_overlap_and_stale_review_rejected_without_partial_creation(own):
    first = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    own.constraints.max_advance_minor = 20000
    with pytest.raises(CashflowError, match='ledger changed'):
        create_plan_drafts(own, 'auto-collections', first['revision'], ctx=context(own))
    assert not own.actions
    own.constraints.max_advance_minor = None
    create_plan_drafts(own, 'auto-collections', first['revision'], ctx=context(own))
    with pytest.raises(CashflowError, match='existing action'):
        plan_draft_preview(own, 'auto-collections', ctx=context(own))


def test_priorities_snapshot_dates_sources_urgency_and_no_mutation(own):
    before = copy.deepcopy(vars(own))
    queue = items(own)
    assert queue[0]['code'] == 'cash_breach'
    assert queue[0]['date'] == '2027-01-07'
    assert queue[0]['amount_minor'] == 60000
    assert 'pay' in queue[0]['evidence_ids']
    assert any(i['code'] == 'protected_due' and i['target'] == 'staff' for i in queue)
    assert vars(own) == before
    with pytest.raises(AuthorizationError):
        attention_items(own, ctx=context(own, business='wrong'))


def test_priorities_unknown_date_and_unlinked_receipt(own):
    own.receivables['custom-x'].expected_date = None
    own.record_receipt(Receipt('loose-receipt', 'custom-y', 10000, own.as_of, evidence_id='customer-y'))
    assert any(i['code'] == 'confirm_input' and 'unknown' in i['why'] for i in items(own))
    assert any(i['code'] == 'unlinked_receipt' and i['target'] == 'loose-receipt' for i in items(own))


def test_priorities_follow_full_action_lifecycle_and_clear_reconciliation(own):
    action = actions.create_action('life', 'request_early_payment',
        {'receivable_id': 'custom-x', 'amount_minor': 10000, 'date': '2027-01-06'}, business_id=own.business_id)
    own.actions[action.id] = action
    expected = ['approval_needed', 'approved_pending', 'response_pending', 'payment_pending', 'reconcile_needed']
    states = [lambda: None,
        lambda: actions.approve(action), lambda: actions.send(action),
        lambda: actions.counterparty_respond(action, True, 'customer-x'),
        lambda: (own.record_receipt(Receipt('life-receipt', 'custom-x', 10000, own.as_of, evidence_id='customer-x'), authorizing_action=action), actions.observe_payment(action, 'life-receipt'))]
    for step, code in zip(states, expected):
        step()
        queue = items(own)
        assert any(i['code'] == code and i['target'] == 'life' for i in queue)
        assert not any(i['code'] == 'unlinked_receipt' for i in queue)
    actions.reconcile(action)
    assert not any(i['target'] in {'life', 'life-receipt'} for i in items(own))


def test_priorities_show_rejection_and_staleness(own):
    preview = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    result = create_plan_drafts(own, 'auto-collections', preview['revision'], ctx=context(own))
    action = own.actions[result['action_ids'][0]]
    assert not stale_plan(own, action)
    own.constraints.max_advance_minor = 100
    assert any(i['code'] == 'stale_plan' for i in items(own))
    actions.approve(action)
    actions.send(action)
    actions.counterparty_respond(action, False, 'customer-x')
    assert any(i['code'] == 'request_rejected' for i in items(own))


def test_agent_compare_uses_current_data_and_never_grants_permission(own):
    answer = _tool_compare_plans(context(own, 'agent'), own, {}, 'test mock')
    assert any(p['financially_sufficient'] for p in answer['plans'])
    assert all(p['feasible'] is False for p in answer['plans'])
    assert 'custom-x' in json.dumps(answer)
    assert 'inv-client-001' not in json.dumps(answer)
    missing = _tool_missing_facts(context(own), own, {}, 'test mock')
    assert 'materials' in json.dumps(missing) and '2026-11-06' not in json.dumps(missing)


def test_http_own_dataset_pages_concurrent_submit_csrf_and_no_override(live_server, own):
    base, db = live_server
    save(db, own)
    for path in ['/', '/plans', '/today', '/ledger', '/actions', '/stress', '/plans/examples']:
        status, body = _get(base, path)
        assert status == 200
        assert 'SYNTHETIC' in body
    _, body = _get(base, '/plans/auto-collections/draft')
    token = re.search("name='revision' value='([a-f0-9]+)'", body).group(1)
    path = '/plans/auto-collections/draft'
    assert _post(base, path, {'revision': token})[0] == 403
    fields = dict(csrf=web.CSRF_TOKEN, revision=token)
    assert _post(base, path, dict(fields, amount_minor=1))[0] == 400
    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(lambda _: _post(base, path, fields)[0], range(3)))
    assert statuses == [200, 200, 200]
    ledger = _load(db)
    assert len(ledger.actions) == 1 and not ledger.receipts and not ledger.consent_map
    _, body = _get(base, '/today')
    assert 'Draft awaiting owner review' in body


def test_http_constraints_save_persist_and_mark_existing_plan_stale(live_server, own):
    base, db = live_server
    preview = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    create_plan_drafts(own, 'auto-collections', preview['revision'], ctx=context(own))
    save(db, own)
    path = '/plans/constraints'
    fields = dict(csrf=web.CSRF_TOKEN, advance_cap_USD='100.00')
    assert _post(base, path, dict(fields, advance_cap_USD='1.234'))[0] == 400
    assert _post(base, path, {'advance_cap_USD': '0'})[0] == 403
    assert _post(base, path, dict(fields, role='agent'))[0] == 400
    assert _post(base, path, fields)[0] == 200
    current = _load(db)
    assert current.constraints.max_advance_minor == 10000
    assert not current.constraints.allow_supplier_date_change
    assert not current.constraints.allow_ops_deferral
    _, body = _get(base, '/today')
    assert 'Ledger changed since this plan was reviewed' in body


def test_http_generic_ops_approval_retains_source_and_changes_only_approved_bill(live_server, own):
    base, db = live_server
    preview = plan_draft_preview(own, 'auto-deferrals', ctx=context(own))
    create_plan_drafts(own, 'auto-deferrals', preview['revision'], ctx=context(own))
    save(db, own)
    ops = next(a for a in own.actions.values() if a.kind == 'defer_ops_expense')
    assert _post(base, f'/actions/{ops.id}/approve', {'csrf': web.CSRF_TOKEN})[0] == 200
    ledger = _load(db)
    assert ledger.outflows['overhead'].recorded_splits == [(date(2027, 1, 8), 10000), (date(2027, 1, 13), 10000)]
    assert ledger.outflows['overhead'].evidence_id == 'ops-source'
    assert ledger.outflows['materials'].date == date(2027, 1, 7)
    assert not ledger.consent_map and not ledger.receipts
    assert ledger.outflows['overhead'].amendments[-1]['evidence_id'].startswith('ev-owner-approval-')
    assert not any(i['target'] == ops.id and i['code'] == 'approved_pending' for i in items(ledger))


def test_currency_limits_are_independent_and_persist_without_schema_change(own, tmp_path):
    own.accounts['jpy'] = Account('jpy', 'JPY', 0, 0)
    own.receivables['jpy-invoice'] = Receivable('jpy-invoice', 'JPY', 3000, date(2027, 1, 15), 'customer-y')
    own.outflows['jpy-bill'] = Outflow('jpy-bill', 'JPY', 2000, date(2027, 1, 10), True, 'pay')
    own.constraints.advance_caps_minor = {'USD': 60000, 'JPY': 1000}
    p = proposal(own)
    assert not p.financially_sufficient
    assert sum(s['amount_minor'] for s in p.drafts if s['currency'] == 'USD') == 60000
    assert sum(s['amount_minor'] for s in p.drafts if s['currency'] == 'JPY') == 1000
    db = str(tmp_path / 'limits.sqlite')
    save(db, own)
    assert _load(db).constraints.advance_caps_minor == {'USD': 60000, 'JPY': 1000}


@pytest.mark.parametrize('changed', ['paid_invoice', 'lower_cap', 'wrong_currency', 'bad_date', 'protected_bill', 'consent_bypass'])
def test_owner_approval_rechecks_current_terms_without_financial_change(live_server, own, changed):
    base, db = live_server
    pid = 'auto-deferrals' if changed in {'protected_bill', 'consent_bypass'} else 'auto-collections'
    preview = plan_draft_preview(own, pid, ctx=context(own))
    create_plan_drafts(own, pid, preview['revision'], ctx=context(own))
    action = next(a for a in own.actions.values() if a.kind != 'defer_ops_expense')
    if changed == 'paid_invoice':
        own.receivables['custom-x'].original_minor = 100
    elif changed == 'lower_cap':
        own.constraints.max_advance_minor = 1
    elif changed == 'wrong_currency':
        action.terms['currency'] = 'EUR'
    elif changed == 'bad_date':
        action.terms['date'] = '2020-01-01'
    elif changed == 'protected_bill':
        own.outflows['materials'].protected = True
    else:
        action.kind = 'defer_ops_expense'
        own.outflows['materials'].maximum_deferrable_minor = 60000
    save(db, own)
    before = copy.deepcopy(vars(own))
    status, _ = _post(base, f'/actions/{action.id}/approve', dict(csrf=web.CSRF_TOKEN))
    assert status == 400
    assert vars(_load(db)) == before


def test_http_jpy_input_is_not_scaled_like_usd_and_bad_precision_rejected(live_server, own):
    base, db = live_server
    own.accounts['jpy'] = Account('jpy', 'JPY', 1000, 0)
    save(db, own)
    fields = dict(csrf=web.CSRF_TOKEN, advance_cap_USD='100.00', advance_cap_JPY='500')
    assert _post(base, '/plans/constraints', fields)[0] == 200
    assert _load(db).constraints.advance_caps_minor == {'USD': 10000, 'JPY': 500}
    assert _post(base, '/plans/constraints', dict(fields, advance_cap_JPY='500.1'))[0] == 400


def test_http_full_fixture_import_then_plan_and_today(live_server, own):
    base, db = live_server
    payload = {'fixture_version': '1.0', 'synthetic': True,
        'business': {'id': own.business_id, 'name': own.business_name, 'base_currency': own.base_currency,
            'as_of': own.as_of.isoformat(), 'horizon_days': 30, 'timezone': 'UTC'},
        'accounts': [asdict(a) for a in own.accounts.values()],
        'evidence': [asdict(e) for e in own.evidence.values()], 'events': [], 'display_fx': [],
        'receivables': [dict(id=r.id, currency=r.currency, original_minor=r.original_minor,
            expected_date=r.expected_date.isoformat(), evidence_id=r.evidence_id) for r in own.receivables.values()],
        'outflows': [dict(id=o.id, currency=o.currency, amount_minor=o.amount_minor, date=o.date.isoformat(),
            protected=o.protected, evidence_id=o.evidence_id,
            counterparty_consent_required_for_change=o.counterparty_consent_required_for_change,
            maximum_deferrable_minor=o.maximum_deferrable_minor) for o in own.outflows.values()]}
    fields = dict(csrf=web.CSRF_TOKEN)
    status, body = _mpost(base, '/ledger/import/fixture', fields, {
        'file': ('own.json', json.dumps(payload).encode(), 'application/json')})
    assert status == 200, body
    token = re.search('name="token" value="([a-f0-9]+)"', body).group(1)
    assert _post(base, '/ledger/import/confirm', dict(fields, token=token))[0] == 200
    assert _load(db).business_id == 'own-synthetic'
    _, body = _get(base, '/plans')
    assert 'custom-x' in body and 'USD 600.00' in body
    assert '2026-10-22' not in body
    _, body = _get(base, '/today')
    assert '2027-01-07' in body


def test_discard_draft_allows_a_new_review_but_old_retry_never_revives_it(own):
    preview = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    original = create_plan_drafts(own, 'auto-collections', preview['revision'], ctx=context(own))
    action = own.actions[original['action_ids'][0]]
    before = copy.deepcopy({k: v for k, v in vars(own).items() if k not in {'actions', 'audit'}})
    with pytest.raises(AuthorizationError):
        actions.cancel_draft(action, ctx=context(own, 'agent'))
    actions.cancel_draft(action, ctx=context(own))
    assert action.state == 'cancelled'
    retry = create_plan_drafts(own, 'auto-collections', preview['revision'], ctx=context(own))
    assert retry['duplicate'] and retry['action_ids'] == original['action_ids']
    assert action.state == 'cancelled'
    new = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    assert new['revision'] != preview['revision']
    result = create_plan_drafts(own, 'auto-collections', new['revision'], ctx=context(own))
    assert result['action_ids'] != original['action_ids']
    assert len(own.actions) == 2
    assert {k: v for k, v in vars(own).items() if k not in {'actions', 'audit'}} == before
    assert not any(i['target'] == action.id for i in items(own))


def test_http_discard_only_unapproved_draft_and_requires_csrf(live_server, own):
    base, db = live_server
    preview = plan_draft_preview(own, 'auto-collections', ctx=context(own))
    result = create_plan_drafts(own, 'auto-collections', preview['revision'], ctx=context(own))
    aid = result['action_ids'][0]
    save(db, own)
    assert _post(base, f'/actions/{aid}/cancel', {})[0] == 403
    assert _post(base, f'/actions/{aid}/cancel', dict(csrf=web.CSRF_TOKEN))[0] == 200
    assert _load(db).actions[aid].state == 'cancelled'
    assert _post(base, f'/actions/{aid}/update-terms', dict(csrf=web.CSRF_TOKEN, amount_major='1'))[0] == 400
    assert _post(base, f'/actions/{aid}/approve', dict(csrf=web.CSRF_TOKEN))[0] == 400


def test_http_generic_supplier_acceptance_retains_original_and_records_no_cash(live_server, own):
    base, db = live_server
    preview = plan_draft_preview(own, 'auto-deferrals', ctx=context(own))
    create_plan_drafts(own, 'auto-deferrals', preview['revision'], ctx=context(own))
    aid = next(a.id for a in own.actions.values() if a.kind == 'request_supplier_extension')
    save(db, own)
    fields = dict(csrf=web.CSRF_TOKEN)
    for step in ['approve', 'send']:
        assert _post(base, f'/actions/{aid}/{step}', fields)[0] == 200
    assert _post(base, f'/actions/{aid}/cancel', fields)[0] == 400
    assert _post(base, f'/actions/{aid}/respond', dict(fields, accepted='true'))[0] == 200
    ledger = _load(db)
    bill = ledger.outflows['materials']
    assert bill.date == date(2027, 1, 13) and bill.original_date == date(2027, 1, 7)
    assert ledger.consent_map['materials'] in ledger.runtime_evidence_ids
    assert bill.amendments[-1]['evidence_id'] == ledger.consent_map['materials']
    assert not ledger.receipts and ledger.actual_cash('USD') == 100000
