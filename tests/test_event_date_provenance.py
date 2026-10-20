"""A changed collection date cites its event, not only the original invoice."""
import copy
from datetime import timedelta
from cashflow_copilot.agent import execute_tool
from cashflow_copilot.authz import RequestContext,owner_context_for
from cashflow_copilot.store import SQLiteStore


def read(ledger,name,args):
    return execute_tool(RequestContext('agent','agent',ledger.business_id),ledger,name,args)['result']


def test_applied_delay_records_date_source_and_forecast_keeps_amount_and_date_sources(ledger):
    ledger.apply_event('evt-delay-001',ctx=owner_context_for(ledger.business_id))
    assert ledger.collection_date_sources['inv-client-001']=='ev-delay-message'
    evidence=read(ledger,'get_evidence',{})
    rec=next(r for r in evidence['receivables'] if r['id']=='inv-client-001')
    assert rec['expected_date_evidence_id']=='ev-delay-message' and rec['evidence_id']=='ev-client-invoice'
    result=read(ledger,'compute_forecast',{})
    driver=next(r for r in result['currencies']['USD']['source_basis']['drivers'] if r['id']=='inv-client-001')
    assert {'ev-client-invoice','ev-delay-message'}<=set(driver['evidence_ids'])
    assert result['currencies']['USD']['min_balance']=='USD -2,000.00'


def test_legacy_saved_event_source_is_resolved_read_only_after_sqlite_reload(ledger,tmp_path):
    ledger.apply_event('evt-delay-001',ctx=owner_context_for(ledger.business_id))
    ledger.collection_date_sources.clear()  # Reproduce old saved demo state.
    db=tmp_path/'old.sqlite';s=SQLiteStore(db);s.save_ledger(ledger);s.close()
    s=SQLiteStore(db);loaded=s.load_ledger();s.close();before=copy.deepcopy(vars(loaded))
    assert loaded.collection_date_source('inv-client-001')=='ev-delay-message'
    assert vars(loaded)==before
    result=read(loaded,'compute_forecast',{})
    assert any('ev-delay-message' in row['evidence_ids'] for row in result['currencies']['USD']['source_basis']['drivers'])


def test_unapplied_or_nonmatching_event_does_not_supply_current_date_source(ledger):
    assert ledger.collection_date_source('inv-client-001')=='ev-client-invoice'
    ledger.apply_event('evt-delay-001',ctx=owner_context_for(ledger.business_id))
    ledger.collection_date_sources.clear()
    ledger.receivables['inv-client-001'].expected_date+=timedelta(days=1)
    assert ledger.collection_date_source('inv-client-001')=='ev-client-invoice'


def test_explicit_later_review_source_takes_precedence_and_event_replay_changes_nothing(ledger):
    ctx=owner_context_for(ledger.business_id);ledger.apply_event('evt-delay-001',ctx=ctx)
    ledger.collection_date_sources['inv-client-001']='ev-client-invoice'
    before=copy.deepcopy(vars(ledger));assert ledger.apply_event('evt-delay-001',ctx=ctx)['already_applied']
    assert ledger.collection_date_source('inv-client-001')=='ev-client-invoice' and vars(ledger)==before
