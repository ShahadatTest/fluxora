"""Actual HTTP tests of the restricted single-owner deployment boundary."""
import base64
import copy
import http.client
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from cashflow_copilot import web,agent_web,hosted
from cashflow_copilot.cli import cmd_seed
from tests.test_web import _load

PASSWORD='dummy-owner-credential-for-regression-only'
DOMAIN='fluxora.sklab.cc'


@pytest.fixture
def server(tmp_path,monkeypatch):
    db=str(tmp_path/'hosted.sqlite');cmd_seed(db);monkeypatch.setattr(web,'DB_PATH',db)
    monkeypatch.setattr(web,'_STAGED',{})
    agent_web.mount(web)
    config=hosted.Config(DOMAIN,'owner',hosted.password_hash(PASSWORD),db,workers=2)
    app=hosted.DemoServer(('127.0.0.1',0),hosted.Handler,config)
    thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
    yield app,db
    app.shutdown();app.server_close();thread.join(5)


def request(server,path='/',*,auth=True,method='GET',origin=None,fields=None,host=DOMAIN,extra=None):
    app,_=server;headers={'Host':host}
    if auth:headers['Authorization']='Basic '+base64.b64encode(('owner:'+PASSWORD).encode()).decode()
    if origin is not None:headers['Origin']=origin
    if extra:headers.update(extra)
    if getattr(app,'test_cookie',None) and 'Cookie' not in headers:headers['Cookie']=app.test_cookie
    data=None if fields is None else urllib.parse.urlencode(fields).encode()
    req=urllib.request.Request('http://127.0.0.1:'+str(app.server_address[1])+path,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=5) as r:
            if r.headers.get('Set-Cookie'):app.test_cookie=r.headers['Set-Cookie'].split(';',1)[0]
            return r.status,r.read(),r.headers
    except urllib.error.HTTPError as r:return r.code,r.read(),r.headers


@pytest.mark.parametrize('path',['/welcome','/app','/agent/tasks','/ledger','/audit/export','/assets/fluxora.js'])
def test_all_data_and_assets_require_owner_authentication(server,path):
    _,db=server;before=copy.deepcopy(vars(_load(db)))
    status,body,headers=request(server,path,auth=False)
    assert status==401 and 'Basic realm=' in headers['WWW-Authenticate']
    assert PASSWORD.encode() not in body and b'CSRF' not in body
    assert vars(_load(db))==before


def test_authenticated_pages_and_headers_without_provider_calls(server):
    status,body,headers=request(server,'/agent/tasks')
    assert status==200 and b'Ask Fluxora' in body and b'SYNTHETIC DEMO' in body
    assert b'Hosted owner demo' in body and b'Protected' in body
    assert headers['Cache-Control']=='no-store' and headers['X-Frame-Options']=='DENY'
    assert headers['Referrer-Policy']=='same-origin'
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert 'Python' not in headers['Server']


@pytest.mark.parametrize('origin',[None,'null','https://attacker.example','http://fluxora.sklab.cc'])
def test_post_requires_exact_https_origin_before_financial_mutation(server,origin):
    _,db=server;before=copy.deepcopy(vars(_load(db)))
    status,_,_=request(server,'/demo/apply-delay',method='POST',origin=origin,fields={'csrf':web.CSRF_TOKEN})
    assert status==403 and vars(_load(db))==before


def test_same_origin_still_needs_csrf_and_replays_do_not_duplicate_events(server):
    _,db=server;origin='https://'+DOMAIN
    assert request(server,'/demo/apply-delay',method='POST',origin=origin,fields={'csrf':'wrong'})[0]==403
    assert request(server,'/demo/apply-delay',method='POST',origin=origin,fields={'csrf':web.CSRF_TOKEN})[0]==200
    after=copy.deepcopy(vars(_load(db)))
    assert request(server,'/demo/apply-delay',method='POST',origin=origin,fields={'csrf':web.CSRF_TOKEN})[0]==200
    assert vars(_load(db))==after


@pytest.mark.parametrize('host',['attacker.example','fluxora.sklab.cc.attacker.example','fluxora.sklab.cc:8080'])
def test_unexpected_host_is_rejected(server,host):assert request(server,host=host)[0]==403


@pytest.mark.parametrize('authorization',['Bearer dummy','Basic invalid-base64','Basic '+base64.b64encode(('é:'+PASSWORD).encode()).decode()])
def test_malformed_or_unicode_auth_fails_closed(server,authorization):
    assert request(server,extra={'Authorization':authorization})[0]==401


@pytest.mark.parametrize('lengths',[['-1'],['abc'],['1','1'],['2097153']])
def test_invalid_or_excess_body_framing_is_rejected(server,lengths):
    app,_=server;conn=http.client.HTTPConnection('127.0.0.1',app.server_address[1],timeout=5)
    conn.putrequest('POST','/demo/apply-delay',skip_host=True);conn.putheader('Host',DOMAIN)
    conn.putheader('Authorization','Basic '+base64.b64encode(('owner:'+PASSWORD).encode()).decode())
    conn.putheader('Origin','https://'+DOMAIN)
    for length in lengths:conn.putheader('Content-Length',length)
    conn.endheaders();response=conn.getresponse()
    assert response.status==(413 if lengths==['2097153'] else 400)
    response.read();conn.close()


def test_rate_limit_and_worker_exhaustion_refuse_before_tools(server,monkeypatch):
    app,db=server;before=copy.deepcopy(vars(_load(db)))
    assert request(server)[0]==200
    original=app.limits.allow
    monkeypatch.setattr(app.limits,'allow',lambda key,*a:key!='model' and original(key,*a))
    assert request(server,'/agent/tasks/translate',method='POST',origin='https://'+DOMAIN,fields={})[0]==429
    app.slots.acquire();app.slots.acquire()
    try:assert request(server)[0]==503
    finally:app.slots.release();app.slots.release()
    assert vars(_load(db))==before


def test_local_health_has_no_business_details(server):
    status,body,_=request(server,'/healthz',auth=False,host='127.0.0.1')
    assert status==200 and json.loads(body)=={'status':'ok'}


def test_hosted_staging_capacity_and_safe_expiry(server,monkeypatch):
    import time
    pending={str(i):{'_hosted_created':time.monotonic()} for i in range(128)}
    monkeypatch.setattr(web,'_STAGED',pending)
    assert request(server,'/agent/tasks')[0]==503
    pending['0']['_hosted_created']=time.monotonic()-901
    assert request(server,'/agent/tasks')[0]==200
    assert '0' not in pending


def test_health_refuses_missing_database(server):
    _,db=server
    from pathlib import Path
    Path(db).unlink()
    assert request(server,'/healthz',auth=False,host='127.0.0.1')[0]==503


@pytest.mark.parametrize('domain',['','localhost','https://example.com','a..example.com','example.com:443','-x.example.com'])
def test_invalid_hosted_configuration_refuses_start(domain):
    with pytest.raises(ValueError):hosted.Config(domain,'owner',hosted.password_hash(PASSWORD),'/tmp/demo.sqlite')


@pytest.fixture
def public_server(tmp_path,monkeypatch):
    db=str(tmp_path/'public.sqlite');cmd_seed(db);monkeypatch.setattr(web,'DB_PATH',db)
    monkeypatch.setattr(web,'_STAGED',{})
    agent_web.mount(web)
    config=hosted.Config(DOMAIN,'','',db,workers=2,public_demo=True)
    app=hosted.DemoServer(('127.0.0.1',0),hosted.Handler,config)
    thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
    yield app,db
    app.shutdown();app.server_close();thread.join(5)


@pytest.mark.parametrize('path',['/welcome','/app','/agent/tasks','/ledger','/audit/export','/assets/fluxora.js'])
def test_public_demo_opens_without_login_or_challenge(public_server,path):
    status,body,headers=request(public_server,path,auth=False)
    assert status==200 and 'WWW-Authenticate' not in headers
    if path=='/agent/tasks':
        assert b'Visitor demo' in body and b'separate from other visitor sessions' in body
        assert b'Hosted owner demo' not in body


def test_public_demo_keeps_host_origin_csrf_and_idempotency(public_server):
    _,db=public_server;before=copy.deepcopy(vars(_load(db)))
    assert request(public_server,auth=False,host='attacker.example')[0]==403
    body=request(public_server,'/agent/tasks',auth=False)[1]
    csrf=re.search(rb"name='csrf' value='([^']+)'",body).group(1).decode()
    fields={'csrf':csrf}
    assert request(public_server,'/demo/apply-delay',auth=False,method='POST',fields=fields)[0]==403
    assert request(public_server,'/demo/apply-delay',auth=False,method='POST',origin='https://'+DOMAIN,fields={'csrf':'wrong'})[0]==403
    assert vars(_load(db))==before
    assert request(public_server,'/demo/apply-delay',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)[0]==200
    after=copy.deepcopy(vars(_load(db)))
    assert request(public_server,'/demo/apply-delay',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)[0]==200
    assert vars(_load(db))==after


def test_public_mode_is_explicit_and_prevents_billable_provider_configuration(monkeypatch):
    monkeypatch.setenv('FLUXORA_DOMAIN',DOMAIN)
    monkeypatch.delenv('FLUXORA_PUBLIC_DEMO',raising=False)
    monkeypatch.delenv('FLUXORA_OWNER_USER',raising=False)
    monkeypatch.delenv('FLUXORA_OWNER_HASH',raising=False)
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False)
    with pytest.raises(ValueError):hosted.environment_config()
    monkeypatch.setenv('FLUXORA_PUBLIC_DEMO','true')
    with pytest.raises(ValueError):hosted.environment_config()
    monkeypatch.setenv('FLUXORA_PUBLIC_DEMO','1')
    assert hosted.environment_config().public_demo
    dummy='dummy-provider-secret-must-not-be-displayed'
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY',dummy)
    with pytest.raises(ValueError) as error:hosted.environment_config()
    assert dummy not in str(error.value)


def test_public_demo_retains_request_rate_limit(public_server,monkeypatch):
    app,db=public_server;before=copy.deepcopy(vars(_load(db)))
    monkeypatch.setattr(app.limits,'allow',lambda key,*a:False)
    status,_,headers=request(public_server,auth=False)
    assert status==429 and headers['Retry-After']=='60' and 'WWW-Authenticate' not in headers
    assert vars(_load(db))==before
