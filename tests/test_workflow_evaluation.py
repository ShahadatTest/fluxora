"""Evaluation-harness regressions; scripted providers, never live evidence."""
import json
from types import SimpleNamespace
import pytest
from cashflow_copilot.model_adapter import AdapterResult,ToolCall
from tools.workflow_cases import CASES,evaluate_case,parameters_match
from tools.verify_workflows_live import main

def steps(case):
    if case.id=='snapshot':return [dict(operation='save_forecast',arguments=dict(label='Live weekly review'))]
    if case.id=='collection_batch':return [dict(operation='confirm_collection_date',arguments=dict(invoice_id='inv-client-001',expected_date='2026-11-05',evidence_id='ev-delay-message',reason='Synthetic delayed expectation')),
        dict(operation='save_forecast',arguments=dict(label='Reviewed client delay'))]
    if case.id=='observed_payment':return [dict(operation='record_bill_payment',arguments=dict(bill_id='supplier-001',payment_id='live-observed-100',amount_minor=10000,day='2026-10-16',evidence_id='ev-test-payment',note='Synthetic source observation'))]
    return []

class Scripted:
    provider_label='scripted evaluation harness; not a live model'
    def __init__(self,case,wrong=False):self.case=case;self.index=0;self.wrong=wrong
    def status(self):return SimpleNamespace(detail='')
    def chat(self,messages,tools,*,deadline=None):
        self.index+=1
        if self.case.operations:
            source_case=self.case.id in {'collection_batch','observed_payment'}
            if source_case and self.index==1:
                source='ev-delay-message' if self.case.id=='collection_batch' else 'ev-test-payment'
                return AdapterResult(tool_calls=[ToolCall('source','get_evidence',dict(evidence_id=source))])
            if self.index==1+int(source_case):return AdapterResult(tool_calls=[ToolCall('catalog','read_workspace',dict(view='catalog',parameters_json=json.dumps(dict(operation=self.case.operations[0]))))])
            if self.index==2+int(source_case):
                plan=steps(self.case)
                if self.wrong:plan[-1]['arguments']['label']='Wrong label'
                return AdapterResult(tool_calls=[ToolCall('proposal','prepare_workflow',dict(job_id='eval-workflow',label='Synthetic reviewed work',steps_json=json.dumps(plan)))])
        elif self.case.id=='workspace_report' and self.index==1:
            return AdapterResult(tool_calls=[ToolCall('weekly','read_workspace',dict(view='weekly_cashflow',parameters_json='{}'))])
        return AdapterResult(text='Scripted answer. Owner review required; real payments, consent fabrication, protected-payroll bypass and invented currency conversion are unavailable.')

@pytest.mark.parametrize('case',CASES,ids=lambda c:c.id)
def test_scripted_exact_workflows_pass_without_certifying_prose(case,tmp_path):
    row=evaluate_case(case,Scripted(case),tmp_path/'state.sqlite')
    assert row['automated_passed'],row['checks']
    assert row['manual_review_status']=='pending' and row['financial_prose_verified'] is False

def test_valid_but_wrong_snapshot_label_fails_reference_check(tmp_path):
    row=evaluate_case(CASES[0],Scripted(CASES[0],wrong=True),tmp_path/'state.sqlite')
    assert not row['automated_passed'] and row['owner_application']=='not attempted'

@pytest.mark.parametrize('case',CASES[:3])
def test_no_plan_cannot_pass_a_positive_case(case,tmp_path):
    adapter=Scripted(CASES[-1])
    row=evaluate_case(case,adapter,tmp_path/'state.sqlite')
    assert not row['automated_passed']

def test_no_answer_cannot_pass_a_refusal(tmp_path):
    adapter=Scripted(CASES[-1]);adapter.chat=lambda *a,**kw:AdapterResult(text='')
    assert not evaluate_case(CASES[-1],adapter,tmp_path/'state.sqlite')['automated_passed']

def test_listing_never_constructs_provider(monkeypatch,capsys,tmp_path):
    monkeypatch.setattr('tools.verify_workflows_live.OpenAICompatibleAdapter.from_env',lambda:pytest.fail('No provider for listing'))
    assert main(['--output',str(tmp_path/'unused.json'),'--list-cases'])==0
    assert len(json.loads(capsys.readouterr().out))==7

def test_missing_configuration_is_pending_without_calls(tmp_path,monkeypatch):
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False);monkeypatch.delenv('CASHFLOW_MODEL_NAME',raising=False)
    monkeypatch.setattr('tools.verify_workflows_live.OpenAICompatibleAdapter.from_env',lambda:pytest.fail('No provider with absent config'))
    output=tmp_path/'pending.json';assert main(['--output',str(output)])==2
    report=json.loads(output.read_text());assert report['questions']==[] and report['status'].startswith('pending:')

def test_provider_rate_limit_stops_remaining_cases_and_redacts_checkpoint(tmp_path,monkeypatch):
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','DUMMY-EVALUATION-KEY');monkeypatch.setenv('CASHFLOW_MODEL_NAME','scripted')
    monkeypatch.setattr('tools.verify_workflows_live.OpenAICompatibleAdapter.from_env',lambda:SimpleNamespace(provider_label='scripted',request_policy=lambda:{}))
    def rate_limit(*args):return dict(id='snapshot',automated_passed=False,http_status=429,answer_for_manual_review='DUMMY-EVALUATION-KEY')
    monkeypatch.setattr('tools.verify_workflows_live.evaluate_case',rate_limit)
    output=tmp_path/'limited.json';assert main(['--output',str(output)])==1
    report=json.loads(output.read_text());assert len(report['questions'])==1 and len(report['unattempted_cases'])==6
    assert 'DUMMY-EVALUATION-KEY' not in output.read_text()

def test_existing_report_cannot_be_overwritten(tmp_path):
    output=tmp_path/'keep.json';output.write_text('retained')
    with pytest.raises(SystemExit):main(['--output',str(output)])
    assert output.read_text()=='retained'

@pytest.mark.parametrize('stage',['configuration','case'])
def test_unexpected_local_failure_is_checkpointed_without_raw_exception(tmp_path,monkeypatch,stage):
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','DUMMY-LOCAL-KEY');monkeypatch.setenv('CASHFLOW_MODEL_NAME','scripted')
    def fail(*a,**kw):raise RuntimeError('DUMMY-LOCAL-KEY arbitrary secret error')
    monkeypatch.setattr('tools.verify_workflows_live.OpenAICompatibleAdapter.from_env',fail if stage=='configuration' else
        lambda:SimpleNamespace(provider_label='scripted',request_policy=lambda:{}))
    if stage=='case':monkeypatch.setattr('tools.verify_workflows_live.evaluate_case',fail)
    output=tmp_path/'failure.json';assert main(['--output',str(output)])==1
    report=json.loads(output.read_text());assert len(report['unattempted_cases'])==(7 if stage=='configuration' else 6)
    assert 'arbitrary secret error' not in output.read_text() and 'DUMMY-LOCAL-KEY' not in output.read_text()
