"""Private Telegram integration: local transport stubs, no real outgoing calls."""
import copy
from contextlib import contextmanager
from email.message import Message
import io,json,sqlite3,threading,time,urllib.error
from pathlib import Path

import pytest

from cashflow_copilot import web,hosted,agent_web,agent_tasks,agent_workflows
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.telegram_bridge import TelegramBridge,TelegramConfig,configured
from cashflow_copilot.telegram_api import TelegramAPI,TelegramError
from cashflow_copilot.store import SQLiteStore
from cashflow_copilot.visitors import Visitors
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_guided_tasks import Parser
from tests.test_web import _load

OWNER=123456789


class Stub:
    def __init__(self):self.calls=[];self.fail=False;self.updates=[]
    def call(self,method,payload):
        self.calls.append((method,copy.deepcopy(payload)))
        if self.fail:raise TelegramError('dummy-secret-in-stub-error')
        if method=='getUpdates':return self.updates
        if method=='getMe':return dict(is_bot=True,username='FluxoraTestBot')
        return True


@pytest.fixture
def bridge(public_server):
    app,_=public_server;api=Stub();now=[time.time()]
    tg=TelegramBridge(app,TelegramConfig(OWNER,'FluxoraTestBot'),api,clock=lambda:now[0]);app.telegram=tg
    a=Client(app);b=Client(app);assert a.request()[0]==b.request()[0]==200
    tg.test_clients=(a,b);tg.test_clock=now
    yield tg
    tg.close()


def send(tg,text,ident=1,owner=OWNER,chat=OWNER,kind='private',**other):
    update=dict(update_id=ident,message=dict(date=int(tg.clock()),text=text,**{'from':dict(id=owner,is_bot=False)},chat=dict(id=chat,type=kind),**other))
    tg.handle(update);return update


def press(tg,token,ident=100,decision='yes',owner=OWNER,chat=OWNER):
    update=dict(update_id=ident,callback_query=dict(id='callback-'+str(ident),data=decision+':'+token,
        **{'from':dict(id=owner,is_bot=False)},message=dict(chat=dict(id=chat,type='private'))))
    tg.handle(update);return update


def reviews(tg):
    with tg.db() as db:return [dict(r) for r in db.execute('SELECT * FROM reviews ORDER BY rowid')]


def deliveries(tg):
    with tg.db() as db:return [json.loads(r[0]) for r in db.execute('SELECT payload FROM deliveries ORDER BY id')]


def link(tg,ident=1,client=None):
    a=client or tg.test_clients[0];code=tg.pairing(str(a.database));send(tg,'/link '+code,ident)
    assert tg.state(str(a.database))['linked'];return a


def test_web_pairing_is_own_session_single_use_and_hashed(bridge):
    tg=bridge;a,b=tg.test_clients
    assert a.request('/telegram')[0]==200
    assert a.post('/telegram/pair',confirmed='no')[0]==400
    status,body,_=a.post('/telegram/pair',confirmed='yes');assert status==200
    import re
    code=re.search(rb'/link ([a-f0-9]{32})',body).group(1).decode()
    assert code.encode() not in tg.path.read_bytes()
    send(tg,'/link '+code)
    assert tg.state(str(a.database))['linked'] and not tg.state(str(b.database))['linked']
    send(tg,'/link '+code,2)
    with tg.db() as db:assert db.execute('SELECT status FROM updates WHERE id=2').fetchone()[0]=='refused'
    assert b.post('/telegram/disconnect')[0]==200 and tg.state(str(a.database))['linked']
    assert a.post('/telegram/disconnect')[0]==200 and not tg.state(str(a.database))['linked']


@pytest.mark.parametrize('origin',['null','https://attacker.example','http://fluxora.sklab.cc'])
def test_pairing_rejects_untrusted_origin_even_with_valid_csrf_and_referer(bridge,origin):
    tg=bridge;a,_=tg.test_clients
    status,_,headers=a.request('/telegram')
    assert status==200 and headers['Referrer-Policy']=='same-origin'
    fields=dict(csrf=a.csrf,confirmed='yes')
    assert a.request('/telegram/pair',fields,extra={
        'Origin':origin,'Referer':'https://fluxora.sklab.cc/telegram',
        'Sec-Fetch-Site':'same-origin'})[0]==403
    with tg.db() as db:assert db.execute('SELECT count(*) FROM pairs').fetchone()[0]==0
    assert not tg.api.calls


@pytest.mark.parametrize('owner,chat,kind',[(999,OWNER,'private'),(OWNER,999,'private'),(OWNER,OWNER,'group'),(OWNER,-123,'supergroup')])
def test_wrong_account_or_group_cannot_pair_or_receive_any_reply(bridge,owner,chat,kind):
    tg=bridge;a,_=tg.test_clients;code=tg.pairing(str(a.database))
    send(tg,'/link '+code,owner=owner,chat=chat,kind=kind)
    assert not tg.state(str(a.database))['linked'] and not deliveries(tg)
    assert not tg.api.calls


def test_old_edited_forwarded_and_attachment_messages_are_not_instructions(bridge):
    tg=bridge;link(tg);before=len(deliveries(tg))
    send(tg,'/draft inv-client-001 USD 100 2026-10-22',2,forward_origin={'type':'user'})
    tg.handle(dict(update_id=3,message={'date':int(tg.clock())-901,'text':'/snapshot old','from':{'id':OWNER,'is_bot':False},'chat':{'id':OWNER,'type':'private'}}))
    tg.handle({'update_id':4,'edited_message':{'text':'/snapshot edited'}})
    tg.handle({'update_id':5,'message':[]})
    assert not reviews(tg)
    assert len(deliveries(tg))==before+1


def test_pair_code_expiry_relink_and_disconnect_revoke_old_buttons(bridge):
    tg=bridge;a,b=tg.test_clients;code=tg.pairing(str(a.database));tg.test_clock[0]+=301
    send(tg,'/link '+code);assert not tg.state(str(a.database))['linked']
    link(tg,2);send(tg,'/draft inv-client-001 USD 100 2026-10-22',3);token=reviews(tg)[-1]['token']
    link(tg,4,client=b);press(tg,token)
    assert not _load(a.database).actions and not _load(b.database).actions
    assert reviews(tg)[0]['status']=='revoked'
    send(tg,'/snapshot review',5);token=reviews(tg)[-1]['token'];send(tg,'/unlink',6);press(tg,token,101)
    assert not _load(b.database).agent_workflows


def test_draft_requires_exact_review_then_persists_once_as_draft(bridge):
    tg=bridge;a=link(tg);before=_load(a.database);cash=before.actual_cash('USD')
    send(tg,'/draft inv-client-001 USD 100 2026-10-22',2)
    row=reviews(tg)[-1];assert row['kind']=='task' and row['status']=='pending'
    assert not _load(a.database).actions
    button=deliveries(tg)[-1];assert 'USD' in button['text'] and '100' in button['text']
    assert len(button['reply_markup']['inline_keyboard'][0][0]['callback_data'].encode())<=64
    press(tg,row['token']);press(tg,row['token'],101)
    after=_load(a.database);assert len(after.actions)==1 and after.actual_cash('USD')==cash and not after.receipts
    action=next(iter(after.actions.values()));assert action.state=='draft' and action.counterparty_response is None and action.approved_version is None
    assert not _load(tg.test_clients[1].database).actions
    assert reviews(tg)[0]['status']=='completed'


def test_snapshot_has_separate_preparation_and_owner_approval(bridge):
    tg=bridge;a=link(tg);send(tg,'/snapshot Monday cash review',2)
    press(tg,reviews(tg)[0]['token'])
    current=_load(a.database);assert len(current.agent_workflows)==1 and not current.forecast_snapshots
    assert next(iter(current.agent_workflows.values()))['status']=='pending_approval'
    row=reviews(tg)[-1];assert row['kind']=='workflow'
    card=deliveries(tg)[-1];assert 'Approve & save snapshot' in json.dumps(card) and 'Monday cash review' in card['text']
    assert 'integer minor units' in card['text'] and len(card['text'])<=3800
    press(tg,row['token'],101);press(tg,row['token'],102)
    current=_load(a.database);assert len(current.forecast_snapshots)==1
    assert next(iter(current.agent_workflows.values()))['approval']['actor']=='telegram-owner'
    assert not current.receipts and not current.actions


def test_reject_workflow_rejects_existing_pending_job_without_snapshot(bridge):
    tg=bridge;a=link(tg);send(tg,'/snapshot Reject this',2);press(tg,reviews(tg)[0]['token'])
    press(tg,reviews(tg)[-1]['token'],101,decision='no')
    current=_load(a.database);assert not current.forecast_snapshots
    assert next(iter(current.agent_workflows.values()))['status']=='rejected'


@pytest.mark.parametrize('case',['expired','changed','wrong-account','duplicate-update'])
def test_old_changed_or_unowned_callbacks_never_apply(bridge,case):
    tg=bridge;a=link(tg);send(tg,'/draft inv-client-001 USD 100 2026-10-22',2);token=reviews(tg)[0]['token']
    if case=='expired':tg.test_clock[0]+=901
    elif case=='changed':assert a.post('/demo/apply-delay')[0]==200
    elif case=='wrong-account':press(tg,token,100,owner=999);assert not _load(a.database).actions;return
    else:press(tg,token,2);assert not _load(a.database).actions;return
    press(tg,token);assert not _load(a.database).actions


@pytest.mark.parametrize('command',[
    '/draft inv-client-001 USD 10000 2026-10-22','/draft unknown USD 100 2026-10-22',
    '/draft inv-client-001 EUR 100 2026-10-22','/draft inv-client-001 USD 100 bad-date',
    '/draft inv-client-001 USD 1.001 2026-10-22','/draft inv-client-001 USD -100 2026-10-22',
    '/draft inv-client-001 USD 100','/approve anything','/pay supplier','/exec arbitrary',
])
def test_invalid_or_unsupported_commands_refused_without_any_effect(bridge,command):
    tg=bridge;a=link(tg);before=copy.deepcopy(vars(_load(a.database)));send(tg,command,2)
    assert not reviews(tg) and vars(_load(a.database))==before


def test_reports_are_deterministic_alerts_opt_in_and_restart_deduplicated(bridge):
    tg=bridge;a=link(tg)
    before=len(deliveries(tg));tg.notifications();assert len(deliveries(tg))==before
    send(tg,'/forecast',2);assert 'Deterministic report' in deliveries(tg)[-1]['text']
    assert 'USD' in deliveries(tg)[-1]['text']
    send(tg,'/alerts on',3);tg.notifications();count=len(deliveries(tg));tg.notifications();assert len(deliveries(tg))==count
    assert a.post('/demo/apply-delay')[0]==200;tg.notifications();assert len(deliveries(tg))==count+1
    restarted=TelegramBridge(tg.server,tg.config,tg.api,clock=tg.clock)
    restarted.notifications();assert len(deliveries(tg))==count+1
    send(tg,'/alerts off',4);assert a.post('/demo/reset')[0]==200
    count=len(deliveries(tg));tg.notifications();assert len(deliveries(tg))==count


def test_telegram_state_and_preview_survive_restart_without_reexecution(bridge):
    tg=bridge;a=link(tg);send(tg,'/draft inv-client-001 USD 100 2026-10-22',2);token=reviews(tg)[-1]['token']
    restarted=TelegramBridge(tg.server,tg.config,tg.api,clock=tg.clock);press(restarted,token)
    current=copy.deepcopy(vars(_load(a.database)));again=TelegramBridge(tg.server,tg.config,tg.api,clock=tg.clock)
    press(again,token,101);assert vars(_load(a.database))==current and len(_load(a.database).actions)==1


def test_uncertain_send_not_retried_and_running_review_not_replayed(bridge):
    tg=bridge;link(tg);tg.api.fail=True;tg.flush();assert tg.state(str(tg.test_clients[0].database))['uncertain_deliveries']==1
    tg.flush();assert sum(m=='sendMessage' for m,_ in tg.api.calls)==1
    assert 'dummy-secret' not in tg.last_error
    send(tg,'/snapshot interrupted',2);row=reviews(tg)[-1]
    with tg.db() as db:
        db.execute("UPDATE reviews SET status='running' WHERE token=?",(row['token'],))
        db.execute("UPDATE deliveries SET status='sending' WHERE status='queued'")
    restarted=TelegramBridge(tg.server,tg.config,tg.api,clock=tg.clock);press(restarted,row['token'])
    assert reviews(tg)[-1]['status']=='interrupted' and not _load(tg.test_clients[0].database).agent_workflows


def test_owner_configuration_change_revokes_existing_link_and_queue(bridge):
    tg=bridge;a=link(tg);send(tg,'/snapshot old owner',2)
    changed=TelegramBridge(tg.server,TelegramConfig(999),tg.api)
    assert not changed.state(str(a.database))['linked']
    assert reviews(tg)[0]['status']=='revoked'
    changed.flush();assert not tg.api.calls


def test_visitor_expiry_stops_telegram_without_resurrecting_data(bridge):
    tg=bridge;a=link(tg);digest=a.database.parent.name
    with sqlite3.connect(tg.server.visitors.registry) as db:db.execute('UPDATE visitors SET expires=0 WHERE digest=?',(digest,))
    send(tg,'/snapshot after expiry',2)
    assert not reviews(tg) and not tg.state(str(a.database))['linked']


def test_arbitrary_model_prose_is_ignored_exact_model_plan_requires_review(bridge,monkeypatch):
    tg=bridge;a=link(tg);tg.adapter=Parser('action_proposal',dict(kind='request_early_payment',target_id='inv-client-001',currency='USD',amount='100.00',date='2026-10-22'))
    from cashflow_copilot import task_qualification
    monkeypatch.setattr(task_qualification,'status',lambda *a:'qualified')
    send(tg,'Prepare early payment inv-client-001 USD 100 on 2026-10-22',2)
    assert not _load(a.database).actions and reviews(tg)[0]['status']=='pending'
    assert json.loads(reviews(tg)[0]['payload'])['plan']['parameters']['amount']=='100'
    press(tg,reviews(tg)[0]['token']);assert len(_load(a.database).actions)==1


def test_private_model_needs_explicit_check_and_public_never_sees_adapter(bridge,monkeypatch):
    tg=bridge;link(tg)
    class MustNotCall:
        def chat(self,*a,**kw):raise AssertionError('Private provider must not be called from public UI')
        def configuration_matches_env(self):return True
        def status(self):raise AssertionError('Private status must not reach public UI')
    monkeypatch.setattr(agent_web,'_active_adapter',MustNotCall())
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','dummy-private-model-key')
    assert tg.test_clients[0].request('/agent')[0]==200
    assert tg.test_clients[0].post('/agent/verify')[0]==200
    body=tg.test_clients[0].request('/agent/tasks')[1]
    assert b'dummy-private-model-key' not in body and b'AI not connected' in body
    send(tg,'Please show cash flow',2);assert not reviews(tg)


def test_private_configuration_requires_both_allowlist_and_valid_token(monkeypatch):
    for name in ['FLUXORA_TELEGRAM_ENABLED','FLUXORA_TELEGRAM_BOT_TOKEN','FLUXORA_TELEGRAM_OWNER_ID']:monkeypatch.delenv(name,raising=False)
    assert configured() is None
    monkeypatch.setenv('FLUXORA_TELEGRAM_ENABLED','1');dummy='123456:dummy-telegram-token-DO-NOT-USE'
    monkeypatch.setenv('FLUXORA_TELEGRAM_BOT_TOKEN',dummy)
    with pytest.raises(ValueError):configured()
    monkeypatch.setenv('FLUXORA_TELEGRAM_OWNER_ID',str(OWNER));assert configured()[0].owner_id==OWNER
    assert dummy not in repr(configured()[1])


def test_transport_redacts_url_provider_error_and_bounds_response_bytes():
    token='123456:dummy-telegram-token-DO-NOT-USE'
    def fail(request,**kwargs):raise urllib.error.URLError('provider reflected '+token+' in '+request.full_url)
    api=TelegramAPI(token,opener=fail)
    with pytest.raises(TelegramError) as error:api.call('sendMessage',dict(chat_id=OWNER,text='synthetic'))
    assert token not in str(error.value) and 'api.telegram.org/bot' not in str(error.value)
    seen=[]
    class Response(io.BytesIO):
        def read(self,size=-1):seen.append(size);return super().read(size)
    api=TelegramAPI(token,opener=lambda *a,**k:Response(b'x'*262145))
    with pytest.raises(TelegramError):api.call('getMe',{})
    assert seen==[262145]
    with pytest.raises(TelegramError):api.call('deleteWebhook',{})


def test_http_stub_transport_request_shape_and_poll_offset_idempotency(bridge):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    captures=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captures.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body=json.dumps({'ok':True,'result':{'message_id':1}}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def local(request,**kwargs):
        import urllib.request
        return urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(server.server_address[1]),data=request.data,headers={'Content-Type':'application/json'}),timeout=2)
    try:
        api=TelegramAPI('123456:dummy-telegram-token-DO-NOT-USE',opener=local)
        assert api.call('sendMessage',dict(chat_id=OWNER,text='SYNTHETIC',reply_markup={'inline_keyboard':[[{'text':'Review','callback_data':'yes:'+'a'*24}]]}))=={'message_id':1}
        assert captures[0]['reply_markup']['inline_keyboard'][0][0]['callback_data']=='yes:'+'a'*24
    finally:server.shutdown();server.server_close();thread.join(5)
    tg=bridge;link(tg);u=send(tg,'/draft inv-client-001 USD 100 2026-10-22',2)
    tg.api.updates=[u];tg.poll_once();assert len(reviews(tg))==1
    tg.poll_once();assert len(reviews(tg))==1
    assert [p for m,p in tg.api.calls if m=='getUpdates'][-1]['offset']==3


def test_observation_and_consent_workflow_has_no_telegram_approve_button(bridge):
    tg=bridge;link(tg)
    with tg.workspace() as binding:
        ledger=tg._load()
        job=dict(id='unsafe-for-button',steps=[{'operation':'record_bill_payment','arguments':{}}],requires_observed_confirmation=True,requires_counterparty_confirmation=False)
        tg._workflow_card(binding,ledger,job)
    assert 'reply_markup' not in deliveries(tg)[-1] and 'detailed Web review' in deliveries(tg)[-1]['text']


def test_model_checks_have_explicit_cost_confirmation_and_no_ledger_effect(bridge):
    from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
    tg=bridge;a=link(tg)
    class Model:
        def __init__(self):self.calls=0
        def verify(self):self.calls+=1;return OpenAICompatibleAdapter().status()
    model=Model();tg.adapter=model;before=copy.deepcopy(vars(_load(a.database)))
    send(tg,'/verify',2);assert model.calls==0 and 'may incur charges' in deliveries(tg)[-1]['text']
    press(tg,reviews(tg)[0]['token']);assert model.calls==1 and vars(_load(a.database))==before
    press(tg,reviews(tg)[0]['token'],101);assert model.calls==1


def test_new_web_snapshot_alert_gets_exact_approval_card_once(bridge):
    tg=bridge;a=link(tg);send(tg,'/alerts on',2)
    with tg.workspace():
        web.mutate_state(lambda l:agent_workflows.prepare(l,'web-created','Web snapshot',[dict(operation='save_forecast',arguments=dict(label='Web label'))],ctx=RequestContext('web-agent','agent',l.business_id)))
    tg.notifications();assert len(reviews(tg))==1 and reviews(tg)[0]['kind']=='workflow'
    assert 'Web label' in deliveries(tg)[-1]['text']
    tg.notifications();assert len(reviews(tg))==1
    press(tg,reviews(tg)[0]['token']);assert len(_load(a.database).forecast_snapshots)==1


def test_concurrent_owner_clicks_apply_one_draft(bridge):
    from concurrent.futures import ThreadPoolExecutor
    tg=bridge;a=link(tg);send(tg,'/draft inv-client-001 USD 100 2026-10-22',2);token=reviews(tg)[0]['token']
    with ThreadPoolExecutor(2) as pool:list(pool.map(lambda ident:press(tg,token,ident),[100,101]))
    assert len(_load(a.database).actions)==1 and reviews(tg)[0]['status']=='completed'


def test_large_review_is_not_silently_truncated_or_executed(bridge):
    tg=bridge;a=link(tg);send(tg,'/snapshot '+'x'*161,2)
    assert not reviews(tg) and not _load(a.database).agent_workflows
    with pytest.raises(CashflowError):tg.queue('x'*3801)


def test_configured_private_model_does_not_enable_public_provider(monkeypatch,bridge):
    monkeypatch.setenv('FLUXORA_DOMAIN','fluxora.sklab.cc');monkeypatch.setenv('FLUXORA_PUBLIC_DEMO','1')
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','dummy-private-provider-key')
    monkeypatch.setenv('FLUXORA_TELEGRAM_ENABLED','1');monkeypatch.setenv('FLUXORA_TELEGRAM_OWNER_ID',str(OWNER))
    monkeypatch.setenv('FLUXORA_TELEGRAM_BOT_TOKEN','123456:dummy-telegram-token-DO-NOT-USE')
    monkeypatch.setenv('FLUXORA_TELEGRAM_USE_MODEL','0')
    with pytest.raises(ValueError):hosted.environment_config()
    monkeypatch.setenv('FLUXORA_TELEGRAM_USE_MODEL','1');assert hosted.environment_config().public_demo
    body=bridge.test_clients[0].request('/agent')[1]
    assert b'dummy-private-provider-key' not in body and b'AI not connected' in body


def test_unconfigured_telegram_page_is_honest_and_pairing_fails_without_network(public_server):
    app,_=public_server;client=Client(app)
    assert client.request('/agent/tasks')[0]==200
    status,body,_=client.request('/telegram');assert status==200 and b'Not connected' in body
    assert client.post('/telegram/pair',confirmed='yes')[0]==400
