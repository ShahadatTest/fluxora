"""Deterministic grounding regressions. No live provider calls."""
import copy
from datetime import date

import pytest

from cashflow_copilot import agent, agent_web, web
from cashflow_copilot.agent_disclosure import outbound_filter
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Account
from tools.live_cases import case_ledger
from tests.test_web import _post, live_server


def compute(ledger, arguments=None):
    return agent.execute_tool(RequestContext('agent', 'agent', ledger.business_id), ledger,
                              'compute_forecast', arguments or {}, provider_label='scripted test')['result']


def test_no_breach_default_seed_still_supplies_currency_scoped_sources():
    ledger = Ledger.seed(); before = copy.deepcopy(vars(ledger))
    result = compute(ledger)
    usd = result['currencies']['USD']; eur = result['currencies']['EUR']
    assert usd['first_breach_date'] is None
    refs = {ref for row in usd['source_basis']['inputs'] + usd['source_basis']['drivers'] for ref in row['evidence_ids']}
    assert {'ev-client-invoice', 'ev-payroll', 'ev-supplier-invoice', 'ev-ops-budget'} <= refs
    assert all(row['id'] != 'inv-client-001' for row in eur['source_basis']['drivers'])
    assert not usd['source_basis']['truncated']
    assert vars(ledger) == before | {'audit': ledger.audit}


@pytest.mark.parametrize('case_id', ['breach', 'own_accounts', 'restricted_cash', 'credit_customer', 'credit_supplier', 'future_loan'])
def test_source_drivers_reconcile_exact_currency_closing(case_id):
    result = compute(case_ledger(case_id))
    for values in result['currencies'].values():
        basis = values['source_basis']
        assert not basis['truncated']
        assert values['opening_minor'] + sum(row['signed_minor'] for row in basis['drivers']) == values['closing_minor']


@pytest.mark.parametrize('percent', [0, 50, 100])
def test_collection_stress_source_amount_is_conditional_and_matches_result(percent):
    ledger = case_ledger('breach')
    result = compute(ledger, {'scenario': {'receivable_id': 'inv-x', 'collection_percent': percent}})
    values = result['currencies']['USD']
    rows = values['source_basis']['drivers']
    selected = next(row for row in rows if row['id'] == 'inv-x')
    assert selected['signed_minor'] == 65000 * percent // 100
    assert 'hypothetical' in selected['basis'] and 'ev-customer' in selected['evidence_ids']
    assert values['opening_minor'] + sum(row['signed_minor'] for row in rows) == values['closing_minor']
    assert ledger.receivables['inv-x'].remaining_minor == 65000


def test_date_shift_source_uses_scenario_date_without_changing_recorded_date():
    ledger = case_ledger('delay')
    result = compute(ledger, {'scenario': {'receivable_id': 'inv-x', 'shift_receivable_days': 7}})
    row = next(r for r in result['currencies']['USD']['source_basis']['drivers'] if r['id'] == 'inv-x')
    assert row['date'] == '2027-01-19'
    assert ledger.receivables['inv-x'].expected_date == date(2027, 1, 12)


def test_missing_source_is_explicit_and_does_not_invent_an_evidence_id():
    ledger = case_ledger('breach'); ledger.accounts['eur'].evidence_id = None
    basis = compute(ledger)['currencies']['EUR']['source_basis']
    assert basis['inputs'][0]['evidence_ids'] == []


def test_minimized_reply_preserves_sources_and_omits_original_text():
    ledger = case_ledger('breach'); result = compute(ledger)
    filtered = outbound_filter(ledger, 'minimized')(result)
    assert filtered['currencies']['USD']['source_basis']['drivers'] == result['currencies']['USD']['source_basis']['drivers']
    assert 'note' not in filtered['currencies']['USD']['source_basis']


def test_source_rows_are_bounded_with_visible_omissions():
    ledger = case_ledger('breach')
    for n in range(105): ledger.accounts[f'extra-{n}'] = Account(f'extra-{n}', 'EUR', 1, 0)
    basis = compute(ledger)['currencies']['EUR']['source_basis']
    assert basis['input_count'] == 106 and len(basis['inputs']) == 100 and basis['truncated']


def test_normal_ui_displays_sources_outside_unverified_commentary(live_server):
    base, _ = live_server; agent_web.mount(web)
    status, page = _post(base, '/agent/ask', dict(csrf=web.CSRF_TOKEN, request_id='b'*32,
        business_id='demo-agency', question='Why will we breach our cash floor?', provider='mock', data_mode='minimized'))
    assert status == 200
    assert 'Sources returned by deterministic tools' in page and 'ev-payroll' in page
    assert 'Model commentary (unverified)' in page and 'approval alone creates no cash' in page


def test_prompt_distinguishes_agent_limits_from_owner_recording():
    assert 'These limits describe YOU, the agent' in agent.SYSTEM_PROMPT
    assert 'source-backed recording' in agent.SYSTEM_PROMPT
    assert 'call get_evidence' in agent.SYSTEM_PROMPT


def test_section_name_is_not_an_evidence_id_and_recovery_returns_loan_positions():
    ledger = case_ledger('loan_position')
    ctx = RequestContext('agent', 'agent', ledger.business_id)
    with pytest.raises(agent.ToolValidationError, match='Call get_evidence with'):
        agent.execute_tool(ctx, ledger, 'get_evidence', {'evidence_id': 'loan_positions'})
    payload = agent.execute_tool(ctx, ledger, 'get_evidence', {})['result']
    assert payload['loan_positions'][0]['outstanding_principal_minor'] == 8000
    spec = agent.TOOLS['get_evidence']
    assert 'NOT evidence IDs' in spec.description
