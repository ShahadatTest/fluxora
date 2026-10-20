import json
import pytest
from cashflow_copilot.model_adapter import AdapterResult, AdapterStatus, ToolCall
from tools.reliability_cases import CASES, evaluate


class Scripted:
    provider_label='SCRIPTED reliability harness regression — NOT LIVE'
    def __init__(self,case,guess=False):
        self.case=case;self.calls=0;self.guess=guess
    def status(self):return AdapterStatus('mock',self.provider_label,None,'')
    def chat(self,*args,**kwargs):
        self.calls+=1
        if self.calls==1:
            if self.case.expected_tool:
                return AdapterResult(tool_calls=[ToolCall('read',self.case.expected_tool,self.case.expected_arguments)])
            if self.guess:
                return AdapterResult(tool_calls=[ToolCall('guess','prepare_workflow',dict(job_id='guess-proposal',label='Guessed work',steps_json=json.dumps([dict(operation='save_forecast',arguments=dict(label='Unrequested snapshot'))])))])
        return AdapterResult(text='Scripted fixture: please supply the missing details; this is not model-quality evidence.')


@pytest.mark.parametrize('case',[c for c in CASES if not c.operation],ids=lambda c:c.id)
def test_reliability_harness_reference_and_invariants_with_scripted_provider(tmp_path,case):
    row=evaluate(case,Scripted(case),tmp_path/(case.id+'.sqlite'),provider='mock')
    assert row['automated_passed'],row['checks']
    assert row['financial_prose_verified'] is False


def test_reliability_harness_detects_unrequested_proposal_even_when_still_pending(tmp_path):
    case=next(c for c in CASES if c.id=='missing_observation')
    row=evaluate(case,Scripted(case,guess=True),tmp_path/'guess.sqlite',provider='mock')
    assert not row['automated_passed']
    assert any(c['name']=='no_unrequested_or_guessed_proposal' and not c['passed'] for c in row['checks'])
