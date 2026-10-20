"""Default packaged fixture/Decimal metadata preview regressions; no live calls."""
import copy
from decimal import Decimal, localcontext
import re

import pytest

from cashflow_copilot import agent, agent_web, web
from cashflow_copilot.agent_disclosure import disclosure_revision
from cashflow_copilot.planning import _hash
from cashflow_copilot.store import SQLiteStore
from tests.test_web import live_server, _get, _post, _load


@pytest.mark.parametrize('value', ['0.91', '0.9100000000000000000000000001', '1E-30'])
def test_decimal_metadata_hash_is_exact_and_distinct_from_text(value):
    number = Decimal(value)
    assert _hash(number) == _hash(copy.deepcopy(number))
    assert _hash(number) != _hash(value)
    with localcontext() as ctx:
        ctx.prec = 80
        assert _hash(number) != _hash(number + Decimal('0.0000000000000000000000000000000000000001'))


def test_packaged_seed_fx_revision_survives_reload_and_tracks_rate_change(live_server):
    _, db = live_server; before = _load(db)
    assert before.fx_quotes and isinstance(before.fx_quotes[0].rate, Decimal)
    revision = disclosure_revision(before)
    assert disclosure_revision(_load(db)) == revision
    cash = {a.currency: before.actual_cash(a.currency) for a in before.accounts.values()}
    changed = copy.deepcopy(before); changed.fx_quotes[0].rate += Decimal('0.00000001')
    assert disclosure_revision(changed) != revision
    assert {c: changed.actual_cash(c) for c in cash} == cash


@pytest.mark.parametrize('mode', ['minimized', 'full_metadata'])
def test_default_seed_normal_preview_confirm_and_replay_with_fx(live_server, monkeypatch, mode):
    base, db = live_server; agent_web.mount(web)
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY', raising=False); agent_web.reset_live_adapter()
    before = copy.deepcopy(vars(_load(db)))
    fields = dict(csrf=web.CSRF_TOKEN, request_id='f'*32, business_id='demo-agency',
        question='Why will we breach our cash floor?', provider='mock', data_mode=mode)
    status, page = _post(base, '/agent/preview', fields)
    assert status == 200 and 'Review AI request data' in page and not agent.list_agent_runs(db)
    token = re.search(r"name='token' value='([^']+)'", page).group(1)
    confirm = dict(csrf=web.CSRF_TOKEN, token=token, confirmed='yes')
    status, answer = _post(base, '/agent/ask', confirm)
    assert status == 200 and 'Request: completed' in answer
    assert 'compute_forecast' in answer and 'mocked provider' in answer
    assert _post(base, '/agent/ask', confirm)[0] == 200
    assert len(agent.list_agent_runs(db)) == 1
    after = vars(_load(db))
    assert {k:v for k,v in after.items() if k!='audit'} == {k:v for k,v in before.items() if k!='audit'}


def test_fx_rate_change_invalidates_reviewed_preview_without_model_call(live_server, monkeypatch):
    base, db = live_server; agent_web.mount(web)
    fields = dict(csrf=web.CSRF_TOKEN, request_id='e'*32, business_id='demo-agency',
        question='Why will we breach our cash floor?', provider='mock', data_mode='minimized')
    status, page = _post(base, '/agent/preview', fields); assert status == 200
    token = re.search(r"name='token' value='([^']+)'", page).group(1)
    s = SQLiteStore(db)
    try:
        l = s.load_ledger(); l.fx_quotes[0].rate += Decimal('0.01'); s.save_ledger(l)
    finally: s.close()
    monkeypatch.setattr('cashflow_copilot.model_adapter.MockProviderAdapter.chat',
                        lambda *a, **k: pytest.fail('Stale preview must not call model'))
    status, body = _post(base, '/agent/ask', dict(csrf=web.CSRF_TOKEN, token=token, confirmed='yes'))
    assert status == 400 and 'Ledger changed since the data preview' in body
    assert not agent.list_agent_runs(db)
