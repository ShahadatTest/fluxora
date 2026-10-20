"""Public artwork never replaces financial results or installs app effects."""
import copy
from pathlib import Path
import shutil
import subprocess
import urllib.request
import pytest
from cashflow_copilot import site_motion
from tests.test_web import live_server, _get, _load
from tests.test_hosted import public_server, request as hosted_request


@pytest.fixture(autouse=True)
def mounted():
    from cashflow_copilot import web,agent_web
    agent_web.mount(web)


def test_public_artwork_is_labelled_and_existing_financial_preview_stays_authoritative(live_server):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)))
    _,baseline=_get(base,'/welcome')
    _,delay=_get(base,'/welcome?example=delay')
    for html in [baseline,delay]:
        assert 'data-depth-scene' in html and 'Illustrative artwork · no financial data.' in html
        assert '<div class="flow-art" aria-hidden="true">' in html
        assert html.count('<h1>')==1 and 'USD 10,000.00' in html
        assert 'Inspect the computed daily data' in html and 'Expected collections remain conditional' in html
        assert 'data-motion-toggle' in html and 'aria-pressed="false" hidden' in html
        assert '/assets/fluxora-motion.css' in html and '/assets/fluxora-motion.js' in html
    assert 'Seven-day collection delay' in delay and 'Current recorded timing' in baseline
    assert vars(_load(db))==before


@pytest.mark.parametrize('path',['/app','/forecast','/agent/tasks','/ledger','/actions','/telegram/demo'])
def test_motion_never_mounts_inside_financial_or_approval_pages(live_server,path):
    _,html=_get(live_server[0],path)
    assert 'fluxora-motion.js' not in html and 'fluxora-motion.css' not in html
    assert 'data-depth-scene' not in html and 'data-motion-toggle' not in html


def test_locked_hosted_preview_does_not_load_motion_or_decorative_controls(public_server):
    code,html,_=hosted_request(public_server,'/settings/connections',auth=False)
    assert code==200 and b'DEMO PREVIEW' in html
    assert b'fluxora-motion.' not in html and b'data-motion-toggle' not in html


def test_local_motion_assets_are_served_and_do_not_need_network_or_financial_access(live_server):
    base,_=live_server
    for name,mime in [('fluxora-motion.css','text/css'),('fluxora-motion.js','text/javascript')]:
        with urllib.request.urlopen(base+'/assets/'+name) as response:
            source=response.read();assert response.status==200 and response.headers['Content-Type'].startswith(mime)
        assert source==(Path(site_motion.__file__).parent/'static'/name).read_bytes()
        for forbidden in [b'https://',b'http://',b'@import',b'fetch(',b'XMLHttpRequest',b'WebSocket',b'innerHTML',b'eval(']:
            assert forbidden not in source
    js=(Path(site_motion.__file__).parent/'static/fluxora-motion.js').read_text()
    assert 'setInterval' not in js and 'setTimeout' not in js and 'querySelectorAll(\'form' not in js
    assert "getItem('fluxora.motion')" in js and "setItem('fluxora.motion', preference)" in js


def test_motion_preferences_and_event_cleanup_with_offline_dom_doubles():
    node=shutil.which('node')
    if not node:pytest.skip('Optional Node runtime needed for offline motion behavior checks.')
    result=subprocess.run([node,str(Path(__file__).with_name('site_motion.js'))],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS:' in result.stdout
