"""Product home is public; entering the workspace remains explicit."""
import copy

import pytest

from cashflow_copilot import agent_web, web
from cashflow_copilot.store import SQLiteStore
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_web import live_server, _get, _load


@pytest.mark.parametrize('path', ['/', '/welcome'])
def test_home_and_legacy_alias_are_public_without_workspace_or_provider(public_server, monkeypatch, path):
    app, database = public_server
    before = copy.deepcopy(vars(_load(database)))
    def forbidden(*args, **kwargs):
        raise AssertionError('Product home must not read workspace data or model configuration')
    monkeypatch.setattr(web, 'read_state', forbidden)
    monkeypatch.setattr(agent_web, 'live_adapter', forbidden)
    status, body, headers = Client(app).request(path)
    assert status == 200 and not headers.get('Set-Cookie')
    assert b'<body class="site">' in body
    assert b'See your cash.' in body and b'href="/app"' in body
    assert b'<a href="/" aria-current="page">Home</a>' in body
    with app.visitors._db() as db:
        assert db.execute('SELECT count(*) FROM visitors').fetchone()[0] == 0
    assert vars(_load(database)) == before


def test_home_remains_reachable_when_workspace_capacity_is_full(public_server):
    app, _ = public_server
    app.visitors.capacity = 1
    existing = Client(app)
    assert existing.request('/app')[0] == 200
    before = copy.deepcopy(vars(_load(existing.database)))
    fresh = Client(app)
    assert fresh.request('/app')[0] == 503
    status, body, headers = fresh.request('/')
    assert status == 200 and b'See your cash.' in body and not headers.get('Set-Cookie')
    assert existing.request('/app')[0] == 200
    assert vars(_load(existing.database)) == before


def test_entering_app_creates_workspace_and_overview_links_stay_in_app(public_server):
    app, _ = public_server
    visitor = Client(app)
    assert visitor.request('/')[0] == 200 and not visitor.cookie
    status, body, headers = visitor.request('/app')
    assert status == 200 and headers.get('Set-Cookie') and visitor.cookie
    assert b'Business overview' in body and b'<body class="app">' in body
    assert b'href="/app" class="side-link selected" aria-current="page"' in body
    assert b'href="/" aria-label="Fluxora product home"' in body


def test_local_root_is_fixture_only_while_app_retains_saved_business(live_server):
    base, database = live_server
    store = SQLiteStore(database)
    try:
        ledger = store.load_ledger()
        ledger.business_name = 'LOCAL-PRIVATE-HOME-ROUTING-CANARY'
        store.save_ledger(ledger)
    finally:
        store.close()
    before = copy.deepcopy(vars(_load(database)))
    for path in ['/', '/welcome']:
        status, body = _get(base, path)
        assert status == 200 and '<body class="site">' in body
        assert 'LOCAL-PRIVATE-HOME-ROUTING-CANARY' not in body
    status, body = _get(base, '/app')
    assert status == 200 and 'LOCAL-PRIVATE-HOME-ROUTING-CANARY' in body
    assert vars(_load(database)) == before
