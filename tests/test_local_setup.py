"""Full local entrypoint: source bootstrap, HTTP boundaries and private bot stub."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from cashflow_copilot import local_app,web
from cashflow_copilot.telegram_bridge import TelegramConfig
from tests.test_telegram import Stub,OWNER,send,press,reviews,deliveries
from tests.test_web import _load


class Client:
    def __init__(self,app):self.app=app;self.csrf=''
    def request(self,path='/app',fields=None,extra=None):
        base='http://127.0.0.1:'+str(self.app.server_address[1]);headers={}
        if fields is not None:headers['Origin']=base
        headers.update(extra or {})
        data=urllib.parse.urlencode(fields).encode() if fields is not None else None
        try:r=urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=headers),timeout=5)
        except urllib.error.HTTPError as error:r=error
        with r:
            body=r.read();match=re.search(rb"name='csrf' value='([^']+)'",body)
            if match:self.csrf=match.group(1).decode()
            return r.status,body,r.headers
    def post(self,path,**fields):return self.request(path,dict(csrf=self.csrf,**fields))


@pytest.fixture
def local(tmp_path):
    database=tmp_path/'path with spaces'/'business.sqlite'
    api=Stub();app=local_app.create_server(database,0,telegram=True,api=api,telegram_config=TelegramConfig(OWNER,'FluxoraTestBot'))
    thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
    yield app,database,api
    app.shutdown();app.server_close();thread.join(5);app.telegram.close()


def test_local_routes_and_assets_without_live_provider_calls(local):
    app,database,api=local;client=Client(app)
    for path in ['/welcome','/app','/forecast','/plans','/ledger','/actions','/agent/tasks','/agent/skills',
        '/agent/workflows','/workspace/features','/entries','/statements','/backups','/docs','/telegram',
        '/demo/examples?example=retail','/assets/fluxora.js','/assets/fluxora.css','/assets/fluxora-mark-v1.png','/healthz']:
        status,body,headers=client.request(path)
        assert status==200 and body and headers.get('Set-Cookie') is None
    assert not api.calls
    assert b'no 24-hour visitor expiry' in client.request('/telegram')[1]
    assert not _load(database).actions and app.server_address[0]=='127.0.0.1'


def test_local_exact_host_origin_csrf_and_replay_are_enforced(local):
    app,database,_=local;client=Client(app);assert client.request('/demo')[0]==200
    before=copy.deepcopy(vars(_load(database)))
    assert client.request(extra={'Host':'attacker.example'})[0]==403
    for origin in ['null','https://attacker.example','http://localhost:'+str(app.server_address[1])]:
        assert client.request('/demo/apply-delay',dict(csrf=client.csrf),extra={'Origin':origin})[0]==403
    assert client.request('/demo/apply-delay',dict(csrf='wrong'))[0]==403
    assert vars(_load(database))==before
    assert client.post('/demo/apply-delay')[0]==200;after=copy.deepcopy(vars(_load(database)))
    assert client.post('/demo/apply-delay')[0]==200 and vars(_load(database))==after


def test_local_bot_uses_exact_same_web_database_and_survives_restart(local):
    app,database,api=local;client=Client(app);tg=app.telegram
    assert client.request('/telegram')[0]==200
    status,body,_=client.post('/telegram/pair',confirmed='yes');assert status==200
    code=re.search(rb'/link ([a-f0-9]{32})',body).group(1).decode();send(tg,'/link '+code)
    assert '24 hours after' not in deliveries(tg)[-1]['text']
    send(tg,'/draft inv-client-001 USD 100 2026-10-22',2);press(tg,reviews(tg)[-1]['token'])
    assert len(_load(database).actions)==1 and next(iter(_load(database).actions.values())).state=='draft'
    send(tg,'/snapshot Local check',3);press(tg,reviews(tg)[-1]['token'],101)
    assert not _load(database).forecast_snapshots
    press(tg,reviews(tg)[-1]['token'],102);assert len(_load(database).forecast_snapshots)==1
    app.shutdown();app.server_close()
    restarted=local_app.create_server(database,0,telegram=True,api=api,telegram_config=tg.config,example='retail')
    thread=threading.Thread(target=restarted.serve_forever,daemon=True);thread.start()
    try:
        assert restarted.telegram.state(str(database))['linked']
        before=copy.deepcopy(vars(_load(database)))
        press(restarted.telegram,reviews(tg)[0]['token'],103)
        assert vars(_load(database))==before and _load(database).business_id=='demo-agency'
        assert b'Linked to this workspace' in Client(restarted).request('/telegram')[1]
    finally:restarted.shutdown();restarted.server_close();thread.join(5);restarted.telegram.close()


def test_local_digest_cannot_select_other_file(local,tmp_path):
    app,database,_=local
    with pytest.raises(ValueError):app.visitors.workspace_digest(tmp_path/'other.sqlite')
    with pytest.raises(Exception):
        with app.visitors.lease_digest('../anything'):pass
    assert app.visitors.database_for_digest(app.visitors.digest)==database.resolve()


def test_local_same_database_second_process_lock_refused_and_released(tmp_path):
    path=tmp_path/'state.sqlite'
    with local_app.RunLock(path):
        code='from cashflow_copilot.local_app import RunLock;from pathlib import Path;import sys\nwith RunLock(Path(sys.argv[1])):print("unexpected acquired")'
        result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=10)
        assert result.returncode!=0 and 'already running' in result.stderr
    with local_app.RunLock(path):pass


def test_offline_bootstrap_creates_clean_runtime_with_no_downloads(tmp_path):
    root=Path(__file__).resolve().parents[1];envdir=tmp_path/'clean runtime'
    result=subprocess.run([sys.executable,'-m','tools.local_setup','--venv-dir',str(envdir)],cwd=root,capture_output=True,text=True,timeout=35)
    assert result.returncode==0 and 'No third-party runtime packages or network downloads' in result.stdout
    executable=envdir/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    check=subprocess.run([str(executable),'-m','tools.local_setup','--check'],cwd=root,capture_output=True,text=True,timeout=15)
    assert check.returncode==0
    report=json.loads(check.stdout);assert report['status']=='ready' and report['model_calls']==report['telegram_calls']==0
    assert not (envdir/'Lib/site-packages/pytest').exists()
