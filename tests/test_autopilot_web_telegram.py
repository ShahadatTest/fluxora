import copy,json,re
import pytest
from cashflow_copilot import web,autopilot,task_qualification
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.store import SQLiteStore
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_telegram import bridge,link,send,press,reviews,deliveries,OWNER
from tests.test_web import _load
from tests.test_guided_tasks import Parser


def enable(tg,*,notify=True,strategy='auto-combined'):
    c=link(tg);engine=tg.server.autopilot;engine.clock=tg.clock
    l=_load(c.database);owner=RequestContext('test-owner','owner',l.business_id)
    l.apply_event('evt-delay-001',ctx=owner);s=SQLiteStore(c.database);s.save_ledger(l);s.close()
    p=autopilot.defaults(l);p.update(enabled=True,notify=notify,strategy=strategy,caps={c:10**9 for c in p['caps']})
    engine.save(str(c.database),l,p,ctx=owner,expected='')
    return c,engine,engine.identity(str(c.database))


def test_private_autopilot_queues_once_with_exact_approval_buttons_and_no_auto_approval(bridge):
    tg=bridge;c,engine,key=enable(tg);engine.tick(key);engine.notify(tg)
    first=deliveries(tg);cards=reviews(tg)
    assert any('Autopilot update' in p['text'] for p in first)
    assert len([r for r in cards if r['kind']=='action'])==2
    assert len([r for r in cards if r['kind']=='workflow'])==1
    assert any(b['text']=='View details' for p in first for row in p.get('reply_markup',{}).get('inline_keyboard',[]) for b in row)
    assert all(a.state=='draft' for a in _load(c.database).actions.values())
    engine.notify(tg);assert deliveries(tg)==first and reviews(tg)==cards
    action=next(r for r in cards if r['kind']=='action');press(tg,action['token'])
    l=_load(c.database);chosen=l.actions[json.loads(action['payload'])['id']]
    assert chosen.state=='owner_approved' and chosen.approved_version==chosen.version and not chosen.counterparty_response
    assert not l.receipts and all(not o.observed_payments for o in l.outflows.values())
    before=copy.deepcopy(vars(l));press(tg,action['token'],101);assert vars(_load(c.database))==before


def test_telegram_owner_can_reject_exact_draft_but_foreign_or_stale_buttons_cannot_apply(bridge):
    tg=bridge;c,engine,key=enable(tg);engine.tick(key);engine.notify(tg)
    action=next(r for r in reviews(tg) if r['kind']=='action');before=vars(_load(c.database))
    press(tg,action['token'],owner=OWNER+1);assert vars(_load(c.database))==before
    press(tg,action['token'],101,decision='no')
    assert _load(c.database).actions[json.loads(action['payload'])['id']].state=='cancelled'
    other=next(r for r in reviews(tg) if r['kind']=='action' and r['token']!=action['token'])
    press(tg,other['token'],102)
    assert _load(c.database).actions[json.loads(other['payload'])['id']].state=='draft'


def test_snapshot_requires_its_own_explicit_decision_and_no_cash_or_source_is_invented(bridge):
    tg=bridge;c,engine,key=enable(tg);engine.tick(key);engine.notify(tg)
    job=next(r for r in reviews(tg) if r['kind']=='workflow');before=_load(c.database)
    assert not before.forecast_snapshots
    press(tg,job['token']);after=_load(c.database)
    assert len(after.forecast_snapshots)==1 and after.actual_cash('USD')==before.actual_cash('USD')
    assert not after.receipts and all(a.state=='draft' for a in after.actions.values())


def test_policy_pause_cancels_queued_autopilot_delivery_and_relink_never_leaks_old_workspace(bridge):
    tg=bridge;c,engine,key=enable(tg);engine.tick(key);engine.notify(tg)
    l=_load(c.database);ctx=RequestContext('test-owner','owner',l.business_id);row=engine.policy(str(c.database),l,ctx=ctx)
    p=json.loads(row['policy']);p['enabled']=False;engine.save(str(c.database),l,p,ctx=ctx,expected=row['revision'])
    with tg.db() as db:
        assert not db.execute("SELECT 1 FROM deliveries WHERE status='queued' AND id IN (SELECT delivery_id FROM delivery_keys WHERE key LIKE 'ap:%')").fetchone()
    other=tg.test_clients[1];link(tg,10,other);before=deliveries(tg);engine.notify(tg)
    assert deliveries(tg)==before


def test_disabled_or_missing_notification_policy_does_not_take_bot_busy_lock(bridge,monkeypatch):
    tg=bridge;c=link(tg);engine=tg.server.autopilot
    class BusyLock:
        def __enter__(self):pytest.fail('Disabled Autopilot notification must not lock the bot')
        def __exit__(self,*args):pass
    with monkeypatch.context() as patch:
        patch.setattr(tg,'lock',BusyLock());engine.notify(tg)
    l=_load(c.database);p=autopilot.defaults(l);p.update(enabled=True,notify=False)
    engine.save(str(c.database),l,p,ctx=RequestContext('test-owner','owner',l.business_id),expected='')
    with monkeypatch.context() as patch:
        patch.setattr(tg,'lock',BusyLock());engine.notify(tg)


def test_explicit_telegram_autopilot_on_runs_supported_task_without_preparation_click(bridge):
    tg=bridge;c=link(tg);send(tg,'/autopilot on',2)
    send(tg,'/draft inv-client-001 USD 100 2026-10-22',3)
    assert len(_load(c.database).actions)==1 and next(iter(_load(c.database).actions.values())).state=='draft'
    assert not any(r['kind']=='task' for r in reviews(tg))
    assert any(r['kind']=='action' for r in reviews(tg))
    send(tg,'/autopilot off',4);send(tg,'/draft inv-client-001 USD 100 2026-10-22',5)
    assert len(_load(c.database).actions)==1 and any(r['kind']=='task' for r in reviews(tg))


def test_autopilot_natural_language_uses_one_mock_selector_and_keeps_final_approval(bridge,monkeypatch):
    tg=bridge;c=link(tg);send(tg,'/autopilot on',2)
    tg.adapter=Parser('action_proposal',dict(kind='request_early_payment',target_id='inv-client-001',currency='USD',amount='100',date='2026-10-22'))
    monkeypatch.setattr(task_qualification,'status',lambda *a:'qualified')
    send(tg,'Prepare an early-payment request draft for inv-client-001 USD 100 on 2026-10-22.',3)
    assert tg.adapter.calls==1 and len(_load(c.database).actions)==1
    assert next(iter(_load(c.database).actions.values())).state=='draft'
    assert not any(r['kind']=='task' for r in reviews(tg))


def fields(c,**changes):
    status,body,_=c.request('/autopilot');assert status==200
    revision=re.search(rb"name='revision' value='([^']*)'",body)[1].decode()
    return dict(revision=revision,enabled='yes',interval_seconds='30',digest_seconds='3600',strategy='off',snapshot='yes',cap_USD='0',cap_EUR='0',confirmed='yes',**changes)


def test_public_autopilot_policy_and_history_are_scoped_and_foreign_chat_is_unavailable(public_server):
    app,_=public_server;a=Client(app);b=Client(app);a.request();b.request()
    f=fields(a);assert a.post('/autopilot/policy',**f)[0]==200
    assert a.post('/autopilot/check')[0]==200
    status,body,_=a.request('/autopilot');assert status==200
    ident=re.search(rb'/autopilot/jobs/([a-f0-9]{40})',body)[1].decode()
    assert a.request('/autopilot/jobs/'+ident)[0]==200
    assert b.request('/autopilot/jobs/'+ident)[0]==400
    assert b.post('/autopilot/policy',**dict(fields(b),notify='yes'))[0]==400
    assert b.request('/autopilot/policy',dict(csrf=a.csrf,**f))[0]==403
    assert 'Paused' in b.request('/autopilot')[1].decode()


def test_expired_public_workspace_policy_and_financial_job_index_are_pruned(public_server):
    app,_=public_server;c=Client(app);c.request();assert c.post('/autopilot/policy',**fields(c))[0]==200
    key=app.autopilot.identity(str(c.database));app.autopilot.tick(key)
    with app.visitors._db() as db:db.execute('UPDATE visitors SET expires=0 WHERE digest=?',(key,))
    app.autopilot.tick()
    with app.autopilot.db() as db:
        assert not db.execute('SELECT 1 FROM policies WHERE workspace=?',(key,)).fetchone()
        assert not db.execute('SELECT 1 FROM jobs WHERE workspace=?',(key,)).fetchone()


def test_added_currency_requires_a_new_explicit_limit_and_is_displayed_as_disabled(public_server):
    app,_=public_server;c=Client(app);c.request();assert c.post('/autopilot/policy',**fields(c))[0]==200
    from cashflow_copilot.models import Account
    l=_load(c.database);l.accounts['new-jpy']=Account('new-jpy','JPY',1000,0)
    s=SQLiteStore(c.database);s.save_ledger(l);s.close()
    status,body,_=c.request('/autopilot')
    assert status==200 and b"name='cap_JPY'" in body and b"value='0'" in body
    assert c.post('/autopilot/policy',**dict(fields(c),cap_JPY='1.5'))[0]==400


def test_uncertain_real_transport_stub_send_is_not_retried_after_notification_recovery(bridge):
    tg=bridge;c,engine,key=enable(tg);engine.tick(key);engine.notify(tg)
    tg.api.fail=True;tg.flush()
    with tg.db() as db:before=db.execute("SELECT count(*) FROM deliveries WHERE status='unknown'").fetchone()[0]
    engine.notify(tg)
    with tg.db() as db:assert db.execute("SELECT count(*) FROM deliveries WHERE status='unknown'").fetchone()[0]==before


@pytest.mark.parametrize('change',[{'approved':'yes'},{'confirmed':'no'},{'interval_seconds':'0'},{'cap_USD':'1.001'},{'strategy':'approve-everything'}])
def test_autopilot_http_refuses_missing_confirmation_extra_authority_or_invalid_policy(public_server,change):
    app,_=public_server;c=Client(app);c.request();f=fields(c);f.update(change)
    assert c.post('/autopilot/policy',**f)[0]==400
    assert not app.autopilot.history(str(c.database),_load(c.database),ctx=RequestContext('x','owner','demo-agency'))
