import copy
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cashflow_copilot import agent_web, web
from cashflow_copilot.agent_recovery import failure_category, recovery_html, REPORTS
from cashflow_copilot.agent_requests import run_request, request_status, RequestOutcome
from cashflow_copilot.authz import owner_context_for
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.model_adapter import AdapterError, AdapterResult, AdapterStatus, ToolCall
from cashflow_copilot.store import SQLiteStore
from tests.test_web import live_server, _get, _post


class FailingAfterProposal:
    provider_label='SCRIPTED FAILURE REGRESSION — not live'
    def __init__(self, detail):
        self.calls=0
        self.detail=detail
    def chat(self,*args,**kwargs):
        self.calls+=1
        if self.calls==1:
            return AdapterResult('',[ToolCall('job','prepare_workflow',dict(job_id='proposed-before-failure',label='Must be discarded',steps_json=json.dumps([dict(operation='save_forecast',arguments=dict(label='Uncommitted'))])))])
        raise AdapterError('DUMMY-SECRET-IN-EXCEPTION')
    def status(self):
        return AdapterStatus('error',self.provider_label,None,'http://127.0.0.1',detail=self.detail)


@pytest.mark.parametrize('detail,category',[
    ('model provider returned HTTP 429: rate limit reached','rate_limited'),
    ('provider did not complete its response; no partial tool request accepted','incomplete_response'),
    ('model provider returned HTTP 503: request rejected','provider_unavailable'),
    ('model provider call failed: transport error or timeout','transport_or_timeout'),
    ('DUMMY-SECRET-IN-STATUS','failed'),
])
def test_failed_candidate_discarded_safe_category_persists_and_replay_never_retries(tmp_path,detail,category):
    db=tmp_path/'state.sqlite';ledger=Ledger.seed();store=SQLiteStore(db)
    store.save_ledger(ledger);store.close()
    adapter=FailingAfterProposal(detail);ctx=owner_context_for(ledger.business_id);lock=threading.RLock();ident=uuid4().hex
    outcome=run_request(db,ident,'Prepare a snapshot for review','live',adapter,ctx=ctx,lock=lock)
    assert outcome.status=='failed' and adapter.calls==2
    assert outcome.result.disclosure['failure_category']==category
    store=SQLiteStore(db);after=store.load_ledger();store.close()
    assert vars(after)==vars(ledger)
    replay=request_status(db,ident,ctx=ctx,lock=lock)
    assert replay.replayed and replay.result.disclosure['failure_category']==category
    repeated=run_request(db,ident,'Prepare a snapshot for review','live',adapter,ctx=ctx,lock=lock)
    assert repeated.replayed and adapter.calls==2
    html=recovery_html(web,replay)
    assert 'No candidate drafts' in html and 'Continue without a model' in html
    assert 'DUMMY-SECRET' not in html+json.dumps(vars(replay.result))
    assert b'DUMMY-SECRET' not in db.read_bytes()


def test_unknown_category_and_markup_cannot_inject_recovery_text():
    result=SimpleNamespace(disclosure={'failure_category':'<script>DUMMY-KEY</script>'})
    html=recovery_html(web,RequestOutcome('id','failed',result))
    assert '<script>' not in html and 'DUMMY-KEY' not in html


def test_normal_http_failed_result_and_direct_pages_need_no_new_provider_call(live_server,monkeypatch):
    base,db=live_server;agent_web.mount(web)
    adapter=FailingAfterProposal('model provider returned HTTP 503: request rejected')
    # Panel state is disconnected, independent of the regression adapter used by this request.
    monkeypatch.setattr(agent_web,'live_adapter',lambda:adapter)
    ident=uuid4().hex
    status,html=_post(base,'/agent/ask',dict(csrf=web.CSRF_TOKEN,request_id=ident,business_id='demo-agency',question='Prepare a snapshot',provider='live'))
    assert status==200 and 'temporary server failure' in html and 'data-agent-recovery' in html
    assert adapter.calls==2
    for url,_,_ in REPORTS:
        assert _get(base,url)[0]==200
    assert _get(base,'/agent/requests/'+ident)[0]==200 and adapter.calls==2


def test_completed_without_tools_explains_where_to_read_clarification():
    result=SimpleNamespace(disclosure={},tool_calls=[])
    html=recovery_html(web,RequestOutcome('id','completed',result))
    assert 'No tool-backed result' in html and 'unverified' in html
    assert 'No candidate drafts' not in html
