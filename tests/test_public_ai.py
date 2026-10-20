"""Public guided AI uses local HTTP stubs and synthetic visitor ledgers only."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sqlite3
import threading
import time

import pytest

from cashflow_copilot import hosted, web, agent_web, task_qualification
from cashflow_copilot.public_ai import DailyBudget, create
from cashflow_copilot.model_adapter import AdapterError
from cashflow_copilot.agent_tasks import fingerprint
from cashflow_copilot.cli import cmd_seed
from tests.test_hosted import request, DOMAIN
from tests.test_model_adapter import stub, _StubHandler, _completion, CANARY_KEY


def configure(monkeypatch, stub):
    for name, value in {'CASHFLOW_MODEL_API_KEY':CANARY_KEY,
                        'CASHFLOW_MODEL_BASE_URL':stub,
                        'CASHFLOW_MODEL_NAME':'stub-model'}.items():
        monkeypatch.setenv(name,value)


def test_budget_is_atomic_persistent_and_rolls_over_at_utc_midnight(tmp_path):
    now=[1792454399]  # fixed clock, without sleeping
    path=tmp_path/'usage.sqlite'
    budget=DailyBudget(path,3,clock=lambda:now[0])
    def reserve(_):
        try:budget.reserve();return True
        except AdapterError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve,range(12)))==3
    restarted=DailyBudget(path,3,clock=lambda:now[0])
    with pytest.raises(AdapterError,match='daily'):restarted.reserve()
    now[0]+=86400
    restarted.reserve()
    with sqlite3.connect(path) as db:
        assert sorted(r[0] for r in db.execute('SELECT count FROM attempts'))==[1,3]


@pytest.mark.parametrize('limit',[0,-1,1001,True,'1000'])
def test_invalid_budget_refuses_start(tmp_path,limit):
    with pytest.raises(ValueError):DailyBudget(tmp_path/'u.sqlite',limit)


def test_real_http_calls_count_and_restart_starts_unverified(stub,tmp_path,monkeypatch):
    configure(monkeypatch,stub)
    path=tmp_path/'state.sqlite'
    adapter=create(path,1)
    _StubHandler.responses=[(200,_completion('pong'))]
    assert adapter.verify().state=='verified'
    assert len(_StubHandler.requests)==1
    restarted=create(path,1)
    assert restarted.status().state=='configured-but-unverified'
    with pytest.raises(AdapterError,match='daily'):restarted.chat([],[])
    assert len(_StubHandler.requests)==1
    assert CANARY_KEY.encode() not in (tmp_path/'public-ai/usage.sqlite').read_bytes()


def test_http_429_has_no_paid_retry_and_raw_body_never_leaks(stub,tmp_path,monkeypatch):
    configure(monkeypatch,stub)
    adapter=create(tmp_path/'state.sqlite',10)
    _StubHandler.responses=[(429,{'error':{'message':CANARY_KEY}})]*4
    with pytest.raises(AdapterError) as exc:adapter.chat([],[])
    assert CANARY_KEY not in str(exc.value)
    assert len(_StubHandler.requests)==1
    with pytest.raises(AdapterError):adapter.chat([],[])
    assert len(_StubHandler.requests)==1
    with sqlite3.connect(tmp_path/'public-ai/usage.sqlite') as db:
        assert db.execute('SELECT sum(count) FROM attempts').fetchone()[0]==1


def test_qualification_invalidates_when_key_changes_without_storing_key(stub,tmp_path,monkeypatch):
    configure(monkeypatch,stub)
    one=create(tmp_path/'state.sqlite')
    first=fingerprint(one)
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','dummy-new-credential')
    two=create(tmp_path/'state.sqlite')
    assert first!=fingerprint(two)
    assert CANARY_KEY not in first and 'dummy-new' not in fingerprint(two)


def test_opt_in_requires_complete_configuration(monkeypatch):
    monkeypatch.setenv('FLUXORA_DOMAIN',DOMAIN)
    monkeypatch.setenv('FLUXORA_PUBLIC_DEMO','1')
    monkeypatch.setenv('FLUXORA_PUBLIC_AI','1')
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False)
    with pytest.raises(ValueError,match='complete'):hosted.environment_config()
    configure(monkeypatch,'http://127.0.0.1:1')
    config=hosted.environment_config()
    assert config.public_ai and config.public_ai_daily_limit==1000


@pytest.fixture
def public_live(stub,tmp_path,monkeypatch):
    configure(monkeypatch,stub)
    db=str(tmp_path/'state.sqlite');cmd_seed(db)
    monkeypatch.setattr(web,'DB_PATH',db)
    agent_web.mount(web)
    app=hosted.DemoServer(('127.0.0.1',0),hosted.Handler,
        hosted.Config(DOMAIN,'','',db,public_demo=True,public_ai=True))
    thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(task_qualification,'status',lambda path,adapter:'qualified' if path==adapter.qualification_path else 'not-qualified')
    yield (app,db)
    app.shutdown();app.server_close();thread.join(5)


def csrf(body):return re.search(rb"name='csrf' value='([^']+)'",body).group(1).decode()
def token(body):return re.search(rb"name='token' value='([^']+)'",body).group(1).decode()


def test_public_pages_show_live_mode_without_operator_controls_or_secret(public_live):
    status,body,_=request(public_live,'/agent/tasks',auth=False)
    assert status==200 and b'LIVE AI TASK TRANSLATION' in body and b'qualified' in body
    assert b'Run synthetic model check' not in body
    assert b'This public demo is model-free' not in body
    assert b'Continue in Agent' in body and CANARY_KEY.encode() not in body
    assert _StubHandler.requests==[]  # GET does not verify or call


@pytest.mark.parametrize('path',['/agent/ask','/agent/verify','/agent/tasks/qualify'])
def test_public_cannot_trigger_advanced_loop_verify_or_qualification(public_live,path):
    assert request(public_live,path,auth=False,method='POST',origin='https://'+DOMAIN,fields={})[0]==403
    assert _StubHandler.requests==[]


def test_public_guided_request_requires_disclosure_then_exact_review_and_reuses_translation(public_live):
    app,db=public_live
    body=request(public_live,'/agent/tasks',auth=False)[1]
    fields=dict(csrf=csrf(body),question='Show the baseline cash forecast for every currency. Read only.',
                business_id='demo-agency',request_id='a'*32,skill_id='auto')
    status,preview,_=request(public_live,'/agent/tasks/preview',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)
    assert status==200 and b'Review task translation data' in preview and not _StubHandler.requests
    values=dict(csrf=fields['csrf'],token=token(preview),confirmed='yes')
    args=dict(task='forecast',parameters_json='{}',reason='none')
    _StubHandler.responses=[(200,_completion(text='INVENTED USD 999999',tool_calls=[{'id':'x','function':{'name':'select_task','arguments':json.dumps(args)}}]))]
    status,review,_=request(public_live,'/agent/tasks/translate',auth=False,method='POST',origin='https://'+DOMAIN,fields=values)
    assert status==200 and b'Review interpreted task' in review and b'INVENTED' not in review
    assert len(_StubHandler.requests)==1
    assert request(public_live,'/agent/tasks/translate',auth=False,method='POST',origin='https://'+DOMAIN,fields=values)[0]==200
    assert len(_StubHandler.requests)==1
    # A second visitor cannot submit the first visitor's token/CSRF.
    old=app.test_cookie;app.test_cookie=None
    second=request(public_live,'/agent/tasks',auth=False)[1]
    assert request(public_live,'/agent/tasks/translate',auth=False,method='POST',origin='https://'+DOMAIN,fields=dict(values,csrf=csrf(second)))[0]==400
    app.test_cookie=old
    assert CANARY_KEY not in json.dumps(_StubHandler.requests[0]['body'])


@pytest.mark.parametrize('translate_path',['/agent/tasks/translate','/agent/tasks/translate?ignored=1','/agent/tasks/start','/agent/tasks/start?ignored=1'])
def test_locked_connections_remain_static_and_public_ai_rate_limit_blocks_before_network(public_live,monkeypatch,translate_path):
    app,_=public_live
    body=request(public_live,'/settings/connections',auth=False)[1]
    assert b'LOCKED' in body and CANARY_KEY.encode() not in body and b'stub-model' not in body
    allow=app.limits.allow
    monkeypatch.setattr(app.limits,'allow',lambda key,*args:False if key.startswith('public-ai:') else allow(key,*args))
    fields={'csrf':csrf(request(public_live,'/agent/tasks',auth=False)[1])}
    assert request(public_live,translate_path,auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)[0]==429
    assert not _StubHandler.requests


def test_public_one_submit_uses_real_http_stub_once_and_still_isolates_visitors(public_live):
    app,db=public_live
    body=request(public_live,'/agent/tasks',auth=False)[1]
    fields=dict(csrf=csrf(body),question='Show the baseline cash forecast for every currency. Read only.',
                business_id='demo-agency',request_id='c'*32,skill_id='auto')
    args=dict(task='forecast',parameters_json='{}',reason='none')
    _StubHandler.responses=[(200,_completion(text='INVENTED USD 999999',tool_calls=[{'id':'x','function':{'name':'select_task','arguments':json.dumps(args)}}]))]
    status,answer,_=request(public_live,'/agent/tasks/start',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)
    assert len(_StubHandler.requests)==1
    assert status==200 and b'USD 6,000.00' in answer and b'INVENTED' not in answer
    assert request(public_live,'/agent/tasks/start',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)[0]==200
    assert len(_StubHandler.requests)==1
    old=app.test_cookie;app.test_cookie=None
    request(public_live,'/agent/tasks',auth=False)
    assert request(public_live,'/agent/tasks/start',auth=False,method='POST',origin='https://'+DOMAIN,fields=fields)[0]==403
    app.test_cookie=old


@pytest.mark.parametrize('path',['/agent/tasks/start','/agent/tasks/start?ignored=1'])
def test_direct_route_cannot_bypass_global_model_rate_limit(public_live,monkeypatch,path):
    app,_=public_live;allow=app.limits.allow
    monkeypatch.setattr(app.limits,'allow',lambda key,*args:False if key=='model' else allow(key,*args))
    assert request(public_live,path,auth=False,method='POST',origin='https://'+DOMAIN,fields={})[0]==429
    assert not _StubHandler.requests
