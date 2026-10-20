"""Comparison telemetry/early-stop tests use local stubs and scripted rows only."""
import json
import pytest
from tools.compare_models import MeasuredAdapter,summary,main
from tests.test_model_adapter import stub,_StubHandler,_completion

@pytest.mark.parametrize('cost,expected',[(0.00123,'0.00123'),(0,'0'),(None,None),('secret',None),(-1,None),(True,None),(float('inf'),None)])
def test_cost_telemetry_is_typed_bounded_and_never_exports_extra_provider_fields(stub,cost,expected):
    payload=_completion('Synthetic');payload['usage']=dict(cost=cost,raw_secret='DUMMY-DO-NOT-EXPORT')
    _StubHandler.responses=[(200,payload)]
    a=MeasuredAdapter(api_key='DUMMY',base_url=stub,model='openai/gpt-6-luna',max_retries=0)
    assert a.chat([dict(role='user',content='Synthetic')],[]).text=='Synthetic'
    assert a.measurements[0]['reported_cost_usd']==expected
    assert 'DUMMY-DO-NOT-EXPORT' not in json.dumps(a.measurements)

def test_partial_costs_are_not_complete_or_an_invoice():
    rows=[dict(automated_passed=True,transport_measurements=[dict(response_received=True,reported_cost_usd='0.001')]),
        dict(automated_passed=False,transport_measurements=[dict(response_received=False,reported_cost_usd=None)])]
    r=summary(rows,7)
    assert r['reported_cost_subtotal_usd']=='0.001' and not r['cost_coverage_complete'] and not r['all_planned_automated_passed']

def test_plan_and_absent_environment_make_zero_calls(monkeypatch,tmp_path,capsys):
    monkeypatch.delenv('CASHFLOW_MODEL_API_KEY',raising=False)
    monkeypatch.setattr('tools.compare_models.evaluate_case',lambda *a:pytest.fail('No provider calls'))
    out=tmp_path/'pending.json';assert main(['--output',str(out),'--list-plan'])==0 and not out.exists()
    assert main(['--output',str(out)])==2 and json.loads(out.read_text())['models']==[]

def test_rate_limited_candidate_stops_without_retry_or_changing_selected_model(monkeypatch,tmp_path):
    monkeypatch.setenv('CASHFLOW_MODEL_API_KEY','DUMMY-COMPARE-KEY');monkeypatch.setenv('CASHFLOW_MODEL_BASE_URL','https://openrouter.ai/api/v1')
    monkeypatch.setenv('CASHFLOW_MODEL_NAME','retained-main-model')
    monkeypatch.setattr('tools.compare_models.evaluate_case',lambda *a:dict(id='snapshot',automated_passed=False,outcome='failed',http_status=429,answer_for_manual_review='DUMMY-COMPARE-KEY'))
    monkeypatch.setattr('tools.compare_models.time.sleep',lambda *a:None)
    out=tmp_path/'limited.json';assert main(['--output',str(out),'--model','openai/gpt-6-luna'])==0
    r=json.loads(out.read_text());assert len(r['models'][0]['cases'])==1 and len(r['models'][0]['unattempted_cases'])==6
    assert 'DUMMY-COMPARE-KEY' not in out.read_text()
    import os
    assert os.environ['CASHFLOW_MODEL_NAME']=='retained-main-model'

def test_existing_evidence_cannot_be_overwritten(tmp_path):
    out=tmp_path/'keep.json';out.write_text('retained')
    with pytest.raises(SystemExit):main(['--output',str(out)])
    assert out.read_text()=='retained'
