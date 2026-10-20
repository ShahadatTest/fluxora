"""HTTP/navigation, disclosure and unchanged-state checks for the Fluxora UI."""
import copy
import json
import re
import threading
import urllib.error
import urllib.request
from html.parser import HTMLParser

import pytest

from cashflow_copilot import web,agent_web,site_web,ui,task_qualification
from tests.test_web import live_server,csrf,_get,_post,_load
from tests.test_guided_tasks import Parser


@pytest.fixture(autouse=True)
def mounted():
    agent_web.mount(web)


class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag=='a':
            href=dict(attrs).get('href','')
            if href.startswith('/') and not href.startswith('//'):self.links.append(href.split('#')[0])


def test_multipage_site_docs_and_nav_links_are_real_get_pages_and_read_only(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    pages=['/welcome','/product','/how-it-works','/trust','/about','/docs','/workspace/features','/app','/forecast']
    pages+=['/docs/'+slug for slug in site_web.DOCS]
    links=set()
    for path in pages:
        status,body=_get(base,path)
        assert status==200 and 'Fluxora' in body and '<main id="main"' in body
        parser=Links();parser.feed(body);links.update(parser.links)
    # Validate the complete static navigation mapping, including legacy pages.
    for _,rows in ui.GROUPS:
        for url,_,_,children in rows:links.add(url);links.update(p for p,_ in children)
    for path in sorted(links):
        status,body=_get(base,path)
        assert status==200,path
        assert '<h1' in body,path
    assert vars(_load(db))==before


def test_public_preview_is_synthetic_fixture_and_cannot_disclose_private_active_ledger(live_server):
    base,db=live_server
    from cashflow_copilot.store import SQLiteStore
    store=SQLiteStore(db)
    try:
        ledger=store.load_ledger();ledger.business_name='PRIVATE-NAME-UI-CANARY'
        ledger.accounts['usd-main'].opening_minor=999_987_600
        store.save_ledger(ledger)
    finally:store.close()
    _,site=_get(base,'/welcome');_,app=_get(base,'/app')
    assert 'PRIVATE-NAME-UI-CANARY' not in site and '9,999,876.00' not in site
    assert 'USD 10,000.00' in site and 'SYNTHETIC EXAMPLE' in site
    assert 'PRIVATE-NAME-UI-CANARY' in app and 'USD 9,999,876.00' in app


def test_dashboard_uses_engine_values_and_keeps_currency_and_authority_boundaries(live_server):
    base,db=live_server
    _,body=_get(base,'/')
    for expected in ('USD 10,000.00','EUR 1,000.00','USD 6,000.00','2026-10-28','2026-10-16','Expected collections','conditional','Protected','No real money'):
        assert expected in body,expected
    assert 'No floor breach under assumptions' in body
    assert "method='post' action='/demo/apply-delay'" not in body
    assert '/agent/tasks' in body and '/freshness' in body


def test_sidebar_selects_deep_page_and_exposes_related_destinations():
    assert ui.nav_match('/receipts/inbox')[1]=='/statements'
    assert ui.nav_match('/agent/requests/'+'a'*32)[1]=='/agent/tasks'
    assert ui.nav_match('/agent/workflows/example')[1]=='/agent/workflows'
    assert ui.nav_match('/scenarios')[1]=='/plans'
    assert ui.nav_match('/payment-corrections')[1]=='/payments'


def test_unknown_docs_and_assets_are_404_and_cannot_read_arbitrary_local_files(live_server):
    base,_=live_server
    for path in ('/docs/no-such-page','/assets/web.py','/assets/%2e%2e'):
        with pytest.raises(urllib.error.HTTPError) as exc:_get(base,path)
        assert exc.value.code==404
    for name,mime in (('fluxora.css','text/css'),('fluxora.js','text/javascript'),('fluxora.svg','image/svg+xml')):
        with urllib.request.urlopen(base+'/assets/'+name) as response:
            assert response.status==200 and response.headers['Content-Type'].startswith(mime)
            assert response.headers['X-Content-Type-Options']=='nosniff'
            assert len(response.read())>100


def test_guided_review_renders_exact_action_terms_without_creating_action_until_confirmed(live_server,monkeypatch,csrf):
    base,db=live_server;p=Parser('action_proposal',dict(kind='request_early_payment',target_id='inv-client-001',currency='USD',amount='75.00',date='2026-10-22'))
    monkeypatch.setattr(agent_web,'live_adapter',lambda:p)
    monkeypatch.setattr(task_qualification,'status',lambda *a:'qualified')
    question='Prepare early payment for inv-client-001 USD 75.00 on 2026-10-22. DRAFT only.'
    fields=dict(csrf=web.CSRF_TOKEN,question=question,business_id=_load(db).business_id,request_id='e'*32)
    status,body=_post(base,'/agent/tasks/preview',fields);assert status==200
    token=re.search("name='token' value='([^']+)'",body).group(1)
    status,body=_post(base,'/agent/tasks/translate',dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes'))
    assert status==200 and p.calls==1 and not _load(db).actions
    assert '<dt>Amount</dt><dd>USD 75.00</dd>' in body
    assert '<dt>Requested date</dt><dd>2026-10-22</dd>' in body
    assert '<dt>Record</dt><dd>inv-client-001</dd>' in body
    assert '<summary>Exact structured parameters</summary>' in body
    status,result=_post(base,'/agent/tasks/run',dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes'))
    assert status==200 and 'DRAFT, USD 75.00' in result
    current=_load(db);assert len(current.actions)==1 and not current.receipts
    action=next(iter(current.actions.values()))
    assert action.state=='draft' and action.approved_version is None and action.counterparty_response is None
    assert action.terms['amount_minor']==7500 and current.actual_cash('USD')==1000000
    assert _post(base,'/agent/tasks/run',dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes'))[0]==200
    assert len(_load(db).actions)==1 and p.calls==1


def test_review_escapes_exact_workflow_labels():
    body=ui.task_review({'task':'proposal','parameters':{'steps':[{'operation':'save_forecast','arguments':{'label':'<script>unsafe</script>'}}]}})
    assert '<script>' not in body and '&lt;script&gt;' in body
    assert 'Pending owner review' in body


def test_public_and_documentation_pages_have_no_external_asset_dependency(live_server):
    base,_=live_server
    for path in ('/welcome','/docs/local-setup','/product','/agent/tasks'):
        _,body=_get(base,path)
        assert not re.search(r'(?:src|href)=[\'"]https?://',body)
        assert '/assets/fluxora.css' in body and '/assets/fluxora.js' in body


def test_generated_logo_and_concept_art_are_local_real_png_assets(live_server):
    base,_=live_server
    from pathlib import Path
    for name in ('fluxora-mark-v1.png','fluxora-planning-v1.png','fluxora-evidence-v1.png'):
        with urllib.request.urlopen(base+'/assets/'+name) as response:
            data=response.read()
            assert response.headers['Content-Type']=='image/png'
            assert data.startswith(b'\x89PNG\r\n\x1a\n')
            assert data==(Path(site_web.__file__).parent/'static'/name).read_bytes()
    for page in ('/welcome','/app','/docs','/agent/tasks'):
        _,body=_get(base,page)
        assert '<img src="/assets/fluxora-mark-v1.png"' in body
        assert 'href="/assets/fluxora-mark-v1.png" type="image/png"' in body
        assert '\u00e2\u20ac\u201d' not in body  # literal UTF-8 em dash, not mojibake


def test_generated_illustrations_are_labelled_concepts_and_leave_financial_preview_intact(live_server):
    base,_=live_server
    for page,image in (('/welcome','fluxora-planning-v1.png'),('/product','fluxora-planning-v1.png'),('/how-it-works','fluxora-evidence-v1.png')):
        _,body=_get(base,page)
        assert '/assets/'+image in body
        assert 'Concept illustration · not financial data or evidence' in body
        assert 'loading="lazy"' in body
    _,home=_get(base,'/welcome')
    assert 'USD 10,000.00' in home and 'USD 6,000.00' in home and 'SYNTHETIC EXAMPLE' in home


def test_checkmark_styles_and_owner_form_labels_do_not_contain_mojibake(live_server):
    base,_=live_server
    with urllib.request.urlopen(base+'/assets/fluxora.css') as response:
        css=response.read().decode('utf-8')
    assert css.count(r"content:'\2713'")==2
    assert '\u00e2\u0153\u201c' not in css
    for page in ('/facts','/receipts','/daily'):
        _,body=_get(base,page)
        assert '\u00e2\u20ac\u201d' not in body and '\u00c2\u00b7' not in body


def test_public_product_examples_compute_real_baseline_and_delay_without_ledger_changes(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    _,baseline=_get(base,'/welcome?example=baseline')
    _,delay=_get(base,'/welcome?example=delay')
    assert 'USD 6,000.00' in baseline and 'No breach under assumptions' in baseline
    assert 'USD -500.00' in delay and 'USD 3,500.00' in delay and '2026-10-25' in delay
    for body in (baseline,delay):
        assert 'EUR 1,000.00' in body and 'SYNTHETIC EXAMPLE' in body
        assert 'Switching examples changes no saved record' in body
        assert '/welcome?example=baseline#product-preview' in body
        assert '/welcome?example=delay#product-preview' in body
        assert 'No real bank connection' in body
    assert 'example=baseline#product-preview" aria-current="page"' in baseline
    assert 'example=delay#product-preview" aria-current="page"' in delay
    assert vars(_load(db))==before


@pytest.mark.parametrize('query',['example=unknown','example=baseline&example=delay'])
def test_public_example_selector_refuses_unsupported_or_repeated_values(live_server,query):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    with pytest.raises(urllib.error.HTTPError) as exc:_get(base,'/welcome?'+query)
    assert exc.value.code==400 and vars(_load(db))==before
