"""Every catalog operation reaches owner-approved persistence, not just a listed name."""
import copy
import pytest
from tools.operation_cases import fixture,names,assert_effect
from cashflow_copilot.agent_workflows import prepare,decide,catalog
from cashflow_copilot.authz import RequestContext,owner_context_for
from cashflow_copilot.store import SQLiteStore

@pytest.mark.parametrize('name',names())
def test_each_supported_operation_is_pending_only_then_owner_applied_and_restart_idempotent(name,tmp_path):
    ledger,step=fixture(name);before=copy.deepcopy(vars(ledger))
    baseline=copy.deepcopy(ledger)
    prepare(ledger,'case-one','Synthetic '+name,[step],ctx=RequestContext('agent','agent',ledger.business_id))
    assert {k:v for k,v in vars(ledger).items() if k not in {'audit','agent_workflows'}}=={k:v for k,v in before.items() if k not in {'audit','agent_workflows'}}
    job=ledger.agent_workflows['case-one'];assert job['approval'] is None and job['status']=='pending_approval'
    decide(ledger,'case-one',job['review_token'],'approve',ctx=owner_context_for(ledger.business_id),observed_confirmed=True,counterparty_confirmed=True)
    assert ledger.agent_workflows['case-one']['status']=='executed'
    assert ledger.agent_workflows['case-one']['execution']['results'][0]['operation']==name
    assert_effect(name,ledger,baseline)
    db=tmp_path/'state.sqlite';store=SQLiteStore(db);store.save_ledger(ledger);store.close();store=SQLiteStore(db);loaded=store.load_ledger();store.close()
    saved=copy.deepcopy(vars(loaded))
    assert decide(loaded,'case-one',job['review_token'],'approve',ctx=owner_context_for(loaded.business_id))['duplicate']
    assert vars(loaded)==saved

def test_split_assignment_catalog_matches_domain_object_and_ui_currency():
    from cashflow_copilot.agent_workflow_web import _terms
    from cashflow_copilot import web
    ledger,step=fixture('confirm_split_observation')
    detail=catalog('confirm_split_observation')['operations'][0]
    assert detail['arguments']['assignments']['type']=='object'
    prepare(ledger,'split-one','Split review',[step],ctx=RequestContext('agent','agent',ledger.business_id))
    assert 'USD 50.00' in _terms(web,ledger.agent_workflows['split-one'],ledger)
