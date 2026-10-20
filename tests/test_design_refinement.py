"""Navigation and provenance contracts for the product/workspace refinement."""
import copy
from html.parser import HTMLParser
import re
import pytest
from cashflow_copilot import web, agent_web, site_web
from tests.test_web import live_server, _get, _load


@pytest.fixture(autouse=True)
def mounted():
    agent_web.mount(web)


class Document(HTMLParser):
    def __init__(self):
        super().__init__();self.ids=[];self.fragments=[];self.links=[];self.text=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'id' in attrs:self.ids.append(attrs['id'])
        if tag=='a':
            href=attrs.get('href','');self.links.append(href)
            if href.startswith('#'):self.fragments.append(href[1:])
    def handle_data(self,text):self.text.append(text)


def test_product_navigation_and_examples_remain_read_only(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    for variant,amount in [('baseline','USD 6,000.00'),('delay','USD -500.00')]:
        _,html=_get(base,'/welcome?example='+variant)
        doc=Document();doc.feed(html)
        assert len(doc.ids)==len(set(doc.ids))
        assert set(doc.fragments)<=set(doc.ids)
        assert amount in html and 'EUR 1,000.00' in html
        assert 'Inspect the computed daily data' in html
        assert 'Illustrative artwork · no financial data.' in html
        assert 'Concept illustration · not financial data or evidence' in html
        assert 'The hosted operator can explicitly enable bounded public guided AI.' in html
        for href in ['/ledger','/cash-accounts','/actions','/telegram/demo','/docs','/docs/model-configuration']:
            assert href in doc.links
        # The existing floating local guide has a client-only question form;
        # the product narrative and financial preview have no submission form.
        main=re.search(r'<main\b[^>]*>(.*?)</main>',html,re.S).group(1)
        assert not re.search(r'<form\b',main)
    assert vars(_load(db))==before


def test_documentation_groups_contents_and_original_body_are_reachable_without_script(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    expected={'/docs/'+key for key in site_web.DOCS}
    for slug,(_,_,original) in site_web.DOCS.items():
        _,html=_get(base,'/docs/'+slug)
        doc=Document();doc.feed(html)
        assert expected<=set(doc.links)
        assert len(doc.ids)==len(set(doc.ids))
        assert set(doc.fragments)<=set(doc.ids)
        # New heading IDs must not remove existing documentation facts.
        original_doc=Document();original_doc.feed(original)
        assert ''.join(original_doc.text) in ''.join(doc.text)
        assert 'aria-current="page">'+site_web.DOCS[slug][0]+'</a>' in html
    assert vars(_load(db))==before


@pytest.mark.parametrize('path,parent,children',[
    ('/receipts/inbox','/statements',['/statement-mapping','/batch-matching','/receipts','/corrections']),
    ('/payment-corrections','/payments',['/recurring','/payment-warnings','/instalments']),
    ('/facts','/ledger',['/attachments','/freshness','/entries','/search']),
])
def test_compact_family_navigation_preserves_destinations_and_current_page(live_server,path,parent,children):
    _,html=_get(live_server[0],path)
    section=re.search(r'<details class="section-navigation">(.*?)</details>',html,re.S).group(1)
    doc=Document();doc.feed(section)
    assert set([parent]+children)<=set(doc.links)
    assert 'href="'+path+'" aria-current="page"' in section
    assert 'related views' in section and '<nav class="local-tabs"' in section


def test_guide_position_announcement_and_existing_duplicate_callout_contract(live_server):
    _,html=_get(live_server[0],'/app')
    assert 'data-guide-progress aria-live="polite" aria-atomic="true"' in html
    # Actual hiding is a local guide preference, not a server financial state.
    assert 'data-guided-rail' in html and 'start-callout' in html
    assert '<a class="today-link" href="/today">' in html
