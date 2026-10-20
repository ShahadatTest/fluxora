"""Preserve the exact operation surface while reorganising workspace screens."""
import copy
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

import pytest

from cashflow_copilot import agent_web,site_web,ui,workspace_ui
from tests.test_web import live_server,_get,_load,_post,csrf


@pytest.fixture(autouse=True)
def mounted():
    from cashflow_copilot import web
    agent_web.mount(web)


def operation_surface(html):
    """Attributes and original text for every form/control, in source order."""
    tree=workspace_ui.Markup(html)
    result=[]
    for node in tree.elements:
        if node.tag not in {'form','input','select','textarea','button','option','label'}:continue
        if node.tag=='input' and 'data-table-filter' in node.attrs:continue
        if node.tag=='input' and 'data-feature-filter' in node.attrs:continue
        # New local table/directory search labels are outside operation forms.
        if node.tag=='label' and not any(n.tag=='form' for n in node.ancestors()):continue
        text=unescape(re.sub('<[^>]*>','',html[node.open_end:node.close_start])).strip() if node.tag in {'label','textarea','option','button'} else ''
        result.append((node.tag,node.attrs,text))
    return result


def test_every_navigation_page_has_explicit_purpose_family_and_real_help():
    for _,rows in ui.GROUPS:
        for url,_,_,children in rows:
            for path in [url]+[p for p,_ in children]:
                assert path in workspace_ui.SCREENS,path
                purpose,family,doc=workspace_ui.profile(path)
                assert len(purpose)>25 and family in workspace_ui.FAMILIES
                assert doc in site_web.DOCS,path


@pytest.mark.parametrize('path,family,phrase',[
    ('/agent/tasks/report/forecast','report','local report'),
    ('/agent/workflows/example','approval','exact operations'),
    ('/agent/requests/example','agent','does not retry'),
])
def test_detail_and_result_headers_describe_the_current_screen(path,family,phrase):
    purpose,kind,doc=workspace_ui.profile(path)
    assert kind==family and phrase in purpose and doc in site_web.DOCS


@pytest.mark.parametrize('body',[
    '<h1>Test &amp; inspect</h1><p>Keep &#x27;exact&#x27; text.</p>',
    '<div><h1>Review</h1><form method="post"><input type="hidden" name="csrf" value="a&amp;b"><label>Source<select name="evidence_id"><option value="">Choose</option><option value="x">X &amp; Y</option></select></label><label>Note<textarea name="note">A &lt; B</textarea></label><label><input name="confirmed" type="checkbox" value="yes" required> I reviewed source facts.</label><button name="decision" value="approve">Approve exact terms</button></form></div>',
    '<h1>Filter</h1><form method="get" action="/search"><label for="q">Query</label> <input id="q" name="q" value="A&amp;B"><button>Search</button></form>',
    '<h1>Upload</h1><form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="x"><label>CSV<input type="file" name="file" accept=".csv" required></label><button>Preview</button></form>',
    '<h1>Compact</h1><form class="inline" method="post" onsubmit="return confirm(\'Exact?\')"><input type="hidden" name="token" value="x"/><button class="danger">Reset</button></form>',
    '<h1>Malformed legacy</h1><div><p>Unclosed paragraph</div>',
])
def test_markup_roundtrip_and_submitted_controls_are_lossless(body):
    tree=workspace_ui.Markup(body)
    assert tree.root.render(body)==body
    new=workspace_ui.render('Test',body,'/entries')
    assert operation_surface(new)==operation_surface(body)
    assert new.count('<h1>')==1
    assert 'feature-header' in new


def test_visible_confirmation_text_is_not_moved_into_a_disclosure():
    source='<h1>Review operation</h1><form method="post"><label><input type="checkbox" name="confirmed" value="yes" required> Check source, payment observations and counterparty acceptance separately.</label><button>Confirm</button></form>'
    new=workspace_ui.render('Review',source,'/agent/workflows/example')
    tree=workspace_ui.Markup(new)
    control=next(n for n in tree.elements if n.tag=='input')
    assert not any(n.tag=='details' for n in control.ancestors())
    assert control.attrs=={'type':'checkbox','name':'confirmed','value':'yes','required':None}
    assert 'form-field--check' in new and 'Read every confirmation' in new


def test_only_link_only_paragraphs_can_be_collapsed():
    links=' · '.join('<a href="/records">Records</a>' for _ in range(5))
    source='<h1>Example</h1><p>'+links+'</p><p>Protected payroll warning. '+links+'</p>'
    new=workspace_ui.render('Example',source,'/payments')
    assert new.count('class="related-pages"')==1
    tree=workspace_ui.Markup(new)
    warning=next(n for n in tree.elements if n.tag=='p' and 'Protected payroll' in new[n.start:n.end])
    assert not any(n.tag=='details' for n in warning.ancestors())
    sources=' · '.join('<a href="/ledger#evidence-'+str(i)+'">Source</a>' for i in range(5))
    assert 'class="related-pages"' not in workspace_ui.render('Sources','<h1>Sources</h1><p>'+sources+'</p>','/ledger')


def test_table_filter_is_only_added_to_read_only_tables_and_preserves_row_order():
    rows=''.join('<tr><td>USD '+str(i)+'</td></tr>' for i in range(8))
    source='<h1>Report</h1><h2>Cash</h2><table><tr><th>Amount</th></tr>'+rows+'</table>'
    new=workspace_ui.render('Report',source,'/reports')
    assert 'data-table-filter=' in new and 'data-table-filter="workspace-table-1"' in new
    assert '<label class="table-filter" hidden>' in new
    assert '8 rows shown' in new and rows in new
    assert '<th scope="col">Amount</th>' in new
    edited=source.replace('<td>USD 0</td>','<td><form method="post"><button>Reconcile</button></form></td>')
    assert 'data-table-filter=' not in workspace_ui.render('Report',edited,'/reports')
    in_form='<h1>Report</h1><form method="post">'+source[source.index('<table>'):]+'</form>'
    assert 'data-table-filter=' not in workspace_ui.render('Report',in_form,'/reports')


def test_generated_ids_avoid_collisions_and_closed_detail_headings_are_not_dead_links():
    source='<h1>Report</h1><div id="workspace-section-1"></div><h2>A</h2><h2>B</h2><details><summary>Raw</summary><h2>Hidden heading</h2></details><div id="workspace-table-1"></div><table><tr><td>x</td></tr></table>'
    new=workspace_ui.render('Report',source,'/reports')
    tree=workspace_ui.Markup(new);ids=[n.attrs['id'] for n in tree.elements if n.attrs.get('id')]
    assert len(ids)==len(set(ids))
    assert '>Hidden heading</a>' not in new
    assert 'id="workspace-table-1-view"' in new


def test_all_workspace_get_pages_preserve_operations_sources_and_state(live_server,monkeypatch):
    base,db=live_server
    original=workspace_ui.render;captured=[]
    def inspect(title,body,path,active='',group='Workspace'):
        new=original(title,body,path,active,group)
        assert operation_surface(body)==operation_surface(new),path
        raw=workspace_ui.Markup(body)
        for node in raw.elements:
            if node.tag in {'td','option','textarea','button','label','pre'}:
                # Original financial cells, raw source data and authority labels
                # remain verbatim; their wrappers may receive presentation IDs.
                assert body[node.start:node.end] in new,(path,node.tag)
        assert 'feature-header' in new or path in {'/','/app'},path
        captured.append(path)
        return new
    monkeypatch.setattr(workspace_ui,'render',inspect)
    before=copy.deepcopy(vars(_load(db)))
    paths={'/app','/workspace/features'}
    for _,rows in ui.GROUPS:
        for url,_,_,children in rows:
            if url!='/docs':paths.add(url)
            paths.update(p for p,_ in children)
    for path in sorted(paths):
        status,body=_get(base,path)
        assert status==200 and '/assets/fluxora-workspace.css' in body,path
        tree=workspace_ui.Markup(body)
        assert len([n for n in tree.elements if n.tag=='h1'])==1,path
        ids=[n.attrs['id'] for n in tree.elements if n.attrs.get('id')]
        assert len(ids)==len(set(ids)),path
        # No form is introduced inside a form by layout transformations.
        assert all(not any(a.tag=='form' for a in n.ancestors()) for n in tree.elements if n.tag=='form'),path
    assert len(captured)==len(paths)
    assert vars(_load(db))==before


def test_feature_directory_links_every_page_and_is_usable_without_javascript(live_server):
    base,_=live_server
    _,body=_get(base,'/workspace/features')
    tree=workspace_ui.Markup(body)
    tiles=[n for n in tree.elements if n.tag=='a' and n.attrs.get('class')=='feature-tile']
    expected={p for _,rows in ui.GROUPS for url,_,_,children in rows for p in [url]+[c[0] for c in children]}
    assert {n.attrs['href'] for n in tiles}==expected
    assert len(tiles)==len(expected)
    assert all('hidden' not in n.attrs for n in tiles)
    assert 'data-feature-filter' in body and 'dedicated workspace pages' in body
    assert all('hidden' not in n.attrs for n in tree.elements if n.attrs.get('class')=='directory-group')


def test_new_stylesheet_is_local_and_public_site_keeps_its_own_styles(live_server):
    base,_=live_server
    with urllib.request.urlopen(base+'/assets/fluxora-workspace.css') as r:
        data=r.read()
        assert r.headers['Content-Type'].startswith('text/css') and r.headers['X-Content-Type-Options']=='nosniff'
    assert data==(Path(workspace_ui.__file__).parent/'static/fluxora-workspace.css').read_bytes()
    assert '/assets/fluxora-workspace.css' not in _get(base,'/welcome')[1]


def test_preview_and_error_screens_keep_exact_owner_checks(live_server,csrf):
    base,db=live_server
    # Existing workflow handler validates and renders its private preview.
    from cashflow_copilot.owner_workflows import review_revision
    ledger=_load(db)
    fields={'csrf':csrf,'operation':'manual_record','revision':review_revision(ledger),'kind':'invoice','id':'ui-new-invoice','currency':'USD','amount':'120.00','date':'2026-10-25','due_date':'2026-10-26','category':'invoice','counterparty':'UI test','invoice_reference':'UI-1','evidence_id':'ev-client-invoice','note':'Checked the synthetic invoice.'}
    # Choose a real seed source; this is fixture evidence, not external truth.
    fields['evidence_id']=next(iter(ledger.evidence))
    status,body=_post(base,'/owner-entries/preview',fields)
    assert status==200 and 'feature-header' in body and 'form-field--check' in body
    assert "name='confirmed' value='yes' required" in body
    assert not _load(db).entry_records
    status,error=_post(base,'/owner-entries/confirm',fields)
    assert status==400 and 'feature-header' in error
    assert not _load(db).entry_records


def test_optional_client_filters_with_offline_dom_doubles():
    node=shutil.which('node')
    if node is None:pytest.skip('Node is optional; required only for the offline JS behaviour check.')
    result=subprocess.run([node,str(Path(__file__).with_name('workspace_filters.js'))],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS:' in result.stdout


@pytest.mark.parametrize('qualified',['qualified','not-qualified'])
def test_copilot_home_has_one_task_focus_and_keeps_model_gate_and_local_reports(live_server,monkeypatch,qualified):
    from cashflow_copilot import task_qualification
    from tests.test_guided_tasks import Parser
    base,db=live_server;adapter=Parser('forecast',{})
    monkeypatch.setattr(agent_web,'live_adapter',lambda:adapter)
    monkeypatch.setattr(task_qualification,'status',lambda *args:qualified)
    before=copy.deepcopy(vars(_load(db)))
    _,body=_get(base,'/agent/tasks')
    tree=workspace_ui.Markup(body)
    assert body.count('<h1>Ask Fluxora</h1>')==1
    assert 'copilot-home' in body and 'copilot-composer' in body and 'agent-layout' not in body
    form=next(n for n in tree.elements if n.tag=='form' and n.attrs.get('action')=='/agent/tasks/start')
    submit=next(n for n in tree.descendants(form,'button'))
    assert ('disabled' in submit.attrs)==(qualified!='qualified')
    reports=[n for n in tree.elements if n.tag=='a' and (n.attrs.get('href') or '').startswith('/agent/tasks/report/')]
    assert len(reports)==8
    assert all(not any(a.tag=='details' for a in n.ancestors()) for n in reports)
    assert 'cannot approve, move money, send real messages, or invent consent or receipts' in body
    assert adapter.calls==0 and vars(_load(db))==before


def test_compact_connection_details_preserve_exact_status_and_settings_stay_discoverable():
    try:
        ui.REQUEST.path='/agent/tasks'
        state='AI not connected · provider: configured-but-unverified · dummy-model'
        body=ui.app_page('Test','<h1>Test</h1>','/agent/tasks',state)
        tree=workspace_ui.Markup(body)
        status=next(n for n in tree.elements if n.tag=='details' and n.attrs.get('class')=='connection-status')
        assert state in unescape(body[status.start:status.end])
        assert 'Model verified' not in body
        assert 'SYNTHETIC DEMO' in body and 'No real money or bank connection' in body
        settings=next(n for n in tree.elements if n.tag=='details' and n.attrs.get('class')=='nav-settings')
        assert any(n.attrs.get('href')=='/setup' for n in tree.descendants(settings,'a'))
        ui.REQUEST.path='/setup'
        selected=ui.app_page('Setup','<h1>Setup</h1>','/setup',state)
        tree=workspace_ui.Markup(selected)
        assert all('open' in n.attrs for n in tree.elements if n.tag=='details' and n.attrs.get('class')=='nav-settings')
    finally:ui.REQUEST.path=''
