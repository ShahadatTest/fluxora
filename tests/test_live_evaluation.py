"""Evaluation harness regression, explicitly scripted and never live-model evidence."""
import copy,json
import pytest
from cashflow_copilot.agent import AgentRunResult
from cashflow_copilot.model_adapter import AdapterResult,ToolCall
from tools.live_cases import CASES,assertions,case_ledger,evaluate
from tools.verify_live import main


def plan(case):
    calls=[]
    for name in case.tools:
        args={}
        if name=='draft_action':args=dict(action_id='eval-draft',kind='request_early_payment',receivable_id='inv-x',amount_minor=5000,date='2027-01-03')
        if name=='compute_forecast' and case.id=='delay':args=dict(scenario=dict(receivable_id='inv-x',shift_receivable_days=7))
        if name=='compute_forecast' and case.id in {'partial_collection','no_collection'}:args=dict(scenario=dict(receivable_id='inv-x',collection_percent=50 if case.id=='partial_collection' else 0))
        calls.append(ToolCall('call-'+name,name,args))
    return calls


class Scripted:
    provider_label='scripted evaluation harness; not a live model'
    def __init__(self,case):self.case=case;self.called=False
    def chat(self,messages,tools,*,deadline=None):
        if not self.called:
            self.called=True;calls=plan(self.case)
            if calls:return AdapterResult(tool_calls=calls)
        return AdapterResult(text='Scripted harness output. Human commentary/refusal assessment still required.')


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.id)
def test_each_reference_case_accepts_exact_scripted_tools_without_claiming_live_success(case):
    result=evaluate(Scripted,[case],mode='scripted_test')[0]
    assert result['automated_passed'],[c for c in result['checks'] if not c['passed']]
    assert result['mode']=='scripted_test' and result['financial_prose_verified'] is False and result['manual_review']


@pytest.mark.parametrize('case_id',['breach','credit_customer','credit_supplier','credit_reversed','loan_position','future_loan','statement_mismatch','no_feasible','delay','account_funding'])
def test_expected_tool_name_without_correct_result_cannot_pass(case_id):
    case=next(c for c in CASES if c.id==case_id);l=case_ledger(case_id)
    records=[dict(name=name,ok=True,arguments={},result={}) for name in case.tools]
    run=AgentRunResult('run',l.business_id,case.question,'scripted','Unsupported claim',records,'now','now')
    try:checks=assertions(case,l,copy.deepcopy(l),run)
    except (KeyError,TypeError):return  # malformed output is rejected, not counted passed
    assert not all(c['passed'] for c in checks)


def test_unexpected_financial_mutation_fails_even_for_a_refusal_question():
    case=next(c for c in CASES if c.id=='authority_refusal');l=case_ledger(case.id);changed=copy.deepcopy(l)
    changed.accounts['usd'].opening_minor+=1
    r=AgentRunResult('run',l.business_id,case.question,'scripted','I refuse',[],'now','now')
    checks=assertions(case,l,changed,r)
    assert not next(c['passed'] for c in checks if c['name']=='financial_and_authority_state_unchanged')


def test_case_listing_makes_zero_provider_calls(monkeypatch,capsys):
    monkeypatch.setattr('tools.verify_live.OpenAICompatibleAdapter.from_env',lambda:pytest.fail('Listing must not construct a provider'))
    assert main(['--list-cases'])==0
    rows=json.loads(capsys.readouterr().out);assert len(rows)==23


def test_absent_environment_is_pending_with_all_planned_cases_and_zero_calls(tmp_path,monkeypatch):
    for name in ['CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME']:monkeypatch.delenv(name,raising=False)
    monkeypatch.setattr('tools.verify_live.OpenAICompatibleAdapter.from_env',lambda:pytest.fail('Missing credentials must not call provider'))
    p=tmp_path/'pending.json';assert main(['--output',str(p)])==2
    r=json.loads(p.read_text());assert len(r['planned_cases'])==23 and r['questions']==[] and not r['financial_prose_verified']


def test_case_filter_only_plans_selected_provider_calls(tmp_path,monkeypatch):
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False);monkeypatch.delenv('CASHFLOW_MODEL_NAME',raising=False)
    p=tmp_path/'pending.json';assert main(['--case','loan_position','--case','statement_mismatch','--output',str(p)])==2
    assert json.loads(p.read_text())['planned_cases']==['loan_position','statement_mismatch']


def test_completed_case_checkpoint_survives_interruption_without_exporting_secret(tmp_path, monkeypatch):
    from types import SimpleNamespace
    for key,value in {'CASHFLOW_MODEL_API_KEY':'DUMMY-CHECKPOINT-KEY',
                      'CASHFLOW_MODEL_NAME':'scripted-test'}.items():monkeypatch.setenv(key,value)
    adapter=SimpleNamespace(provider_label='scripted harness',request_policy=lambda:{},
                            verify=lambda:SimpleNamespace(state='verified'))
    monkeypatch.setattr('tools.verify_live.OpenAICompatibleAdapter.from_env',lambda:adapter)
    def interrupted(factory,cases,*,mode,data_mode,on_result):
        on_result(dict(id=cases[0].id,automated_passed=False,answer_for_manual_review='DUMMY-CHECKPOINT-KEY'))
        raise KeyboardInterrupt
    monkeypatch.setattr('tools.verify_live.evaluate',interrupted)
    output=tmp_path/'checkpoint.json'
    with pytest.raises(KeyboardInterrupt):main(['--output',str(output),'--interval-seconds','0'])
    report=json.loads(output.read_text())
    assert len(report['questions'])==1 and report['status'].startswith('in progress')
    assert report['questions'][0]['tool_smoke_passed'] is False
    assert 'DUMMY-CHECKPOINT-KEY' not in output.read_text()
    assert report['questions'][0]['answer_for_manual_review']=='[redacted]'


def test_evaluator_callback_receives_completed_rows_without_mutating_results():
    rows=[]
    def observe(row):
        rows.append(row['id']);row['automated_passed']=False
    results=evaluate(Scripted,CASES[:2],mode='scripted_test',on_result=observe)
    assert rows==[c.id for c in CASES[:2]] and all(r['automated_passed'] for r in results)


def test_live_diagnostic_is_symbolic_and_never_exports_arbitrary_error_text():
    from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
    adapter=OpenAICompatibleAdapter(api_key='dummy-diagnostic-key',model='scripted-test')
    def fail(messages,tools,*,deadline=None):
        adapter._state='error';adapter._last_error='untrusted arbitrary secret text'
        from cashflow_copilot.model_adapter import AdapterError
        raise AdapterError('untrusted arbitrary secret text')
    adapter.chat=fail
    result=evaluate(lambda _:adapter,[CASES[0]],mode='scripted_test')[0]
    assert result['provider_diagnostic']=='provider_response_or_configuration_rejected'
    assert result['outcome']=='provider_error' and result['http_status'] is None
    assert 'untrusted arbitrary secret text' not in json.dumps(result)
