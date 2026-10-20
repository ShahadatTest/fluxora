"""Cookie-free probes/previews, bounded admission and stable synthetic examples."""
import copy
import sqlite3
import threading

import pytest

from cashflow_copilot import hosted,web
from cashflow_copilot.demo_examples import seed,EXAMPLES
from cashflow_copilot.forecast import forecast
from cashflow_copilot.money import format_amount
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_web import _load


def count(app):
    with sqlite3.connect(app.visitors.registry) as db:return db.execute('SELECT count(*) FROM visitors').fetchone()[0]


@pytest.mark.parametrize('path',['/welcome','/welcome?example=delay','/docs','/docs/security','/docs/missing',
    '/product','/how-it-works','/trust','/about','/assets/fluxora.js','/assets/fluxora.svg',
    '/assets/missing','/assets/../state.sqlite','/favicon.ico','/healthz','/not-a-real-route',
    '/demo/examples','/demo/examples?example=studio','/demo/examples?example=retail'])
def test_cookie_free_pages_and_probes_never_allocate_or_read_legacy(public_server,monkeypatch,path):
    app,db=public_server;before=copy.deepcopy(vars(_load(db)))
    def forbidden(*args,**kwargs):raise AssertionError('Public routes must never load legacy financial data')
    monkeypatch.setattr(web,'read_state',forbidden)
    for _ in range(2):
        status,body,headers=Client(app).request(path)
        assert status in {200,404} and not headers.get('Set-Cookie')
    assert count(app)==0 and vars(_load(db))==before


def test_public_assets_do_not_parse_or_rotate_existing_cookie(public_server):
    app,_=public_server;a=Client(app);assert a.request()[0]==200
    before=copy.deepcopy(vars(_load(a.database)))
    for path in ['/welcome','/assets/fluxora.js','/docs/telegram','/demo/examples?example=retail']:
        assert not a.request(path)[2].get('Set-Cookie')
    assert count(app)==1 and vars(_load(a.database))==before


def test_capacity_full_still_allows_examples_and_preserves_existing_workspace(public_server):
    app,_=public_server;app.visitors.capacity=1;a=Client(app);assert a.request()[0]==200
    original=a.cookie;before=copy.deepcopy(vars(_load(a.database)));b=Client(app)
    status,body,headers=b.request('/app')
    assert status==503 and b'/demo/examples' in body and not headers.get('Set-Cookie')
    for path in ['/demo/examples?example=retail','/welcome','/assets/fluxora.css','/healthz']:
        assert Client(app).request(path)[0]==200
    assert count(app)==1 and a.request()[0]==200 and a.cookie==original
    assert vars(_load(a.database))==before


def test_named_seeds_have_different_exact_inputs_and_matching_evidence():
    positions=[]
    for name in EXAMPLES:
        ledger=seed(name);positions.append(ledger.actual_cash('USD'))
        result=forecast(ledger,ledger.as_of,ledger.horizon_days)
        assert set(result.currencies)=={'USD','EUR'}
        assert ledger.outflows['payroll-001'].protected and not ledger.receipts and not ledger.actions
        assert not ledger.runtime_evidence_ids and not ledger.consent_map
        invoice=ledger.receivables['inv-client-001']
        display=format_amount(invoice.original_minor,'USD')
        assert (display if name!='atlas' else display.removesuffix('.00')) in ledger.evidence['ev-client-invoice'].summary
    assert len(set(positions))==3
    assert forecast(seed('retail'),seed('retail').as_of,30).currencies['USD'].worst_shortfall_minor>0


def test_selected_new_example_persists_across_refresh_and_server_restart(public_server):
    app,_=public_server;a=Client(app)
    assert a.request('/app?demo_example=studio')[0]==200
    initial=copy.deepcopy(vars(_load(a.database)));assert initial['business_id']=='demo-studio'
    token=a.cookie;csrf=a.csrf
    for path in ['/app?demo_example=retail','/app','/demo/examples?example=retail','/forecast']:
        assert a.request(path)[0]==200
    assert vars(_load(a.database))==initial and a.cookie==token
    app.shutdown();app.server_close()
    restarted=hosted.DemoServer(('127.0.0.1',0),hosted.Handler,app.config)
    thread=threading.Thread(target=restarted.serve_forever,daemon=True);thread.start()
    try:
        a.app=restarted;assert a.request('/app')[0]==200
        assert a.cookie==token and a.csrf==csrf and vars(_load(a.database))==initial
    finally:restarted.shutdown();restarted.server_close();thread.join(5)


@pytest.mark.parametrize('path',['/app?demo_example=bad','/app?demo_example=','/app?demo_example=atlas&demo_example=studio',
    '/demo/examples?example=../','/demo/examples?example=studio&example=retail','/demo/examples?workspace=anything'])
def test_bad_example_selection_allocates_nothing(public_server,path):
    app,_=public_server;status,_,headers=Client(app).request(path)
    assert status==400 and not headers.get('Set-Cookie') and count(app)==0


def test_capacity_config_is_bounded_and_does_not_change_ttl_or_storage(monkeypatch):
    monkeypatch.setenv('FLUXORA_DOMAIN','fluxora.sklab.cc');monkeypatch.setenv('FLUXORA_PUBLIC_DEMO','1')
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False)
    monkeypatch.delenv('FLUXORA_VISITOR_CAPACITY',raising=False)
    assert hosted.environment_config().visitor_capacity==128
    for invalid in ['0','257','-1','unlimited','9999']:
        monkeypatch.setenv('FLUXORA_VISITOR_CAPACITY',invalid)
        with pytest.raises(ValueError):hosted.environment_config()
