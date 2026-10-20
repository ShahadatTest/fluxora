"""Local HTTP stubs and in-process fakes only. No live-model tests."""
import json
import time

import pytest

from cashflow_copilot import agent_web, web
from cashflow_copilot.agent import list_agent_runs, run_agent_question
from cashflow_copilot.model_adapter import AdapterError, AdapterResult, ToolCall
from tests.test_model_adapter import (
    CANARY_KEY, _StubHandler, _adapter, _completion, stub,
)
from tests.test_web import live_server, _get, _post


@pytest.mark.parametrize('body', [
    {'error': {'message': 'Bearer ' + CANARY_KEY}},
    'unstructured secret: ' + CANARY_KEY,
    {'error': {'message': 'A different secret must also be discarded'}},
])
def test_http_401_never_displays_or_persists_provider_body(stub, ledger, tmp_path, body):
    _StubHandler.responses = [(401, body)]
    adapter = _adapter(stub)
    db = tmp_path / 'runs.sqlite'
    run = run_agent_question(ledger, 'why?', adapter, db_path=db)
    surfaces = json.dumps([run.answer, adapter.status().__dict__, list_agent_runs(db)])
    assert CANARY_KEY not in surfaces
    assert 'unstructured secret' not in surfaces
    assert 'A different secret' not in surfaces
    assert 'failed' in run.answer
    assert len(_StubHandler.requests) == 1
    assert CANARY_KEY.encode() not in db.read_bytes()


def test_http_verification_survives_reload_and_invalidates_config(stub, live_server, monkeypatch):
    base, db = live_server
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY', CANARY_KEY)
    monkeypatch.setenv('CASHFLOW_MODEL_BASE_URL', stub)
    monkeypatch.setenv('CASHFLOW_MODEL_NAME', 'stub-model')
    agent_web.reset_live_adapter()
    agent_web.mount(web)
    _StubHandler.responses = [(200, _completion('pong'))]
    code, body = _post(base, '/agent/verify', {'csrf': web.CSRF_TOKEN})
    assert code == 200
    for path in ('/', '/agent', '/plans'):
        code, body = _get(base, path)
        assert code == 200
        assert 'Live provider: verified' in body
        assert 'configured-but-unverified' not in body
        assert 'AI not connected' not in body
        assert CANARY_KEY not in body
    assert len(_StubHandler.requests) == 1  # page reloads cause no calls
    for variable, value in [('CASHFLOW_MODEL_NAME', 'other-model'),
                            ('CASHFLOW_MODEL_API_KEY', 'dummy-replacement'),
                            ('CASHFLOW_MODEL_BASE_URL', stub + '/v1')]:
        monkeypatch.setenv(variable, value)
        assert agent_web._live_status().state == 'configured-but-unverified'
        # Simulate a successful call for each preceding configuration.
        agent_web.live_adapter()._state = 'verified'
    agent_web.reset_live_adapter()  # same operation used by run_server
    assert agent_web._live_status().state == 'configured-but-unverified'
    assert CANARY_KEY.encode() not in __import__('pathlib').Path(db).read_bytes()
    agent_web.reset_live_adapter()


class LateAdapter:
    provider_label = 'test fake, no live model'

    def __init__(self, draft=False):
        self.draft = draft
        self.deadline = None

    def chat(self, messages, tools, *, deadline=None):
        self.deadline = deadline
        time.sleep(0.08)
        return AdapterResult(text='late final answer', tool_calls=[ToolCall(
            'late', 'draft_action', {'action_id': 'late-draft',
            'kind': 'request_early_payment', 'receivable_id': 'inv-client-001',
            'amount_minor': 500000, 'date': '2026-10-22'})] if self.draft else [])


@pytest.mark.parametrize('draft', [False, True])
def test_fake_late_result_discarded_before_tools(ledger, tmp_path, draft):
    adapter = LateAdapter(draft)
    db = tmp_path / 'late.sqlite'
    run = run_agent_question(ledger, 'q', adapter, time_budget_s=0.03, db_path=db)
    assert adapter.deadline is not None
    assert 'late final answer' not in run.answer
    assert 'budget' in run.answer
    assert run.tool_calls == []
    time.sleep(0.1)  # no background worker can later create the draft
    assert ledger.actions == {}
    assert 'late final answer' not in list_agent_runs(db)[0]['answer_summary']


def test_http_timeout_uses_remaining_run_deadline(stub, ledger):
    _StubHandler.sleep_s = 0.3
    _StubHandler.responses = [(200, _completion('late answer'))]
    start = time.monotonic()
    run = run_agent_question(ledger, 'q', _adapter(stub, timeout_s=20), time_budget_s=0.05)
    assert time.monotonic() - start < 0.25
    assert 'late answer' not in run.answer
    assert run.tool_calls == []


def test_http_retry_backoff_does_not_exceed_remaining_budget(stub, ledger):
    _StubHandler.responses = [(429, {'error': {'message': CANARY_KEY}})] * 10
    start = time.monotonic()
    run = run_agent_question(ledger, 'q', _adapter(stub), time_budget_s=0.1)
    assert time.monotonic() - start < 0.5
    assert len(_StubHandler.requests) == 1
    assert CANARY_KEY not in run.answer


def test_http_response_byte_limit(stub):
    _StubHandler.responses = [(200, _completion('x' * 1_100_000))]
    with pytest.raises(AdapterError, match='byte limit'):
        _adapter(stub).chat([], [])


@pytest.mark.parametrize('payload', [
    {'choices': [{'message': 'bad'}]},
    {'choices': [{'message': {'tool_calls': ['bad']}}]},
    {'choices': [{'message': {'tool_calls': [{'function': {'arguments': 123}}]}}]},
])
def test_http_malformed_shapes_fail_cleanly(stub, payload):
    _StubHandler.responses = [(200, payload)]
    with pytest.raises(AdapterError, match='malformed'):
        _adapter(stub).chat([], [])


def test_http_verify_requires_model_without_network(stub):
    adapter = _adapter(stub, model=None)
    assert adapter.verify().state == 'error'
    assert 'CASHFLOW_MODEL_NAME' in adapter.status().detail
    with pytest.raises(AdapterError, match='CASHFLOW_MODEL_NAME'):
        adapter.chat([], [])
    assert adapter.status().state == 'error'
    assert _StubHandler.requests == []


def test_http_errors_do_not_echo_malformed_tool_name(stub):
    calls = [{'id': 'x', 'function': {'name': CANARY_KEY, 'arguments': 'bad'}}]
    _StubHandler.responses = [(200, _completion(tool_calls=calls))]
    with pytest.raises(AdapterError) as exc:
        _adapter(stub).chat([], [])
    assert CANARY_KEY not in str(exc.value)


def test_unverified_prose_and_authoritative_tool_results_visible(live_server):
    base, db = live_server
    agent_web.mount(web)
    code, body = _post(base, '/agent/ask', {'csrf': web.CSRF_TOKEN,
        'request_id':'a'*32,'business_id':'demo-agency',
        'provider': 'mock', 'question': 'Why will we breach our cash floor?'})
    assert code == 200
    assert 'Model commentary (unverified)' in body
    assert 'Deterministic tool result and evidence' in body
    assert 'source' in body
    assert list_agent_runs(db)[0]['tool_calls'][0]['result']


def test_supplier_acceptance_cannot_record_incoming_cash(live_server):
    from tests.test_web import _load
    base, db = live_server
    fields = {'csrf': web.CSRF_TOKEN}
    code, _ = _post(base, '/actions/create', dict(fields, action_id='supplier-test',
        kind='request_supplier_extension', outflow_id='supplier-001', date='2026-11-06'))
    assert code == 200
    for step in ('approve', 'send'):
        assert _post(base, '/actions/supplier-test/' + step, fields)[0] == 200
    assert _post(base, '/actions/supplier-test/respond', dict(fields, accepted='true'))[0] == 200
    _, html = _get(base, '/actions')
    assert '/actions/supplier-test/observe' not in html
    code, _ = _post(base, '/actions/supplier-test/observe', dict(fields,
        receipt_id='invalid-income', amount_major='5000.00', date_received='2026-10-22'))
    assert code == 400
    assert _load(db).receipts == {}
    assert _load(db).receivables['inv-client-001'].remaining_minor == 800000


@pytest.mark.parametrize('mismatch', ['kind', 'invoice', 'business'])
def test_receipt_approval_matches_purpose_invoice_and_business(ledger, mismatch):
    from datetime import date
    from cashflow_copilot import actions
    from cashflow_copilot.errors import CashflowError
    from cashflow_copilot.models import Receipt
    action = actions.create_action('approval', 'request_early_payment',
        {'receivable_id': 'inv-client-001'}, actor='owner')
    actions.approve(action, actor='owner')
    if mismatch == 'kind':
        action.kind = 'request_supplier_extension'
    elif mismatch == 'invoice':
        action.terms['receivable_id'] = 'another-invoice'
    else:
        action.business_id = 'another-business'
    receipt = Receipt(id='wrong-purpose', receivable_id='inv-client-001',
        amount_minor=10000, date_received=date(2026, 10, 22), evidence_id='ev-client-invoice')
    with pytest.raises(CashflowError):
        ledger.record_receipt(receipt, authorizing_action=action)
    assert ledger.receipts == {}
