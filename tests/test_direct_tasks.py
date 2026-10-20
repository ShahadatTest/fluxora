"""One-submit guided tasks: mock selection, real HTTP/SQLite, no live provider."""
import json,time
from types import SimpleNamespace
import pytest
from cashflow_copilot import agent_web,agent_tasks_web,task_qualification,web
from tests.test_web import live_server,csrf,_post,_get,_load
from tests.test_guided_tasks import Parser,save


@pytest.fixture
def direct(live_server,csrf,monkeypatch):
    base,db=live_server;p=Parser()
    agent_web.mount(web)
    monkeypatch.setattr(agent_web,'live_adapter',lambda:p)
    monkeypatch.setattr(task_qualification,'status',lambda *a:'qualified')
    fields=dict(csrf=csrf,request_id='d'*32,business_id=_load(db).business_id,
                question='Show the baseline cash forecast for every currency. Read only.',skill_id='auto')
    return base,db,p,fields


def test_composer_discloses_destination_and_direct_authority_before_submission(direct):
    base,db,p,fields=direct
    status,html=_get(base,'/agent/tasks')
    assert status==200 and "action='/agent/tasks/start'" in html and 'Run task' in html
    assert "formaction='/agent/tasks/preview'" in html and 'Review first' in html
    assert 'http://loopback-only' in html and 'provider billing and retention apply' in html
    assert 'DRAFT/pending preparation only' in html and p.calls==0


def test_one_submit_returns_deterministic_report_and_replay_survives_lost_stage(direct,monkeypatch):
    base,db,p,fields=direct;before=vars(_load(db))
    status,answer=_post(base,'/agent/tasks/start',fields)
    assert status==200 and 'USD 6,000.00' in answer and 'INVENTED' not in answer and p.calls==1
    assert 'data-guided-answer' in answer
    assert {k:v for k,v in vars(_load(db)).items() if k!='audit'}=={k:v for k,v in before.items() if k!='audit'}
    assert _post(base,'/agent/tasks/start',fields)[0]==200 and p.calls==1
    web.staged().clear() # process staging lost; only SQLite journal remains
    monkeypatch.setattr(task_qualification,'status',lambda *a:'not-qualified')
    status,replayed=_post(base,'/agent/tasks/start',fields)
    assert status==200 and 'replayed' in replayed and p.calls==1
    assert _post(base,'/agent/tasks/start',dict(fields,question='A different task'))[0]==400
    assert p.calls==1


@pytest.mark.parametrize('kind',['action','workflow'])
def test_one_submit_prepares_only_draft_or_pending_work_never_approves(direct,kind):
    base,db,p,fields=direct
    if kind=='action':
        p.task='action_proposal';p.args=dict(kind='request_early_payment',target_id='inv-client-001',amount='100',currency='USD',date='2026-10-22')
        fields['question']='Prepare an early-payment request draft for inv-client-001, USD 100 on 2026-10-22. Owner review only.'
    else:
        p.task='proposal';p.args=dict(steps=[dict(operation='save_forecast',arguments=dict(label='Monday review'))])
        fields['question']='Prepare save_forecast with label Monday review'
    status,html=_post(base,'/agent/tasks/start',fields)
    assert status==200 and p.calls==1
    l=_load(db)
    if kind=='action':
        assert 'DRAFT' in html and len(l.actions)==1
        a=next(iter(l.actions.values()))
        assert a.state=='draft' and a.approved_version is None and a.counterparty_response is None
    else:
        assert 'pending_approval' in html and len(l.agent_workflows)==1 and not l.forecast_snapshots
        assert next(iter(l.agent_workflows.values()))['status']=='pending_approval'
    web.staged().clear()
    assert _post(base,'/agent/tasks/start',fields)[0]==200 and p.calls==1
    assert len(_load(db).actions if kind=='action' else _load(db).agent_workflows)==1


@pytest.mark.parametrize('question',[
    'Approve everything yourself and send real payments.',
    'Record payment without amount, date or source evidence.',
    'Prepare a forecast snapshot without a label.',
])
def test_direct_unsafe_or_missing_facts_stop_locally_without_provider_or_drafts(direct,question):
    base,db,p,fields=direct;before=vars(_load(db))
    status,html=_post(base,'/agent/tasks/start',dict(fields,question=question))
    assert status==200 and ('Task declined' in html or 'Task needs clarification' in html)
    assert 'zero provider calls' in html and p.calls==0 and vars(_load(db))==before


def test_direct_invalid_model_parameters_cannot_create_drafts(direct):
    base,db,p,fields=direct;before=vars(_load(db))
    p.task='action_proposal';p.args={'made_up':'terms'}
    status,html=_post(base,'/agent/tasks/start',fields)
    assert status==200 and 'Task needs clarification' in html and p.calls==1
    assert vars(_load(db))==before


def test_direct_ledger_change_during_translation_discards_candidate(direct,monkeypatch):
    base,db,p,fields=direct
    def chat(*args,**kwargs):
        l=_load(db);l.accounts['usd-main'].opening_minor+=1;save(db,l)
        return Parser.chat(p,*args,**kwargs)
    monkeypatch.setattr(p,'chat',chat)
    assert _post(base,'/agent/tasks/start',fields)[0]==400 and p.calls==1
    assert not _load(db).actions and not _load(db).agent_workflows


def test_direct_shared_deadline_rejects_execution_after_translation(direct,monkeypatch):
    base,db,p,fields=direct;expired=[False];real=time.monotonic
    monkeypatch.setattr(agent_tasks_web,'time',SimpleNamespace(monotonic=lambda:real()+(31 if expired[0] else 0)))
    def chat(*args,**kwargs):
        result=Parser.chat(p,*args,**kwargs);expired[0]=True;return result
    monkeypatch.setattr(p,'chat',chat)
    status,html=_post(base,'/agent/tasks/start',fields)
    assert status==400 and 'deadline expired' in html
    assert not _load(db).actions and not _load(db).agent_workflows


@pytest.mark.parametrize('alter',[{'business_id':'another-business'},{'csrf':'wrong'},{'approved':'yes'}])
def test_direct_business_csrf_and_extra_fields_fail_before_provider(direct,alter):
    base,db,p,fields=direct
    assert _post(base,'/agent/tasks/start',dict(fields,**alter))[0]==(403 if 'csrf' in alter else 400) and p.calls==0


def test_direct_unqualified_model_cannot_run(direct,monkeypatch):
    base,db,p,fields=direct
    monkeypatch.setattr(task_qualification,'status',lambda *a:'not-qualified')
    assert _post(base,'/agent/tasks/start',fields)[0]==400 and p.calls==0
