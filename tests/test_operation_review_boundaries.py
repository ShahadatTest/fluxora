"""All operation fixtures reject foreign authority and stale financial approval."""
import copy
import pytest
from cashflow_copilot.agent_workflows import prepare, decide
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.errors import CashflowError
from tools.operation_cases import fixture, names


@pytest.mark.parametrize('operation',names())
def test_each_operation_rejects_other_business_and_agent_approval(operation):
    ledger,step=fixture(operation)
    prepare(ledger,'boundary-job','Exact source-backed synthetic terms',[step],
            ctx=RequestContext('test-agent','agent',ledger.business_id))
    job=ledger.agent_workflows['boundary-job']
    before=copy.deepcopy(vars(ledger))
    for context in (RequestContext('foreign-owner','owner','different-business'),
                    RequestContext('test-agent','agent',ledger.business_id)):
        with pytest.raises(CashflowError):
            decide(ledger,job['id'],job['review_token'],'approve',ctx=context,
                   observed_confirmed=True,counterparty_confirmed=True)
        assert vars(ledger)==before


@pytest.mark.parametrize('operation',names())
def test_each_operation_requires_new_review_after_cash_changes(operation):
    ledger,step=fixture(operation)
    prepare(ledger,'stale-job','Exact source-backed synthetic terms',[step],
            ctx=RequestContext('test-agent','agent',ledger.business_id))
    job=ledger.agent_workflows['stale-job']
    next(iter(ledger.accounts.values())).opening_minor+=1
    before=copy.deepcopy(vars(ledger))
    with pytest.raises(CashflowError,match='changed'):
        decide(ledger,job['id'],job['review_token'],'approve',
               ctx=RequestContext('owner','owner',ledger.business_id),
               observed_confirmed=True,counterparty_confirmed=True)
    assert vars(ledger)==before
