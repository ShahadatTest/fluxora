"""Approval-gated workflow boundaries, atomic execution and actual HTTP UI.

All ledgers and providers here are synthetic/mock; no live model calls.
"""
import copy
import json
import re
from datetime import date

import pytest

from cashflow_copilot import agent, agent_web, web
from cashflow_copilot.agent_workflows import prepare, decide, catalog, revision
from cashflow_copilot.agent_workspace import read
from cashflow_copilot.authz import RequestContext, owner_context_for
from cashflow_copilot.errors import AuthorizationError, CashflowError, IdempotencyConflict
from cashflow_copilot.model_adapter import MockProviderAdapter, AdapterResult, ToolCall
from cashflow_copilot.store import SQLiteStore
from cashflow_copilot.agent_requests import run_request
from tests.test_web import live_server, _post, _get, _load


def ctx(ledger,role='agent',business=None):return RequestContext(role,role,business or ledger.business_id)
def step(name,**args):return dict(operation=name,arguments=args)
def snapshot(ledger):return {k:copy.deepcopy(v) for k,v in vars(ledger).items() if k not in {'agent_workflows','audit'}}
def stage(ledger,steps=None,ident='job-one'):
    prepare(ledger,ident,'Review local work',steps or [step('save_forecast',label='Review')],ctx=ctx(ledger))
    return ledger.agent_workflows[ident]
def approve(ledger,job,**kwargs):return decide(ledger,job['id'],job['review_token'],'approve',ctx=ctx(ledger,'owner'),**kwargs)


def test_preparation_only_simulates_and_owner_approval_runs_the_whole_batch(ledger):
    before=snapshot(ledger)
    job=stage(ledger,[step('save_forecast',label='Before change'),step('confirm_collection_date',
        invoice_id='inv-client-001',expected_date='2026-10-27',evidence_id='ev-delay-message',reason='Synthetic reviewed delay')])
    assert snapshot(ledger)==before and not ledger.forecast_snapshots
    assert job['status']=='pending_approval' and job['approval'] is None
    assert job['preview']['forecast_after']['USD']['minimum_minor']==-50000
    result=approve(ledger,job)
    assert result['status']=='executed' and len(ledger.forecast_snapshots)==1
    assert ledger.receivables['inv-client-001'].expected_date==date(2026,10,27)
    assert ledger.agent_workflows[job['id']]['approval']['actor']=='owner'
    assert ledger.agent_workflows[job['id']]['execution']['results']


@pytest.mark.parametrize('role',['agent','viewer'])
def test_model_or_viewer_cannot_approve_reject_or_execute_even_on_retry(ledger,role):
    job=stage(ledger)
    for decision in ['approve','reject']:
        with pytest.raises(AuthorizationError):decide(ledger,job['id'],job['review_token'],decision,ctx=ctx(ledger,role))
    approve(ledger,job)
    with pytest.raises(AuthorizationError):decide(ledger,job['id'],job['review_token'],'approve',ctx=ctx(ledger,role))


def test_viewer_and_other_business_cannot_prepare(ledger):
    for context in [ctx(ledger,'viewer'),ctx(ledger,business='unrelated')]:
        with pytest.raises(AuthorizationError):prepare(ledger,'job-one','Review',[step('save_forecast',label='Test')],ctx=context)


def test_exact_approval_replay_survives_sqlite_restart_without_duplicate_effect(ledger,tmp_path):
    job=stage(ledger);db=tmp_path/'state.sqlite';s=SQLiteStore(db);s.save_ledger(ledger);s.close()
    s=SQLiteStore(db);loaded=s.load_ledger();s.close();approve(loaded,job)
    s=SQLiteStore(db);s.save_ledger(loaded);s.close();s=SQLiteStore(db);again=s.load_ledger();s.close()
    before=snapshot(again);assert approve(again,job)['duplicate']
    assert snapshot(again)==before and len(again.forecast_snapshots)==1


def test_stale_financial_or_action_terms_never_inherit_old_approval(ledger):
    job=stage(ledger);ledger.accounts['usd-main'].opening_minor+=1;before=copy.deepcopy(vars(ledger))
    with pytest.raises(CashflowError,match='changed'):approve(ledger,job)
    assert vars(ledger)==before


def test_changing_proposed_steps_invalidates_review_token(ledger):
    job=stage(ledger);job['steps'][0]['arguments']['label']='Other terms'
    with pytest.raises(CashflowError,match='terms changed'):approve(ledger,job)
    assert not ledger.forecast_snapshots


def test_batch_failure_during_execution_rolls_back_every_effect(ledger,monkeypatch):
    from cashflow_copilot import agent_workflows as wf
    job=stage(ledger,[step('save_forecast',label='First'),step('save_forecast',label='Second')])
    original=wf._registry;count=0
    def fail_after_first(ledger,label,revision,*,ctx):
        nonlocal count;count+=1
        if count==2:raise CashflowError('Synthetic second-step failure')
        from cashflow_copilot.accounting import save_forecast
        return save_forecast(ledger,label,revision,ctx=ctx)
    def registry():
        r=original();r['save_forecast']=fail_after_first;return r
    monkeypatch.setattr(wf,'_registry',registry);before=copy.deepcopy(vars(ledger))
    with pytest.raises(CashflowError,match='second-step'):approve(ledger,job)
    assert vars(ledger)==before and not ledger.forecast_snapshots


@pytest.mark.parametrize('operation',['approve_action','execute_action','pay','grant_consent','__import__','setup_business','attach_evidence'])
def test_unregistered_or_privileged_operation_is_not_callable(ledger,operation):
    with pytest.raises(CashflowError):stage(ledger,[step(operation)])
    assert not ledger.agent_workflows


@pytest.mark.parametrize('field,value',[('ctx',{}),('actor','owner'),('revision','old'),('acknowledge_duplicates',True)])
def test_model_cannot_supply_executor_authority_or_owner_acknowledgement(ledger,field,value):
    with pytest.raises(CashflowError):stage(ledger,[step('save_forecast',label='Test',**{field:value})])


def test_observed_cash_needs_separate_factual_confirmation_not_approval_alone(ledger):
    job=stage(ledger,[step('record_cash_movement',movement_id='capital-one',classification='owner_capital',
        currency='USD',amount=10000,day='2026-10-16',evidence_id='ev-client-invoice',note='Synthetic source-backed capital observation')])
    assert job['requires_observed_confirmation'] and ledger.actual_cash('USD')==1000000
    with pytest.raises(CashflowError,match='Separately confirm'):approve(ledger,job)
    approve(ledger,job,observed_confirmed=True)
    assert ledger.actual_cash('USD')==1010000 and ledger.agent_workflows[job['id']]['approval']['observed_facts_confirmed']


def test_supplier_acceptance_needs_a_separate_owner_source_confirmation(ledger):
    steps=[step('create_instalments',proposal_id='supplier-plan',bill_id='supplier-001',
        schedule=[dict(date='2026-10-26',amount_minor=ledger.outflows['supplier-001'].remaining_minor)],evidence_id='ev-supplier-invoice',note='Synthetic agreed terms')]
    steps += [step('advance_instalments',proposal_id='supplier-plan',step=s,evidence_id='ev-supplier-invoice',note='Synthetic separately reviewed external acceptance') for s in ['approve','accept','apply']]
    job=stage(ledger,steps)
    assert job['requires_counterparty_confirmation'] and not ledger.instalment_proposals
    with pytest.raises(CashflowError,match='counterparty'):approve(ledger,job)
    approve(ledger,job,counterparty_confirmed=True)
    assert ledger.instalment_proposals['supplier-plan']['state']=='applied'
    assert ledger.instalment_proposals['supplier-plan']['acceptance_evidence_id'] in ledger.evidence


def test_sandbox_send_requires_owner_review_and_keeps_cash_receipt_and_consent_separate(ledger):
    agent.execute_tool(ctx(ledger),ledger,'draft_action',dict(action_id='draft-send',kind='request_early_payment',
        receivable_id='inv-client-001',amount_minor=10000,date='2026-10-18'))
    job=stage(ledger,[step('sandbox_send_action',action_id='draft-send')]);before=ledger.actual_cash('USD')
    assert ledger.actions['draft-send'].state=='draft'
    approve(ledger,job)
    assert ledger.actions['draft-send'].state=='sent' and ledger.actions['draft-send'].approved_version==1
    assert ledger.actual_cash('USD')==before and not ledger.receipts and not ledger.consent_map


def test_prepare_retry_is_idempotent_but_conflicting_id_is_rejected(ledger):
    job=stage(ledger)
    assert prepare(ledger,job['id'],job['label'],job['steps'],ctx=ctx(ledger))['duplicate']
    with pytest.raises(IdempotencyConflict):prepare(ledger,job['id'],'Different label',job['steps'],ctx=ctx(ledger))


def test_model_request_commits_pending_workflow_and_saved_history_together(live_server):
    _,db=live_server;before=_load(db)
    result=run_request(db,'a'*32,'Prepare a workflow to save a forecast snapshot','mock',MockProviderAdapter(),
        ctx=owner_context_for(before.business_id),lock=web.LOCK,data_mode='minimized')
    assert result.status=='completed'
    after=_load(db);assert after.agent_workflows['mock-weekly-review']['status']=='pending_approval'
    assert not after.forecast_snapshots and snapshot(after)==snapshot(before)


def test_partial_model_failure_discards_unapproved_workflow_candidate(live_server):
    _,db=live_server;before=_load(db)
    class FailsAfterProposal:
        provider_label='scripted-test, no live model'
        def __init__(self):self.called=False
        def chat(self,messages,tools,*,deadline=None):
            if self.called:raise CashflowError('Synthetic provider failure')
            self.called=True
            return AdapterResult(tool_calls=[ToolCall('job','prepare_workflow',dict(job_id='discarded-job',label='Review',
                steps_json=json.dumps([step('save_forecast',label='Unfinished')])) )])
    result=run_request(db,'b'*32,'Prepare unfinished work','live',FailsAfterProposal(),ctx=owner_context_for(before.business_id),lock=web.LOCK)
    assert result.status=='failed' and not _load(db).agent_workflows


def test_normal_ui_preview_approve_execute_refresh_with_csrf_and_no_financial_effect(live_server):
    base,db=live_server;agent_web.mount(web)
    status,_=_get(base,'/agent/workflows');assert status==200
    fields=dict(csrf=web.CSRF_TOKEN,request_id='c'*32,business_id='demo-agency',
        question='Prepare a workflow to save a forecast snapshot',provider='mock',data_mode='minimized')
    status,page=_post(base,'/agent/preview',fields);assert status==200
    token=re.search(r"name='token' value='([^']+)'",page).group(1)
    assert _post(base,'/agent/ask',dict(csrf=web.CSRF_TOKEN,token=token,confirmed='yes'))[0]==200
    ledger=_load(db);job=ledger.agent_workflows['mock-weekly-review'];assert not ledger.forecast_snapshots
    route='/agent/workflows/'+job['id']
    status,page=_get(base,route);assert status==200 and 'Approve &amp; run' in page
    approve_fields=dict(csrf=web.CSRF_TOKEN,business_id=ledger.business_id,review_token=job['review_token'],decision='approve',review_confirmed='yes')
    assert _post(base,route+'/decide',{**approve_fields,'csrf':'wrong'})[0]==403
    assert _post(base,route+'/decide',approve_fields)[0]==200
    assert _post(base,route+'/decide',approve_fields)[0]==200
    ledger=_load(db);assert len(ledger.forecast_snapshots)==1 and ledger.agent_workflows[job['id']]['status']=='executed'


@pytest.mark.parametrize('view',['weekly_cashflow','account_funding','decision_brief','collections','data_freshness','promises','search','calendar','loans','attachments','cash_accounts','duplicate_candidates','catalog','queue'])
def test_extended_workspace_views_are_read_only_and_business_scoped(ledger,view):
    before=copy.deepcopy(vars(ledger));result=read(ledger,view,{},ctx=ctx(ledger))
    json.dumps(result);assert vars(ledger)==before
    with pytest.raises(AuthorizationError):read(ledger,view,{},ctx=ctx(ledger,business='other'))


def test_catalog_has_real_structured_examples_and_no_approval_function():
    c=catalog();assert c['count']==55 and not any(x['operation']=='approve_action' for x in c['operations'])
    assert catalog('create_record')['operations'][0]['structured_fields']['record']['evidence_id']=='existing source ID'


def test_protected_payroll_cannot_be_rescheduled_through_approved_batch(ledger):
    before=snapshot(ledger)
    with pytest.raises(CashflowError,match='payroll'):
        stage(ledger,[step('create_instalments',proposal_id='payroll-workaround',bill_id='payroll-001',
            schedule=[dict(date='2026-11-10',amount_minor=ledger.outflows['payroll-001'].remaining_minor)],
            evidence_id='ev-payroll',note='Synthetic attempted date change')])
    assert snapshot(ledger)==before and not ledger.agent_workflows


def test_deadline_after_model_prepares_workflow_never_saves_candidate(live_server):
    import time
    _,db=live_server;before=_load(db)
    class Late:
        provider_label='scripted-late; no live model'
        def __init__(self):self.called=False
        def chat(self,messages,tools,*,deadline=None):
            if self.called:
                time.sleep(.08);return AdapterResult(text='Late answer')
            self.called=True
            return AdapterResult(tool_calls=[ToolCall('proposal','prepare_workflow',dict(job_id='late-job',label='Late proposal',
                steps_json=json.dumps([step('save_forecast',label='Late')])) )])
    result=run_request(db,'d'*32,'Prepare work','live',Late(),ctx=owner_context_for(before.business_id),lock=web.LOCK,time_budget_s=.05)
    assert result.status=='failed' and not _load(db).agent_workflows and not _load(db).forecast_snapshots


def test_model_cannot_modify_an_existing_owner_decision(live_server):
    _,db=live_server;l=_load(db);job=stage(l);approve(l,job)
    s=SQLiteStore(db);s.save_ledger(l);s.close()
    class MutatesDecision:
        provider_label='scripted adversarial adapter'
        def chat(self,messages,tools,*,deadline=None):
            # A trusted-code fake probes the commit guard; real model tools have no such capability.
            return AdapterResult(text='Pretend work is complete')
    from cashflow_copilot.agent_requests import _draft_only
    changed=copy.deepcopy(l);changed.agent_workflows[job['id']]['status']='pending_approval'
    assert not _draft_only(l,changed)


def test_queue_summary_is_bounded_and_does_not_expose_approval_token(ledger):
    for n in range(21):stage(ledger,ident='queued-'+str(n))
    result=read(ledger,'queue',{},ctx=ctx(ledger))
    assert result['total']==21 and len(result['workflows'])==20 and result['next_offset']==20
    assert 'review_token' not in json.dumps(result) and 'execution' not in json.dumps(result)


def test_optional_contractual_due_date_is_converted_without_changing_expected_collection(ledger):
    original=ledger.receivables['inv-client-001'].expected_date
    job=stage(ledger,[step('item_identity',kind='invoice',item_id='inv-client-001',counterparty='Synthetic Client',
        invoice_reference='INV-reviewed',source='ev-client-invoice',note='Reviewed contractual due date',due_date='2026-10-19')])
    approve(ledger,job)
    assert ledger.receivables['inv-client-001'].due_date==date(2026,10,19)
    assert ledger.receivables['inv-client-001'].expected_date==original
