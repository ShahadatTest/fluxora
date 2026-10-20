"""Hosted locked settings: no credentials, operation or workspace admission."""
from html.parser import HTMLParser
import sqlite3

import pytest

from cashflow_copilot import web,agent_web
from tests.test_hosted import public_server,server,request
from tests.test_visitor_isolation import Client


class Controls(HTMLParser):
    def __init__(self):super().__init__();self.items=[];self.forms=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'input','button','select','textarea'}:self.items.append((tag,dict(attrs)))
        if tag=='form':self.forms.append(dict(attrs))


def count(app):
    with sqlite3.connect(app.visitors.registry) as db:return db.execute('SELECT count(*) FROM visitors').fetchone()[0]


def test_static_preview_never_reads_financial_or_connection_configuration(public_server,monkeypatch):
    app,_=public_server;key='dummy-private-api-key-never-show';token='123456:dummy-private-token-never-show'
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY',key);monkeypatch.setenv('FLUXORA_TELEGRAM_BOT_TOKEN',token)
    def forbidden(*args,**kwargs):raise AssertionError('Preview must never inspect data/configuration or call providers')
    monkeypatch.setattr(web,'read_state',forbidden);monkeypatch.setattr(agent_web,'live_adapter',forbidden)
    class Private:
        def __getattribute__(self,name):return forbidden()
    app.telegram=Private();app.local_connections=Private()
    client=Client(app)
    for _ in range(2):
        code,body,headers=client.request('/settings/connections')
        assert code==200 and b'DEMO PREVIEW' in body and b'LOCKED' in body
        assert key.encode() not in body and token.encode() not in body and b'provider: verified' not in body
        assert not headers.get('Set-Cookie') and headers['Cache-Control']=='no-store'
        tree=Controls();tree.feed(body.decode())
        assert tree.items and not tree.forms
        assert all('disabled' in attrs for tag,attrs in tree.items)
        assert all(not attrs.get('value') for tag,attrs in tree.items)
        assert len([1 for tag,attrs in tree.items if attrs.get('type')=='password'])==2
    assert count(app)==0


@pytest.mark.parametrize('operation',['','profile','activate','verify','remove','bot-save','bot-test','bot-start','bot-stop','bot-forget','forget-saved'])
def test_manual_post_refused_before_operations_or_admission(public_server,monkeypatch,operation):
    app,_=public_server
    def forbidden(*args,**kwargs):raise AssertionError('No dispatch, data read or model call allowed')
    monkeypatch.setattr(web.Handler,'_handle',forbidden);monkeypatch.setattr(agent_web,'live_adapter',forbidden)
    path='/settings/connections'+('/'+operation if operation else '')
    code,body,headers=Client(app).request(path,dict(key='dummy-submitted-key',token='dummy-submitted-token',csrf='anything'))
    assert code==403 and b'locked' in body and b'dummy-submitted' not in body
    assert not headers.get('Set-Cookie') and count(app)==0


def test_preview_remains_available_at_capacity_without_renewing_session(public_server):
    app,_=public_server;app.visitors.capacity=1;c=Client(app)
    assert c.request('/app')[0]==200;cookie=c.cookie
    assert b'/settings/connections' in c.request('/app')[1]
    for client in [c,Client(app)]:
        code,body,headers=client.request('/settings/connections')
        assert code==200 and not headers.get('Set-Cookie')
    assert count(app)==1 and c.cookie==cookie


@pytest.mark.parametrize('path',['/settings/connections?key=dummy-query-secret','/settings/connections?show=1','/settings/connections/profile','/settings/connections/export'])
def test_reveal_queries_and_alternate_paths_are_not_previews(public_server,path):
    app,_=public_server;code,body,headers=Client(app).request(path)
    assert code in {400,404} and b'dummy-query-secret' not in body
    assert not headers.get('Set-Cookie') and count(app)==0


def test_protected_hosted_mode_keeps_auth_and_server_side_lock(server):
    assert request(server,'/settings/connections',auth=False)[0]==401
    assert request(server,'/settings/connections')[0]==200
    assert request(server,'/settings/connections/profile',method='POST',origin='https://fluxora.sklab.cc',fields={'csrf':web.CSRF_TOKEN})[0]==403
