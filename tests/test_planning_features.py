"""Deterministic feature regressions and real local HTTP, no live model."""
import copy
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from cashflow_copilot import actions
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import AuthorizationError, CashflowError, IdempotencyConflict
from cashflow_copilot.models import Evidence, Outflow, Receipt, Receivable
from cashflow_copilot.planning import create_plan_drafts, plan_draft_preview, stress_test
from cashflow_copilot.store import SQLiteStore
from tests.test_web import live_server, _get, _post, _load
from cashflow_copilot import web


def ctx(ledger, role='owner', business=None):
    return RequestContext(actor=role, role=role,
                          business_id=business or ledger.business_id)


def financial_state(ledger):
    return copy.deepcopy({k: v for k, v in vars(ledger).items() if k not in {'actions', 'audit'}})


def revision(base, plan='plan-a'):
    status, html = _get(base, f'/plans/{plan}/draft')
    assert status == 200
    return re.search("name='revision' value='([a-f0-9]{64})'", html).group(1)


def test_stress_known_delay_and_zero_days_do_not_mutate(ledger):
    before = copy.deepcopy(vars(ledger))
    zero = stress_test(ledger, 'inv-client-001', 0, ctx=ctx(ledger, 'viewer'))
    assert zero.baseline == zero.scenario
    result = stress_test(ledger, 'inv-client-001', 7, ctx=ctx(ledger))
    assert result.recorded_date == date(2026, 10, 20)
    assert result.assumed_date == date(2026, 10, 27)
    assert result.baseline.currencies['USD'].min_balance_minor == 600000
    assert result.scenario.currencies['USD'].min_balance_minor == -50000
    assert result.scenario.currencies['USD'].first_breach_date == date(2026, 10, 25)
    assert vars(ledger) == before


@pytest.mark.parametrize('days', [-1, 31, True, 1.2, '7'])
def test_stress_invalid_days_rejected(ledger, days):
    with pytest.raises(CashflowError):
        stress_test(ledger, 'inv-client-001', days, ctx=ctx(ledger))


def test_stress_unknown_and_missing_date_never_invented(ledger):
    with pytest.raises(CashflowError, match='Choose an invoice'):
        stress_test(ledger, 'missing', 7, ctx=ctx(ledger))
    ledger.receivables['inv-client-001'].expected_date = None
    with pytest.raises(CashflowError, match='no expected collection date'):
        stress_test(ledger, 'inv-client-001', 7, ctx=ctx(ledger))
    assert ledger.receivables['inv-client-001'].expected_date is None


def test_stress_outside_window_and_paid_allocation(delayed_ledger):
    ledger = delayed_ledger
    ledger.record_receipt(Receipt('part', 'inv-client-001', 100000,
                                 date(2026, 10, 22), evidence_id='ev-client-invoice'))
    before = copy.deepcopy(vars(ledger))
    result = stress_test(ledger, 'inv-client-001', 30, ctx=ctx(ledger))
    assert result.remaining_minor == 700000
    # The actual partial receipt stays in-window. Only remaining collection moves.
    assert sum(d.inflow_minor for d in result.scenario.currencies['USD'].days) == 100000
    assert vars(ledger) == before


def test_stress_currencies_and_protected_dates_are_independent(ledger):
    ledger.evidence['eur-evidence'] = Evidence('eur-evidence', 'synthetic', 'EUR scenario source')
    ledger.receivables['eur-invoice'] = Receivable('eur-invoice', 'EUR', 50000,
        date(2026, 10, 20), 'eur-evidence')
    ledger.outflows['eur-payroll'] = Outflow('eur-payroll', 'EUR', 120000,
        date(2026, 10, 25), True, 'eur-evidence')
    result = stress_test(ledger, 'eur-invoice', 14, ctx=ctx(ledger))
    assert result.baseline.currencies['USD'] == result.scenario.currencies['USD']
    assert result.baseline.currencies['EUR'].min_balance_minor == 30000
    assert result.scenario.currencies['EUR'].min_balance_minor == -20000
    assert ledger.outflows['eur-payroll'].date == date(2026, 10, 25)
    assert ledger.outflows['payroll-001'].date == date(2026, 10, 23)


def test_stress_fully_paid_invoice_and_other_business_refused(ledger):
    with pytest.raises(AuthorizationError):
        stress_test(ledger, 'inv-client-001', 7, ctx=ctx(ledger, business='other'))
    ledger.record_receipt(Receipt('full', 'inv-client-001', 800000,
                                 date(2026, 10, 20), evidence_id='ev-client-invoice'))
    with pytest.raises(CashflowError, match='no remaining balance'):
        stress_test(ledger, 'inv-client-001', 7, ctx=ctx(ledger))


def test_plan_a_creates_one_advance_not_an_extra_residual_request(ledger):
    before = financial_state(ledger)
    preview = plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))
    assert len(preview['drafts']) == 1
    assert preview['drafts'][0]['amount_minor'] == 500000
    result = create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=ctx(ledger))
    action = ledger.actions[result['action_ids'][0]]
    assert action.state == 'draft'
    assert action.approved_version is None
    assert action.counterparty_response is None
    assert action.terms['receivable_id'] == 'inv-client-001'
    assert action.terms['currency'] == 'USD'
    assert action.terms['source_plan_id'] == 'plan-a'
    assert action.business_id == ledger.business_id
    assert financial_state(ledger) == before


def test_plan_b_missing_acceptance_can_only_prepare_requests(delayed_ledger):
    ledger = delayed_ledger
    before = financial_state(ledger)
    preview = plan_draft_preview(ledger, 'plan-b', ctx=ctx(ledger))
    assert not preview['feasible']
    result = create_plan_drafts(ledger, 'plan-b', preview['revision'], ctx=ctx(ledger))
    assert len(result['action_ids']) == 2
    assert {a.kind for a in ledger.actions.values()} == {'request_supplier_extension', 'defer_ops_expense'}
    assert all(a.state == 'draft' and a.approved_version is None for a in ledger.actions.values())
    assert ledger.consent_map == {}
    assert ledger.outflows['supplier-001'].date == date(2026, 10, 25)
    assert ledger.outflows['ops-001'].recorded_splits is None
    assert financial_state(ledger) == before


def test_draft_replay_survives_restart_and_does_not_reset_approval(ledger, tmp_path):
    preview = plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))
    first = create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=ctx(ledger))
    actions.approve(ledger.actions[first['action_ids'][0]], ctx=ctx(ledger))
    ledger.apply_event('evt-delay-001')  # financial inputs can change after creation
    store = SQLiteStore(tmp_path / 'restart.sqlite')
    store.save_ledger(ledger)
    store.close()
    store = SQLiteStore(tmp_path / 'restart.sqlite')
    loaded = store.load_ledger()
    store.close()
    again = create_plan_drafts(loaded, 'plan-a', preview['revision'], ctx=ctx(loaded))
    assert again['duplicate']
    assert again['action_ids'] == first['action_ids']
    assert len(loaded.actions) == 1
    assert loaded.actions[first['action_ids'][0]].state == 'owner_approved'


def test_replay_checks_role_and_business_first(ledger):
    preview = plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))
    create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=ctx(ledger))
    for caller in (ctx(ledger, 'viewer'), ctx(ledger, business='other')):
        with pytest.raises(AuthorizationError):
            create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=caller)
    assert len(ledger.actions) == 1


def test_agent_context_still_creates_only_drafts(ledger):
    caller = ctx(ledger, 'agent')
    preview = plan_draft_preview(ledger, 'plan-a', ctx=caller)
    create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=caller)
    assert all(a.state == 'draft' for a in ledger.actions.values())
    with pytest.raises(AuthorizationError):
        actions.approve(next(iter(ledger.actions.values())), ctx=caller)


@pytest.mark.parametrize('change', ['delay', 'floor', 'receipt', 'constraints'])
def test_new_batch_requires_fresh_review(ledger, change):
    preview = plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))
    if change == 'delay':
        ledger.apply_event('evt-delay-001')
    elif change == 'floor':
        ledger.accounts['usd-main'].floor_minor += 1
    elif change == 'receipt':
        ledger.record_receipt(Receipt('new', 'inv-client-001', 100,
            date(2026, 10, 22), evidence_id='ev-client-invoice'))
    else:
        ledger.constraints.max_advance_minor = 600000
    with pytest.raises(CashflowError):
        create_plan_drafts(ledger, 'plan-a', preview['revision'], ctx=ctx(ledger))
    assert ledger.actions == {}


def test_partial_batch_replay_refuses_recreation(ledger):
    preview = plan_draft_preview(ledger, 'plan-b', ctx=ctx(ledger))
    batch = create_plan_drafts(ledger, 'plan-b', preview['revision'], ctx=ctx(ledger))
    del ledger.actions[batch['action_ids'][1]]
    with pytest.raises(IdempotencyConflict):
        create_plan_drafts(ledger, 'plan-b', preview['revision'], ctx=ctx(ledger))
    assert len(ledger.actions) == 1


@pytest.mark.parametrize('constraint', ['advance_cap', 'supplier', 'ops', 'protected', 'currency'])
def test_plan_drafting_obeys_constraints_and_currency(ledger, constraint):
    plan = 'plan-b'
    if constraint == 'advance_cap':
        ledger.constraints.max_advance_minor = 400000
        plan = 'plan-a'
    elif constraint == 'supplier':
        ledger.constraints.allow_supplier_date_change = False
    elif constraint == 'ops':
        ledger.constraints.allow_ops_deferral = False
    elif constraint == 'protected':
        ledger.outflows['supplier-001'].protected = True
    else:
        ledger.receivables['inv-client-001'].currency = 'EUR'
        plan = 'plan-a'
    with pytest.raises(CashflowError):
        plan_draft_preview(ledger, plan, ctx=ctx(ledger))
    assert ledger.actions == {}


def test_infeasible_capped_plan_remains_labelled_infeasible(delayed_ledger):
    preview = plan_draft_preview(delayed_ledger, 'plan-advance-capped', ctx=ctx(delayed_ledger))
    assert not preview['feasible']
    assert preview['drafts'][0]['amount_minor'] == 400000


def test_expired_preset_missing_evidence_and_unknown_plan(ledger):
    with pytest.raises(CashflowError, match='Unknown plan'):
        plan_draft_preview(ledger, 'approve_everything', ctx=ctx(ledger))
    ledger.as_of = date(2026, 11, 1)
    with pytest.raises(CashflowError, match='before the ledger'):
        plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))
    ledger.as_of = date(2026, 10, 16)
    del ledger.evidence['ev-client-invoice']
    with pytest.raises(CashflowError, match='evidence record is missing'):
        plan_draft_preview(ledger, 'plan-a', ctx=ctx(ledger))


def test_http_stress_and_review_are_read_only(live_server):
    base, db = live_server
    before = vars(_load(db))
    for path in ('/stress', '/stress?delay_days=14', '/plans/plan-a/draft', '/plans/plan-b/draft'):
        status, html = _get(base, path)
        assert status == 200
        assert 'SYNTHETIC DEMO' in html
    assert vars(_load(db)) == before
    _, html = _get(base, '/stress?delay_days=30')
    assert 'outside the forecast window' in html
    assert 'USD' in html and 'EUR' in html


@pytest.mark.parametrize('query', ['delay_days=31', 'delay_days=-1', 'delay_days=1.2', 'receivable_id=wrong'])
def test_http_invalid_stress_returns_clean_400(live_server, query):
    import urllib.error
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as failure:
        _get(base, '/stress?' + query)
    assert failure.value.code == 400


def test_http_plan_review_draft_creation_retry_and_csrf(live_server):
    base, db = live_server
    token = revision(base)
    assert _post(base, '/plans/plan-a/draft', {'revision': token})[0] == 403
    fields = {'csrf': web.CSRF_TOKEN, 'revision': token}
    before = financial_state(_load(db))
    assert _post(base, '/plans/plan-a/draft', dict(fields, amount_minor=1))[0] == 400
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(lambda _: _post(base, '/plans/plan-a/draft', fields)[0], range(4)))
    assert codes == [200] * 4
    loaded = _load(db)
    assert len(loaded.actions) == 1
    assert all(a.state == 'draft' for a in loaded.actions.values())
    assert financial_state(loaded) == before


def test_http_stale_review_and_plan_b_drafts(live_server):
    base, db = live_server
    token = revision(base)
    fields = {'csrf': web.CSRF_TOKEN}
    assert _post(base, '/demo/apply-delay', fields)[0] == 200
    code, html = _post(base, '/plans/plan-a/draft', dict(fields, revision=token))
    assert code == 400 and 'ledger changed' in html
    token = revision(base, 'plan-b')
    _, preview = _get(base, '/plans/plan-b/draft')
    assert 'Not feasible under current evidence' in preview
    assert _post(base, '/plans/plan-b/draft', dict(fields, revision=token))[0] == 200
    loaded = _load(db)
    assert len(loaded.actions) == 2
    assert loaded.consent_map == {}
    assert loaded.receipts == {}


def test_http_plan_draft_edit_keeps_currency_and_refreshes_display(live_server):
    base, db = live_server
    fields = {'csrf': web.CSRF_TOKEN}
    web.mutate_state(lambda ledger: setattr(ledger.outflows['supplier-001'], 'currency', 'EUR'))
    token = revision(base, 'plan-b')
    assert _post(base, '/plans/plan-b/draft', dict(fields, revision=token))[0] == 200
    ledger = _load(db)
    supplier = next(a for a in ledger.actions.values() if a.kind == 'request_supplier_extension')
    assert _post(base, f'/actions/{supplier.id}/update-terms',
        dict(fields, amount_major='1234.00'))[0] == 200
    changed = _load(db).actions[supplier.id]
    assert changed.terms['currency'] == 'EUR'
    assert changed.terms['amount_minor'] == 123400
    assert changed.terms['amount'] == 'EUR 1,234.00'
    assert changed.state == 'draft' and changed.approved_version is None


def test_stress_currency_without_cash_account_needs_clarification(ledger):
    ledger.receivables['inv-client-001'].currency = 'JPY'
    with pytest.raises(CashflowError, match='no cash account'):
        stress_test(ledger, 'inv-client-001', 7, ctx=ctx(ledger))


def test_plan_after_receipt_cannot_double_count_collected_invoice(ledger):
    from cashflow_copilot.plans import build_plan_a, evaluate_plan
    ledger.record_receipt(Receipt('already-collected', 'inv-client-001', 500000,
        date(2026, 10, 22), evidence_id='ev-client-invoice'))
    result = evaluate_plan(ledger, build_plan_a())
    assert not result.feasible
    assert result.forecast is None
    assert any('exceed its remaining balance' in r for r in result.reasons)


@pytest.mark.parametrize('amount', [0, -100, True])
def test_invalid_projected_money_never_enters_plan_forecast(ledger, amount):
    from cashflow_copilot.plans import build_advance_plan, evaluate_plan
    result = evaluate_plan(ledger, build_advance_plan(amount))
    assert not result.feasible and result.forecast is None
