import copy
from pathlib import Path
import shutil,subprocess,urllib.request,urllib.error
import pytest
from cashflow_copilot import getting_started,web,agent_web,workspace_ui
from tests.test_web import live_server,csrf,_get,_post,_load
from tests.test_hosted import public_server,request as hosted_request
from tests.test_workspace_ui import operation_surface


@pytest.fixture(autouse=True)
def mounted():agent_web.mount(web)


def test_getting_started_is_public_cookie_free_and_reads_no_business_model_or_bot(public_server,monkeypatch):
    def denied(*a,**kw):raise AssertionError('A navigation guide must not read a business or configuration.')
    monkeypatch.setattr(web,'read_state',denied);monkeypatch.setattr(agent_web,'live_adapter',denied)
    status,body,headers=hosted_request(public_server,'/start',auth=False)
    assert status==200 and 'Set-Cookie' not in headers
    assert b'Five-minute judge tour' in body and b'Explore as a business owner' in body
    assert b'not a competition requirement' in body and b'separate, disposable Atlas practice ledger' in body
    assert b'not proof of task completion' not in body # The hidden rail only mounts inside app pages.
    assert b'<noscript>' in body and b'/app#tour-judge-1' in body
    assert b'name="csrf"' not in body and b"name='csrf'" not in body
    assert hosted_request(public_server,'/start?mode=bad',auth=False)[0]==400


def test_all_guide_destinations_render_without_mutating_main_ledger(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    for mode,guide in getting_started.GUIDES.items():
        assert len(guide['steps'])==5
        for index,step in enumerate(guide['steps']):
            status,html=_get(base,getting_started.target(mode,index))
            assert status==200 and 'data-guided-rail' in html
            assert 'not proof of task completion' in html
            assert 'fluxora-guide.js' in html
    assert vars(_load(db))==before


def test_guide_practice_explicitly_separates_draft_from_snapshot_approval():
    steps=getting_started.GUIDES['judge']['steps']
    assert steps[3]['path']==steps[4]['path']=='/telegram/demo'
    assert 'no live AI' in steps[3]['text']
    assert 'does not approve the earlier DRAFT' in steps[4]['text']
    assert 'not a payment' in steps[4]['check']


def test_locked_preview_does_not_gain_guide_script_or_controls(public_server):
    status,body,_=hosted_request(public_server,'/settings/connections',auth=False)
    assert status==200 and b'data-guided-rail' not in body and b'fluxora-guide.' not in body


def test_accessible_notices_preserve_original_controls_and_financial_cells():
    raw='<h1>Review</h1><p class="err">The exact error.</p><p class="ok">Saved exact record.</p><form method="post"><input name="csrf" value="x"><button name="choice" value="yes">Approve exact terms</button></form><table><tr><td><p class="err">USD 100.00</p></td></tr></table>'
    result=workspace_ui.render('Review',raw,'/actions')
    assert 'class="err" role="alert"' in result and 'class="ok" role="status"' in result
    assert '<td><p class="err">USD 100.00</p></td>' in result
    assert operation_surface(raw)==operation_surface(result)


def test_unexpected_error_never_claims_nothing_saved_or_leaks_raw_exception(live_server,csrf,monkeypatch):
    base,db=live_server
    def fails_after_record(handler,req):
        def mark(ledger):ledger.demo_checks['committed_before_response_error']=True
        web.mutate_state(mark)
        raise RuntimeError('dummy-private-exception-must-not-be-displayed')
    monkeypatch.setitem(web.ROUTES,('POST','/test-response-error'),fails_after_record)
    status,html=_post(base,'/test-response-error',dict(csrf=csrf))
    assert status==500 and 'Completion is not confirmed' in html
    assert 'Nothing was saved' not in html and 'dummy-private-exception' not in html
    assert '/actions' in html and '/records' in html and 'role="alert"' in html
    assert _load(db).demo_checks['committed_before_response_error'] is True


def test_optional_guide_browser_logic_offline_and_local_assets(live_server):
    base,_=live_server
    for name in ['fluxora-guide.css','fluxora-guide.js']:
        with urllib.request.urlopen(base+'/assets/'+name) as response:
            data=response.read();assert response.status==200
        assert data==(Path(getting_started.__file__).parent/'static'/name).read_bytes()
        for forbidden in [b'fetch(',b'XMLHttpRequest',b'WebSocket',b'innerHTML',b'.submit(',b'.click(',b'localStorage']:
            assert forbidden not in data
    node=shutil.which('node')
    if not node:pytest.skip('Optional Node required for the isolated behavior check.')
    result=subprocess.run([node,str(Path(__file__).with_name('getting_started.js'))],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
