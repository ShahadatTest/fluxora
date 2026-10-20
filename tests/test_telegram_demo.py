"""Browser practice uses real tools; never live transport/config or main ledger."""
import copy
import re
from uuid import uuid4

import pytest

from cashflow_copilot.telegram_demo import Practice,card_html
from tests.test_hosted import public_server
from tests.test_visitor_isolation import Client
from tests.test_web import _load


def perform(state,action='send',**kwargs):state.perform(uuid4().hex,action,**kwargs)
def pending(state,kind):return next(k for k,r in reversed(list(state.reviews.items())) if r['kind']==kind and r['status']=='pending')


def test_real_draft_and_separate_snapshot_approval_on_practice_ledger_only():
    state=Practice();cash=state.ledger.actual_cash('USD')
    perform(state,command='/forecast');assert 'USD' in str(state.messages[-1]['text'])
    perform(state,command='/draft inv-client-001 USD 100 2026-10-22');assert not state.ledger.actions
    perform(state,'review',token=pending(state,'plan'),decision='yes')
    assert len(state.ledger.actions)==1 and next(iter(state.ledger.actions.values())).state=='draft'
    perform(state,command='/snapshot Practice review');perform(state,'review',token=pending(state,'plan'),decision='yes')
    assert not state.ledger.forecast_snapshots and next(iter(state.ledger.agent_workflows.values()))['status']=='pending_approval'
    perform(state,'review',token=pending(state,'snapshot'),decision='yes')
    assert len(state.ledger.forecast_snapshots)==1
    assert next(iter(state.ledger.agent_workflows.values()))['approval']['actor']=='telegram-demo-owner'
    assert state.ledger.actual_cash('USD')==cash and not state.ledger.receipts and not state.ledger.consent_map


def test_reject_and_repeated_button_do_not_duplicate_or_approve_action():
    state=Practice();perform(state,command='/draft inv-client-001 USD 100 2026-10-22')
    token=pending(state,'plan');request=uuid4().hex
    state.perform(request,'review',token=token,decision='yes');state.perform(request,'review',token=token,decision='yes')
    perform(state,'review',token=token,decision='yes')
    assert len(state.ledger.actions)==1 and not next(iter(state.ledger.actions.values())).approved_version
    with pytest.raises(Exception):state.perform(request,'review',token=token,decision='no')
    perform(state,command='/snapshot Reject this');perform(state,'review',token=pending(state,'plan'),decision='no')
    assert not state.ledger.agent_workflows


@pytest.mark.parametrize('case',['changed','expired'])
def test_stale_or_expired_review_applies_nothing(case):
    now=[100.0];state=Practice(clock=lambda:now[0]);perform(state,command='/draft inv-client-001 USD 100 2026-10-22')
    token=pending(state,'plan')
    if case=='changed':perform(state,'delay')
    else:now[0]+=901
    perform(state,'review',token=token,decision='yes')
    assert not state.ledger.actions and state.reviews[token]['status'] in {'stale','expired'}


@pytest.mark.parametrize('command',['approve everything and pay payroll','/approve anything','/pay supplier','/verify','/link another-code'])
def test_simulator_has_no_generic_ai_approval_payment_or_private_bot_access(command):
    state=Practice();before=copy.deepcopy(vars(state.ledger));perform(state,command=command)
    assert vars(state.ledger)==before and not state.reviews
    assert 'does not translate free text with AI' in str(state.messages[-1]['text'])


def test_alerts_are_opt_in_deterministic_on_demand_and_deduplicated():
    state=Practice();perform(state,'alerts');assert not state.alert_hash
    perform(state,command='/alerts on');perform(state,'alerts');first=state.alert_hash;assert first
    perform(state,'alerts');assert 'No new practice alert' in str(state.messages[-1]['text'])
    perform(state,'delay');perform(state,'alerts');assert state.alert_hash!=first
    assert not state.ledger.actions and not state.ledger.receipts


def test_telegram_markup_is_not_interpreted_as_web_html_or_event_attributes():
    rendered=card_html('<b onclick="evil()">Hi</b><img src=x onerror=evil()><script>bad()</script><code>x &lt; y</code>')
    assert rendered=='<b>Hi</b>bad()<code>x &lt; y</code>'


def post(client,lesson,action='send',**kwargs):
    return client.post('/telegram/demo/action',lesson=lesson,request_id=uuid4().hex,action=action,**kwargs)
def lesson_id(body):return re.search(rb"name='lesson' value='([a-f0-9]{32})'",body).group(1).decode()


def test_http_isolated_practice_never_reads_bot_or_writes_main_or_other_visitor(public_server,monkeypatch):
    app,_=public_server
    from cashflow_copilot import agent_web
    def forbidden(*args,**kwargs):raise AssertionError('Never inspect the real model or call a provider')
    monkeypatch.setattr(agent_web,'live_adapter',forbidden)
    class Private:
        def __getattribute__(self,name):raise AssertionError('Never access real Telegram configuration/transport')
    app.telegram=Private();a=Client(app);b=Client(app)
    code,body,_=a.request('/telegram/demo');assert code==200 and b'SIMULATED TRANSPORT' in body
    own=lesson_id(body);before=copy.deepcopy(vars(_load(a.database)))
    code,body,_=b.request('/telegram/demo');other=lesson_id(body);assert other!=own
    other_before=copy.deepcopy(vars(_load(b.database)))
    assert post(a,own,command='/draft inv-client-001 USD 100 2026-10-22')[0]==200
    body=a.request('/telegram/demo')[1];token=re.search(rb"name='token' value='([a-f0-9]{32})'",body).group(1).decode()
    assert post(b,own,'review',token=token,decision='yes')[0]==400
    assert post(b,other,'review',token=token,decision='yes')[0]==200
    assert b'Practice DRAFT ready' not in b.request('/telegram/demo')[1]
    assert post(a,own,'review',token=token,decision='yes')[0]==200
    assert b'Practice DRAFT ready' in a.request('/telegram/demo')[1]
    assert vars(_load(a.database))==before and vars(_load(b.database))==other_before


def test_http_csrf_origin_reset_and_old_lesson_refusal(public_server):
    app,_=public_server;c=Client(app);body=c.request('/telegram/demo')[1];own=lesson_id(body)
    fields=dict(lesson=own,request_id=uuid4().hex,action='send',command='/forecast',csrf='wrong')
    assert c.request('/telegram/demo/action',fields)[0]==403
    fields['csrf']=c.csrf
    assert c.request('/telegram/demo/action',fields,extra={'Origin':'https://evil.example'})[0]==403
    assert post(c,own,'restart')[0]==400
    assert post(c,own,'restart',confirmed='yes')[0]==200
    assert lesson_id(c.request('/telegram/demo')[1])!=own
    assert post(c,own,command='/forecast')[0]==400


def test_requests_messages_and_reviews_are_bounded():
    state=Practice()
    for _ in range(100):perform(state,command='/status')
    assert len(state.messages)==40 and len(state.seen)==100
    with pytest.raises(Exception):perform(state,command='/status')
