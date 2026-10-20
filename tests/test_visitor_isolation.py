"""Real local HTTP visitor sessions plus expiry, restart and concurrency checks."""
import copy
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import hashlib
from http.cookies import SimpleCookie
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from cashflow_copilot import hosted,web,agent_web
from cashflow_copilot.visitors import Visitors,VisitorError,COOKIE,Workspace
from tests.test_hosted import public_server,DOMAIN
from tests.test_web import _load


class Client:
    def __init__(self,app,cookie=''):self.app=app;self.cookie=cookie;self.csrf=''
    def request(self,path='/agent/tasks',fields=None,extra=None):
        headers={'Host':DOMAIN}
        if self.cookie:headers['Cookie']=self.cookie
        if fields is not None:headers['Origin']='https://'+DOMAIN
        if extra:headers.update(extra)
        data=urllib.parse.urlencode(fields).encode() if fields is not None else None
        req=urllib.request.Request('http://127.0.0.1:'+str(self.app.server_address[1])+path,data=data,headers=headers)
        try:response=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as error:response=error
        with response:
            body=response.read();cookie=response.headers.get('Set-Cookie')
            if cookie:self.cookie=cookie.split(';',1)[0]
            match=re.search(rb"name='csrf' value='([^']+)'",body)
            if match:self.csrf=match.group(1).decode()
            return response.status,body,response.headers
    def post(self,path,**fields):return self.request(path,dict(csrf=self.csrf,**fields))
    @property
    def database(self):
        token=self.cookie.split('=',1)[1]
        return self.app.visitors.root/hashlib.sha256(token.encode()).hexdigest()/'state.sqlite'


def two(server):
    app,_=server;a=Client(app);b=Client(app)
    assert a.request()[0]==b.request()[0]==200
    return a,b


def test_two_visitors_have_distinct_cookie_csrf_ledger_and_no_legacy_data(public_server):
    app,legacy=public_server;original=copy.deepcopy(vars(_load(legacy)))
    # A marker in the old shared ledger must never seed a visitor.
    with sqlite3.connect(legacy) as db:db.execute('CREATE TABLE private_legacy_marker (value TEXT)')
    a,b=two(public_server)
    assert a.cookie!=b.cookie and a.csrf!=b.csrf and a.csrf!=web.CSRF_TOKEN
    assert a.database!=b.database
    assert a.post('/demo/apply-delay')[0]==200
    assert _load(a.database).applied_event_ids and not _load(b.database).applied_event_ids
    assert vars(_load(legacy))==original
    with sqlite3.connect(a.database) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='private_legacy_marker'").fetchall()
    assert b.request('/ledger?workspace='+a.cookie.split('=',1)[1])[0]==200
    assert not _load(b.database).applied_event_ids


def test_secure_cookie_no_raw_token_on_disk_and_session_fixation_refused(public_server):
    app,_=public_server;chosen='a'*43;a=Client(app,COOKIE+'='+chosen)
    status,body,headers=a.request();assert status==200
    assert chosen not in a.cookie
    cookie=headers['Set-Cookie'];parsed=SimpleCookie();parsed.load(cookie);m=parsed[COOKIE]
    assert m['secure'] and m['httponly'] and m['samesite']=='Lax' and m['path']=='/' and not m['domain']
    token=a.cookie.split('=',1)[1]
    assert token.encode() not in body
    assert token.encode() not in app.visitors.registry.read_bytes()
    assert a.request()[2].get('Set-Cookie') is None
    # Static assets do not consume a workspace or issue a bearer cookie.
    status,_,headers=Client(app).request('/assets/fluxora.js')
    assert status==200 and not headers.get('Set-Cookie') and headers['Cache-Control']=='no-store'


def test_missing_expired_or_forged_cookie_post_cannot_create_workspace(public_server):
    app,_=public_server
    for cookie in ['',COOKIE+'='+'b'*43,COOKIE+'=../../state.sqlite']:
        c=Client(app,cookie);assert c.post('/demo/apply-delay')[0]==409
    with sqlite3.connect(app.visitors.registry) as db:assert db.execute('SELECT count(*) FROM visitors').fetchone()[0]==0


def test_cross_session_csrf_and_duplicate_cookie_fail_closed(public_server):
    app,_=public_server;a,b=two(public_server)
    assert b.request('/demo/apply-delay',dict(csrf=a.csrf))[0]==403
    assert b.request(extra={'Cookie':a.cookie+'; '+b.cookie})[0]==400
    assert not _load(a.database).applied_event_ids and not _load(b.database).applied_event_ids


def test_preview_token_cannot_be_consumed_by_another_visitor(public_server):
    a,b=two(public_server)
    # Use real mock-disclosure route; preview never contacts a live model.
    business=_load(a.database).business_id
    status,body,_=a.post('/agent/preview',request_id='d'*32,business_id=business,question='Review cash forecast',provider='mock',data_mode='minimized')
    assert status==200
    token=re.search(rb"name='token' value='([^']+)'",body).group(1).decode()
    assert b.post('/agent/ask',token=token,confirmed='yes')[0]==400
    assert token in next(s.staged for s in a.app.visitors.cache.values() if s.database==str(a.database))
    assert not _load(b.database).actions
    assert a.post('/agent/ask',token=token,confirmed='yes')[0]==200
    status,body,_=b.request('/agent/requests/'+'d'*32)
    assert status==400 and b'No agent request exists' in body
    assert a.request('/agent/requests/'+'d'*32)[0]==200


def test_backups_download_restore_and_reset_are_session_scoped(public_server):
    a,b=two(public_server)
    assert a.post('/backups/create',label='VISITOR-A-ONLY',confirmed='yes')[0]==200
    listing=a.request('/backups')[1];assert b'VISITOR-A-ONLY' in listing
    key=re.search(rb'/backups/([a-f0-9]{32})/download',listing).group(1).decode()
    assert a.request('/backups/'+key+'/download')[0]==200
    assert b'VISITOR-A-ONLY' not in b.request('/backups')[1]
    assert b.request('/backups/'+key+'/download')[0]==400
    assert b.request('/backups/'+key+'/preview')[0]==400
    assert a.post('/demo/apply-delay')[0]==200
    before=copy.deepcopy(vars(_load(a.database)))
    assert b.post('/demo/reset')[0]==200
    assert _load(b.database).demo_checks['reset_done'] is True
    assert vars(_load(a.database))==before


def test_real_request_history_identity_and_replay_are_session_scoped(public_server):
    a,b=two(public_server);ident='e'*32
    fields=dict(request_id=ident,business_id=_load(a.database).business_id,question='Review forecast',provider='mock',data_mode='minimized')
    assert a.post('/agent/ask',**fields)[0]==200
    before=copy.deepcopy(vars(_load(a.database)))
    assert a.post('/agent/ask',**fields)[0]==200 and vars(_load(a.database))==before
    assert a.request('/agent/requests/'+ident)[0]==200
    assert b.request('/agent/requests/'+ident)[0]==400
    # Same request id belongs to a separate SQLite request namespace.
    assert b.post('/agent/ask',**fields)[0]==200


def test_concurrent_requests_and_scope_cleanup_do_not_cross_workspaces(public_server):
    a,b=two(public_server)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda c:c.post('/demo/apply-delay')[0],[a,b]))
    assert results==[200,200]
    assert _load(a.database).applied_event_ids and _load(b.database).applied_event_ids
    assert web.database_path()==web.DB_PATH and web.staged() is web._STAGED
    with pytest.raises(RuntimeError):
        with web.workspace_scope(Workspace('test-only','csrf-only')):
            assert web.database_path()=='test-only'
            raise RuntimeError('end request')
    assert web.database_path()==web.DB_PATH and web.csrf_token()==web.CSRF_TOKEN


def test_http_restart_preserves_cookie_history_and_idempotency(public_server):
    app,_=public_server;a,b=two(public_server);ident='f'*32
    fields=dict(request_id=ident,business_id=_load(a.database).business_id,question='Review forecast',provider='mock',data_mode='minimized')
    assert a.post('/agent/ask',**fields)[0]==200
    before=copy.deepcopy(vars(_load(a.database)));csrf=a.csrf;cookie=a.cookie
    app.shutdown();app.server_close()
    restarted=hosted.DemoServer(('127.0.0.1',0),hosted.Handler,app.config)
    thread=threading.Thread(target=restarted.serve_forever,daemon=True);thread.start()
    try:
        a.app=restarted;b.app=restarted
        assert a.request()[0]==200 and a.cookie==cookie and a.csrf==csrf
        assert a.request('/agent/requests/'+ident)[0]==200
        assert b.request('/agent/requests/'+ident)[0]==400
        assert a.post('/agent/ask',**fields)[0]==200 and vars(_load(a.database))==before
    finally:restarted.shutdown();restarted.server_close();thread.join(5)


def test_preview_limits_remain_bounded_across_visitor_sessions(public_server):
    import time
    app,_=public_server;a,b=two(public_server)
    state=next(s for s in app.visitors.cache.values() if s.database==str(a.database))
    state.staged.clear()
    state.staged.update({str(i):{'_hosted_created':time.monotonic()} for i in range(16)})
    assert a.request()[0]==503 and b.request()[0]==200
    # Pending previews belonging to an inactive browser also expire safely.
    state.staged['0']['_hosted_created']=time.monotonic()-901
    assert b.request()[0]==200 and a.request()[0]==200 and '0' not in state.staged


def headers(cookie=''):
    h=Message()
    if cookie:h['Cookie']=cookie
    return h


def test_registry_restart_retains_session_csrf_and_sqlite_but_not_previews(tmp_path):
    v=Visitors(tmp_path/'visitors')
    with v.lease(headers(),'GET') as (s,token):
        s.staged['only-in-memory']={'kind':'test'};db=s.database;csrf=s.csrf
        with web.workspace_scope(s):web.mutate_state(lambda l:(l.applied_event_ids.add('restart-marker'),None))
    restarted=Visitors(tmp_path/'visitors')
    with restarted.lease(headers(COOKIE+'='+token),'GET') as (s,issued):
        assert issued is None and s.database==db and s.csrf==csrf and s.staged=={}
        assert 'restart-marker' in _load(db).applied_event_ids


def test_expiry_prunes_only_expired_inactive_data_and_rotates_get(tmp_path):
    now=[1000.];v=Visitors(tmp_path/'visitors',ttl=10,clock=lambda:now[0])
    with v.lease(headers(),'GET') as (a,token):
        old=a.database;now[0]=1011
        # A running request protects its directory while another arrives.
        with v.lease(headers(),'GET') as (b,_):assert b.database!=old
        assert __import__('pathlib').Path(old).exists()
    with pytest.raises(VisitorError) as error:
        with v.lease(headers(COOKIE+'='+token),'POST'):pass
    assert error.value.code==409 and not __import__('pathlib').Path(old).exists()
    with v.lease(headers(COOKIE+'='+token),'GET') as (new,new_token):assert new_token!=token and new.database!=old


def test_capacity_does_not_evict_existing_sessions_and_storage_refuses(tmp_path):
    v=Visitors(tmp_path/'visitors',capacity=1)
    with v.lease(headers(),'GET') as (s,token):db=s.database
    with pytest.raises(VisitorError) as error:
        with v.lease(headers(),'GET'):pass
    assert error.value.code==503
    with v.lease(headers(COOKIE+'='+token),'GET') as (s,issued):assert s.database==db and issued is None
    v.workspace_bytes=1
    with pytest.raises(VisitorError) as error:
        with v.lease(headers(COOKIE+'='+token),'POST'):pass
    assert error.value.code==503


def test_opaque_path_guard_never_removes_outside_root(tmp_path):
    v=Visitors(tmp_path/'visitors');outside=tmp_path/'keep';outside.mkdir();(outside/'keep.txt').write_text('keep')
    for key in ['../keep','/keep','z'*64,'a'*63]:
        with pytest.raises(RuntimeError):v._folder(key)
    assert (outside/'keep.txt').read_text()=='keep'
