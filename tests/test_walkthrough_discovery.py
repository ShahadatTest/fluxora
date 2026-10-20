"""Regressions from an agent-operated browser walkthrough, not human feedback."""
import copy
import re
import threading
from uuid import uuid4

from cashflow_copilot import agent_web,web
from cashflow_copilot.agent_requests import run_request
from cashflow_copilot.agent_workflows import prepare,decide
from cashflow_copilot.attention import attention_items
from cashflow_copilot.authz import RequestContext,owner_context_for
from cashflow_copilot.model_adapter import MockProviderAdapter
from cashflow_copilot.review import daily_review
from cashflow_copilot.store import SQLiteStore
from tests.test_web import live_server,_get


def test_pending_work_reaches_daily_review_and_leaves_after_owner_decision(ledger):
    owner=owner_context_for(ledger.business_id)
    prepare(ledger,'discover-work','Weekly review',[dict(operation='save_forecast',arguments=dict(label='Review'))],
            ctx=RequestContext('agent','agent',ledger.business_id))
    before=copy.deepcopy(vars(ledger))
    report=daily_review(ledger,ctx=owner)
    stage=next(s for s in report['stages'] if s['id']=='drafts')
    pending=[i for i in stage['items'] if i['code']=='workflow_approval_needed']
    assert len(pending)==1 and pending[0]['href']=='/agent/workflows/discover-work'
    assert 'no financial effect' in pending[0]['why'] and vars(ledger)==before
    job=ledger.agent_workflows['discover-work']
    decide(ledger,job['id'],job['review_token'],'reject',ctx=owner)
    assert not any(i['code']=='workflow_approval_needed' for i in attention_items(ledger,ctx=owner))


def test_opening_source_warnings_distinguish_accounts_and_currencies(ledger):
    rows=[i for i in attention_items(ledger,ctx=owner_context_for(ledger.business_id)) if i['code']=='cash_source_review']
    assert rows and len({i['title'] for i in rows})==len(rows)
    assert all(i['currency'] in i['title'] and i['target'] in i['title'] for i in rows)


def test_http_history_reopens_exact_saved_request_and_raw_json_is_collapsed(live_server):
    base,db=live_server;agent_web.mount(web)
    s=SQLiteStore(db);ledger=s.load_ledger();s.close()
    ident=uuid4().hex
    run_request(db,ident,'Prepare a workflow to save a forecast snapshot','mock',MockProviderAdapter(),
                ctx=owner_context_for(ledger.business_id),lock=threading.RLock(),data_mode='minimized')
    status,history=_get(base,'/agent')
    assert status==200 and f"href='/agent/requests/{ident}'" in history
    status,answer=_get(base,'/agent/requests/'+ident)
    assert status==200 and 'replayed' in answer
    assert '<details data-tool-result>' in answer
    assert not re.search(r'<details[^>]*data-tool-result[^>]*\bopen\b',answer)
    assert 'Deterministic answer' in answer and 'pending_approval' in answer
    status,guide=_get(base,'/review')
    assert status==200 and 'Review pending workflow: Weekly cash review' in guide
    assert "href='/agent/workflows'" in guide
