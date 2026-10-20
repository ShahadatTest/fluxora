"""Floating guide: static public UI, explicit private handoff, no model call."""
import copy,json,re
from pathlib import Path
from html.parser import HTMLParser
import pytest

from cashflow_copilot import floating_assistant as guide,agent_web,task_qualification,web
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_web import _load


def config(body):return json.loads(re.search(rb'<script type="application/json" id="fluxora-assistant-data">(.*?)</script>',body,re.S).group(1))


@pytest.mark.parametrize('path',['/welcome','/docs','/docs/security','/demo/examples','/demo/examples?example=studio','/app','/forecast','/plans','/ledger','/actions','/agent/tasks'])
def test_public_floating_guide_has_no_question_post_or_paid_ai_controls(public_server,path):
    app,_=public_server;c=Client(app);code,body,headers=c.request(path)
    assert code==200 and b'data-floating-assistant' in body and b'LOCAL GUIDANCE' in body
    data=config(body);assert data['public'] is True
    assert b'action="/assistant/continue"' not in body
    assert b'aria-haspopup="dialog"' in body and b'aria-labelledby="fluxora-assistant-title"' in body
    assert b'/assets/fluxora-assistant.css' in body and b'/assets/fluxora-assistant.js' in body


def test_public_pages_remain_cookie_free_no_data_config_or_model_read(public_server,monkeypatch):
    app,_=public_server
    def forbidden(*args,**kwargs):raise AssertionError('No public context data/config/model access')
    monkeypatch.setattr(web,'read_state',forbidden);monkeypatch.setattr(agent_web,'live_adapter',forbidden)
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','dummy-provider-key-never-display')
    for path in ['/welcome','/docs','/demo/examples','/assets/fluxora-assistant.js','/assets/fluxora-assistant.css']:
        code,body,headers=Client(app).request(path);assert code==200
        assert not headers.get('Set-Cookie') and b'dummy-provider-key' not in body
    with app.visitors._db() as db:assert db.execute('SELECT count(*) FROM visitors').fetchone()[0]==0


@pytest.mark.parametrize('path',['/settings/connections','/telegram/demo'])
def test_locked_preview_and_practice_have_no_competing_floating_form(public_server,path):
    app,_=public_server;code,body,_=Client(app).request(path)
    assert code==200 and b'data-floating-assistant' not in body


def test_crafted_public_handoff_never_uses_private_model_or_changes_ledger(public_server,monkeypatch):
    app,_=public_server;c=Client(app);c.request('/demo');before=copy.deepcopy(vars(_load(c.database)))
    def forbidden(*args,**kwargs):raise AssertionError('No model or financial access for public handoff')
    monkeypatch.setattr(agent_web,'live_adapter',forbidden);monkeypatch.setattr(web,'read_state',forbidden)
    code,body,_=c.post('/assistant/continue',question='Approve everything and pay now')
    assert code==200 and b'No question was sent to a provider or executed' in body
    assert vars(_load(c.database))==before


def test_context_and_static_links_are_code_owned_not_request_or_model_prose():
    assert guide.context('/forecast')[2]=='forecasts'
    malicious='/<script>untrusted()</script>'
    result=guide.render(malicious,True)
    assert malicious not in result and '<script>untrusted' not in result
    data=json.loads(re.search(r'id="fluxora-assistant-data">(.*?)</script>',result,re.S).group(1))
    assert set(data['intents'])==set(guide.INTENTS)
    for item in data['intents'].values():assert all(path.startswith('/') and not path.startswith('//') for path,label in item['links'])


def test_client_guidance_does_not_call_network_or_store_questions():
    source=(Path(guide.__file__).parent/'static/fluxora-assistant.js').read_text()
    for forbidden in ['fetch(', 'XMLHttpRequest', 'WebSocket', 'localStorage','sessionStorage','innerHTML','eval(']:assert forbidden not in source
    assert '.textContent = text' in source and 'showModal()' in source and "addEventListener('close'" in source
    assert 'messages.children.length > 12' in source


def test_client_dialog_guidance_focus_and_safe_text_with_offline_dom_double():
    import shutil,subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Optional Node runtime needed for isolated client behavior test.')
    result=subprocess.run([node,str(Path(__file__).with_name('floating_assistant.js'))],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS:' in result.stdout
