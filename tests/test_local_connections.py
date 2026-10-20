"""Local Web configuration with dummy credentials, DPAPI and local HTTP stub."""
import copy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from cashflow_copilot import local_app,agent_tasks
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.local_connections import Connections
from cashflow_copilot.local_vault import WindowsVault
from tests.test_local_setup import Client
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client as VisitorClient
from tests.test_telegram import Stub,OWNER,send,press,reviews
from tests.test_web import _load

KEY='dummy-api-credential-DO-NOT-USE'
TOKEN='123456:dummy-telegram-token-DO-NOT-USE'


@pytest.fixture
def settings(tmp_path,monkeypatch):
    for name in ['CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME']:monkeypatch.delenv(name,raising=False)
    app=local_app.create_server(tmp_path/'local.sqlite',0)
    thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
    client=Client(app);assert client.request('/settings/connections')[0]==200
    yield app,client
    app.shutdown();app.server_close();thread.join(5)
    if app.telegram:app.telegram.close()


@pytest.fixture
def provider():
    state={'calls':[],'status':200}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state['calls'].append(dict(auth=self.headers.get('Authorization'),request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            value={'choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]}
            if state['status']!=200:value={'error':{'message':'reflected '+KEY}}
            body=json.dumps(value).encode();self.send_response(state['status']);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(server.server_address[1])+'/v1',state
    server.shutdown();server.server_close();thread.join(5)


def add(client,**kw):
    fields=dict(label='Local test profile',provider='custom',base_url='http://127.0.0.1:1234/v1',model='test-model',key=KEY)
    fields.update(kw);return client.post('/settings/connections/profile',**fields)


def test_multiple_profiles_are_masked_not_in_ledger_or_environment(settings,monkeypatch):
    app,c=settings;m=app.local_connections;before=copy.deepcopy(vars(_load(app.workspace_database)))
    assert add(c)[0]==200
    assert add(c,label='Second',provider='openrouter',model='qwen/test',key='dummy-second-secret')[0]==200
    assert len(m.public())==2 and not m.vault.path.exists()
    for path in ['/settings/connections','/app','/agent','/telegram','/audit/export']:
        status,body,_=c.request(path);assert status==200
        assert KEY.encode() not in body and b'dummy-second-secret' not in body
    assert KEY.encode() not in Path(app.workspace_database).read_bytes()
    assert vars(_load(app.workspace_database))==before
    assert 'CASHFLOW_MODEL_API_KEY' not in os.environ
    assert 'key' not in m.public()[0]


def test_real_stub_verify_reload_switch_and_qualification_invalidation(settings,provider):
    app,c=settings;url,state=provider;m=app.local_connections
    assert add(c,base_url=url)[0]==200;ident=m.public()[0]['id']
    assert state['calls']==[]
    assert c.post('/settings/connections/activate',id=ident)[0]==200
    first=m.active_adapter();fingerprint=agent_tasks.fingerprint(first)
    assert first.status().state=='configured-but-unverified'
    assert c.post('/settings/connections/verify',id=ident)[0]==400 and not state['calls']
    assert c.post('/settings/connections/verify',id=ident,confirmed='yes')[0]==200
    assert m.active_adapter().status().state=='verified' and len(state['calls'])==1
    assert state['calls'][0]['auth']=='Bearer '+KEY
    assert b'provider: verified' in c.request('/app')[1]
    assert c.request('/settings/connections')[0]==200 and m.active_adapter() is first
    assert add(c,id=ident,base_url=url,model='changed-model',key='')[0]==200
    assert m.active_adapter().status().state=='configured-but-unverified'
    assert agent_tasks.fingerprint(m.active_adapter())!=fingerprint
    c.post('/settings/connections/activate',id='off');assert m.active_adapter().status().state=='disconnected'
    assert len(state['calls'])==1


def test_provider_401_body_never_returned_persisted_or_echoed(settings,provider):
    app,c=settings;url,state=provider;state['status']=401
    add(c,base_url=url);ident=app.local_connections.public()[0]['id'];c.post('/settings/connections/activate',id=ident)
    assert c.post('/settings/connections/verify',id=ident,confirmed='yes')[0]==200
    for path in ['/settings/connections','/agent','/audit/export']:
        assert KEY.encode() not in c.request(path)[1]
    assert KEY.encode() not in Path(app.workspace_database).read_bytes()
    assert len(state['calls'])==1 and app.local_connections.active_adapter().status().state=='error'


@pytest.mark.parametrize('fields',[{'base_url':'http://evil.example/v1'},{'base_url':'https://u:secret@example.com/v1'},
    {'base_url':'https://example.com/v1?key=abc'},{'base_url':'https://example.com/v1/chat/completions'},
    {'label':KEY},{'model':KEY},{'base_url':'https://example.com/v1','no_key':'yes'},{'key':'bad\r\nvalue'},
    {'provider':'native-anthropic'}])
def test_bad_destinations_or_secret_metadata_are_not_saved(settings,fields):
    app,c=settings;status,body,_=add(c,**fields)
    assert status==400 and KEY.encode() not in body and not app.local_connections.public()


@pytest.mark.skipif(os.name!='nt',reason='Windows current-user DPAPI')
def test_remember_uses_dpapi_restart_is_unverified_no_network(settings,provider):
    app,c=settings;url,state=provider;add(c,base_url=url,remember='yes')
    m=app.local_connections;ident=m.public()[0]['id'];c.post('/settings/connections/activate',id=ident)
    c.post('/settings/connections/verify',id=ident,confirmed='yes');assert len(state['calls'])==1
    ciphertext=m.vault.path.read_bytes();assert KEY.encode() not in ciphertext and b'test-model' not in ciphertext
    restored=Connections(app)
    assert not restored.load_error and restored.data['active']==ident
    assert restored.active_adapter().status().state=='configured-but-unverified' and len(state['calls'])==1
    assert restored.data['profiles'][ident]['key']==KEY
    c.post('/settings/connections/forget-saved',confirmed='yes')
    assert not m.vault.path.exists() and m.active_adapter().is_configured


def test_unsupported_vault_has_no_plaintext_fallback(settings):
    app,c=settings;app.local_connections.vault.available=False
    status,body,_=add(c,remember='yes')
    assert status==400 and not app.local_connections.public() and not app.local_connections.vault.path.exists()


def test_corrupt_vault_does_not_activate_environment_or_overwrite(settings):
    app,c=settings;m=app.local_connections;m.vault.path.write_bytes(b'corrupt saved settings')
    restored=Connections(app);assert restored.load_error and restored.active_adapter().status().state=='disconnected'
    app.local_connections=restored
    assert add(c)[0]==200 and m.vault.path.read_bytes()==b'corrupt saved settings'
    assert add(c,remember='yes')[0]==400
    c.post('/settings/connections/forget-saved',confirmed='yes');assert not m.vault.path.exists()


def test_hosted_cannot_access_configure_or_allocate_local_settings(public_server):
    app,_=public_server;c=VisitorClient(app)
    assert c.request('/settings/connections')[0]==200
    assert c.request('/settings/connections/profile',dict(key=KEY))[0]==403
    with sqlite3.connect(app.visitors.registry) as db:assert db.execute('SELECT count(*) FROM visitors').fetchone()[0]==0


def test_local_settings_requires_host_origin_csrf_and_confirmation(settings):
    app,c=settings
    assert c.request('/settings/connections',extra={'Host':'evil.example'})[0]==403
    assert c.request('/settings/connections/profile',dict(key=KEY),extra={'Origin':'https://evil.example'})[0]==403
    assert c.request('/settings/connections/profile',dict(csrf='wrong',key=KEY))[0]==403
    assert c.request('/settings/connections?key='+KEY)[0]==400
    assert not app.local_connections.public()


def test_telegram_save_test_start_pair_draft_stop_and_restart(settings):
    app,c=settings;m=app.local_connections;api=Stub();m.api_factory=lambda token:api
    assert c.post('/settings/connections/bot-save',token=TOKEN,owner=str(OWNER),name='FluxoraTestBot')[0]==200
    assert not api.calls and app.telegram is None
    body=c.request('/settings/connections')[1];assert TOKEN.encode() not in body and b"value='"+TOKEN.encode() not in body
    assert c.post('/settings/connections/bot-test',confirmed='yes')[0]==200
    assert [name for name,_ in api.calls]==['getMe']
    assert c.post('/settings/connections/bot-start')[0]==400 and app.telegram is None
    assert c.post('/settings/connections/bot-start',confirmed='yes')[0]==200
    assert app.telegram is not None
    status,body,_=c.post('/telegram/pair',confirmed='yes');assert status==200
    code=re.search(rb'/link ([a-f0-9]{32})',body).group(1).decode()
    send(app.telegram,'/link '+code,1000);send(app.telegram,'/draft inv-client-001 USD 100 2026-10-22',1001)
    token=reviews(app.telegram)[0]['token'];press(app.telegram,token,1002)
    assert len(_load(app.workspace_database).actions)==1
    assert c.post('/settings/connections/bot-stop',confirmed='yes')[0]==200 and app.telegram is None
    assert c.post('/settings/connections/bot-forget',confirmed='yes')[0]==200
    assert TOKEN.encode() not in Path(app.workspace_database).read_bytes()


def test_profile_change_invalidates_model_previews_and_stale_adapter(settings):
    app,c=settings;m=app.local_connections;add(c);ident=m.public()[0]['id']
    c.post('/settings/connections/activate',id=ident)
    app.visitors.state.staged['old-preview']={'configuration_id':id(m.active_adapter())}
    old=m.active_adapter();c.post('/settings/connections/activate',id=ident)
    assert not app.visitors.state.staged and m.active_adapter() is not old


def test_busy_bot_blocks_configuration_mutation_without_deadlock(settings,monkeypatch):
    app,c=settings;m=app.local_connections;add(c);ident=m.public()[0]['id']
    # Hold exactly one synthetic in-flight task, without background polling
    # independently acquiring the lock when that task finishes.
    monkeypatch.setattr('cashflow_copilot.local_connections.TelegramBridge.start',lambda self:None)
    m.api_factory=lambda token:Stub()
    m.save_bot(token=TOKEN,owner=str(OWNER),name='FluxoraTestBot')
    m.start_bot();bot=app.telegram
    acquired=threading.Event();release=threading.Event()
    def busy():
        with bot.lock:acquired.set();release.wait(10)
    worker=threading.Thread(target=busy);worker.start();assert acquired.wait(3)
    try:
        assert c.post('/settings/connections/activate',id=ident)[0]==400
        assert c.post('/settings/connections/bot-stop',confirmed='yes')[0]==400
        assert m.data['active']=='environment' and app.telegram is bot and not bot.stop.is_set()
    finally:release.set();worker.join(5)
    assert c.post('/settings/connections/bot-stop',confirmed='yes')[0]==200


def test_bot_configuration_change_revokes_old_link_reviews_and_queue(settings):
    app,c=settings;m=app.local_connections;api=Stub();m.api_factory=lambda token:api
    m.save_bot(token=TOKEN,owner=str(OWNER),name='FluxoraTestBot');m.start_bot()
    status,body,_=c.post('/telegram/pair',confirmed='yes');assert status==200
    code=re.search(rb'/link ([a-f0-9]{32})',body).group(1).decode()
    send(app.telegram,'/link '+code,2000);send(app.telegram,'/draft inv-client-001 USD 100 2026-10-22',2001)
    old=app.telegram;assert old.state(app.workspace_database)['linked'] and reviews(old)[-1]['status']=='pending'
    m.stop_bot()
    with old.db() as db:db.execute("INSERT OR REPLACE INTO settings VALUES ('offset','9000000')")
    m.save_bot(token='654321:another-dummy-token-DO-NOT-USE',owner=str(OWNER),name='FluxoraTestBot')
    with old.db() as db:
        assert db.execute('SELECT count(*) FROM binding').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM reviews WHERE status='pending'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM deliveries WHERE status='queued'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM settings WHERE key='offset'").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM updates').fetchone()[0]==0
    assert not _load(app.workspace_database).actions


@pytest.mark.parametrize('operation',['stop','activate'])
def test_short_bot_housekeeping_finishes_before_connection_change(settings,monkeypatch,operation):
    from cashflow_copilot.telegram_bridge import TelegramBridge
    app,c=settings;m=app.local_connections;m.api_factory=lambda token:Stub()
    assert add(c)[0]==200;ident=m.public()[0]['id']
    # Control the housekeeping section directly; no polling/network timing is
    # needed to reproduce a busy lock between two otherwise idle operations.
    monkeypatch.setattr(TelegramBridge,'start',lambda self:None)
    m.save_bot(token=TOKEN,owner=str(OWNER),name='FluxoraTestBot');m.start_bot();bot=app.telegram
    lock=bot.lock;acquired=threading.Event();release=threading.Event()
    def housekeeping():
        with lock:acquired.set();release.wait(5)
    worker=threading.Thread(target=housekeeping);worker.start();assert acquired.wait(3)
    class FinishingHousekeeping:
        def acquire(self,blocking=True,timeout=-1):
            if blocking:
                assert 0<timeout<=.25
                release.set()
                return lock.acquire(timeout=timeout)
            return lock.acquire(False)
        def release(self):lock.release()
        def __enter__(self):lock.acquire();return self
        def __exit__(self,*args):lock.release()
    bot.lock=FinishingHousekeeping()
    try:
        if operation=='stop':m.stop_bot();assert app.telegram is None and bot.stop.is_set()
        else:m.activate(ident);assert m.data['active']==ident and app.telegram is bot
    finally:
        release.set();worker.join(5);bot.lock=lock
    assert not worker.is_alive()


@pytest.mark.skipif(os.name!='nt',reason='Windows current-user DPAPI')
def test_remembered_bot_does_not_autostart_or_call_telegram(settings):
    app,c=settings;m=app.local_connections;api=Stub();m.api_factory=lambda token:api
    assert c.post('/settings/connections/bot-save',token=TOKEN,owner=str(OWNER),name='FluxoraTestBot',remember='yes')[0]==200
    restored=Connections(app,api_factory=lambda token:api)
    assert restored.data['telegram']['token']==TOKEN and app.telegram is None and not api.calls
    assert TOKEN.encode() not in restored.vault.path.read_bytes()


def test_keyless_loopback_profile_and_delete_active(settings,provider):
    app,c=settings;url,state=provider;m=app.local_connections
    assert add(c,base_url=url,key='',no_key='yes')[0]==200
    ident=m.public()[0]['id'];c.post('/settings/connections/activate',id=ident)
    assert c.post('/settings/connections/verify',id=ident,confirmed='yes')[0]==200
    assert state['calls'][0]['auth']=='Bearer local-no-key'
    assert c.post('/settings/connections/remove',id=ident,confirmed='yes')[0]==200
    assert not m.public() and m.data['active']=='off' and m.active_adapter().status().state=='disconnected'
