"""Six approved workflows: independent money controls, authority and HTTP/restart."""
import copy
from datetime import date,timedelta
import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import recovery,web
from cashflow_copilot.accounting import (record_bill_payment,correct_bill_payment,correct_receipt,
    save_forecast,compare_forecast)
from cashflow_copilot.agent import TOOLS,execute_tool
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.completion import adjust_face,correct_cash_movement,correct_split_payment
from cashflow_copilot.errors import CashflowError,AuthorizationError
from cashflow_copilot.exploration import compound_scenario,save_scenario,search_records,cash_calendar
from cashflow_copilot.extension import (record_refund,correct_refund,correct_payment_date,
    create_instalments,advance_instalments,movement_date)
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account,Receipt,Plan,OutflowAmendment
from cashflow_copilot.owner_workflows import review_revision,daily_preview,close_day
from cashflow_copilot.payment_review import observed_movements,payment_warnings
from cashflow_copilot.planning import _revision,plan_draft_preview
from cashflow_copilot.reports import report_tables
from cashflow_copilot.settlement import cash_events,bill_schedule,effective_receipt,receipt_fingerprint
from cashflow_copilot.store import SQLiteStore
from tests.test_adaptive_priorities import own
from tests.test_business_tools import imported,split
from tests.test_owner_workflows import ctx,save,receipt
from tests.test_completion import cash,statement,match
from tests.test_web import live_server,_get,_post,_load


def refund(ledger,kind='invoice',amount=5000,ident='refund-one',day=None,revision=None,caller=None,ack=False):
    return record_refund(ledger,kind,'custom-x' if kind=='invoice' else 'materials',ident,amount,day or ledger.as_of,
        'customer-x','Checked observed synthetic refund.',revision or review_revision(ledger),ctx=caller or ctx(ledger),acknowledge_duplicates=ack)


def credit(ledger,kind='invoice'):
    if kind=='invoice':receipt(ledger,amount=20000)
    else:record_bill_payment(ledger,'materials','paid-one',20000,ledger.as_of,'supplier-source','Observed.',review_revision(ledger),ctx=ctx(ledger))
    adjust_face(ledger,kind,'custom-x' if kind=='invoice' else 'materials',10000,ledger.as_of,'customer-x','Source credit.',review_revision(ledger),ctx=ctx(ledger))


def datefix(ledger,key='receipt:existing-r',day=None,revision=None,caller=None):
    return correct_payment_date(ledger,key,day or ledger.as_of+timedelta(days=2),'customer-x','Checked original posting date.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def agreement(ledger,ident='agreement-one',bill='materials',schedule=None,revision=None,caller=None):
    schedule=schedule or [dict(date='2027-01-15',amount_minor=20000),dict(date='2027-01-22',amount_minor=ledger.outflows[bill].remaining_minor-20000)]
    return create_instalments(ledger,ident,bill,schedule,'supplier-source','Proposed remaining supplier terms.',revision or review_revision(ledger),ctx=caller or ctx(ledger))


def step(ledger,step='approve',revision=None,caller=None,ident='agreement-one'):
    return advance_instalments(ledger,ident,step,'supplier-source','Checked explicit external synthetic terms.',revision or review_revision(ledger),ctx=caller or ctx(ledger))


@pytest.mark.parametrize('kind,sign',[('invoice',-1),('bill',1)])
def test_partial_full_refund_preserves_originals_claim_and_cash_once(own,kind,sign):
    credit(own,kind);before=own.actual_cash('USD');originals=copy.deepcopy(own.receipts if kind=='invoice' else own.outflows['materials'].observed_payments)
    item=own.receivables['custom-x'] if kind=='invoice' else own.outflows['materials']
    refund(own,kind);assert own.actual_cash('USD')==before+sign*5000
    assert (item.credit_balance_minor if kind=='invoice' else item.overpaid_minor)==5000 and item.remaining_minor==0
    refund(own,kind,5000,'refund-two',ack=True)
    assert own.actual_cash('USD')==before+sign*10000 and item.remaining_minor==0
    assert (item.credit_balance_minor if kind=='invoice' else item.overpaid_minor)==0
    assert originals==(own.receipts if kind=='invoice' else own.outflows['materials'].observed_payments)
    curve=forecast(own,own.as_of,30).currencies['USD']
    assert sum(e['amount_minor'] for e in cash_events(own) if e['kind']=='refund_observed')==sign*10000
    assert not any(c['code'] in {'customer_credit_requires_review','supplier_overpayment_requires_review'} for c in forecast(own,own.as_of,30).clarifications)
    assert not own.actions and not own.consent_map


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_refund_correction_reversal_and_source_reinstatement(own,kind):
    credit(own,kind);before=own.actual_cash('USD');refund(own,kind,10000)
    sign=-1 if kind=='invoice' else 1
    result=correct_refund(own,'refund-one',3000,own.as_of,'customer-x','Amount source correction.',review_revision(own),ctx=ctx(own))
    assert result['cash_delta_minor']==sign*-7000 and own.actual_cash('USD')==before+sign*3000
    correct_refund(own,'refund-one',0,own.as_of,'customer-x','Mistaken observation reversed.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==before
    correct_refund(own,'refund-one',10000,own.as_of,'customer-x','Observed full amount confirmed.',review_revision(own),ctx=ctx(own))
    adjust_face(own,kind,'custom-x' if kind=='invoice' else 'materials',30000,own.as_of,'customer-x','Correct source total.',review_revision(own),ctx=ctx(own))
    item=own.receivables['custom-x'] if kind=='invoice' else own.outflows['materials']
    assert item.remaining_minor==20000  # paid/collected 20k minus refund 10k -> net 10k
    assert own.actual_cash('USD')==before+sign*10000


@pytest.mark.parametrize('bad',['over','zero','negative','bool','no_credit','past','far','unknown','source','note','agent','viewer','business','stale','id_collision'])
def test_invalid_refund_atomic(own,bad):
    if bad!='no_credit':credit(own)
    amount=5000;day=own.as_of;ident='refund-one';evidence='customer-x';note='Checked.';caller=ctx(own);revision=review_revision(own)
    if bad=='over':amount=10001
    if bad=='zero':amount=0
    if bad=='negative':amount=-1
    if bad=='bool':amount=True
    if bad=='past':day-=timedelta(days=1)
    if bad=='far':day+=timedelta(days=367)
    if bad=='unknown':ident='../bad'
    if bad=='source':evidence='not-here'
    if bad=='note':note=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='another')
    if bad=='stale':revision='old'
    if bad=='id_collision':ident='existing-r'
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):record_refund(own,'invoice','custom-x',ident,amount,day,evidence,note,revision,ctx=caller)
    assert vars(own)==before


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_refund_must_not_be_unbacked_by_later_payment_reversal(own,kind):
    credit(own,kind);refund(own,kind,10000);before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):
        if kind=='invoice':correct_receipt(own,'existing-r','custom-x',0,own.as_of,'customer-x','Invalid reversal.',review_revision(own),ctx=ctx(own))
        else:correct_bill_payment(own,'materials','paid-one',0,own.as_of,'supplier-source','Invalid reversal.',review_revision(own),ctx=ctx(own))
    assert vars(own)==before


def test_refund_duplicate_warning_and_cross_path_id(own):
    credit(own);cash(own,'fee',5000);before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):refund(own)
    assert vars(own)==before
    assert refund(own,ack=True)['possible_duplicate_ids']==['other_cash:fee']
    with pytest.raises(CashflowError):cash(own,'refund-one',100)
    with pytest.raises(CashflowError):receipt(own,rid='refund-one',amount=10)
    assert any(p['status']=='distinct_acknowledged' for p in payment_warnings(own,ctx=ctx(own)))


def test_refund_future_source_date_and_daily_close_and_variance(own):
    credit(own);snap=save_forecast(own,'Before refund.',review_revision(own),ctx=ctx(own))['snapshot_id']
    refund(own,day=own.as_of+timedelta(days=1));assert own.actual_cash('USD')==120000
    assert own.actual_cash('USD',date(2027,1,2))==115000
    comparison=compare_forecast(own,snap,date(2027,1,2),ctx=ctx(own))['currencies']['USD'][-1]
    assert comparison['recorded_closing_minor']==115000 and comparison['other_cash_minor']==-5000
    prev=daily_preview(own,date(2027,1,3),ctx=ctx(own));assert prev['accounts'][0]['calculated_opening_minor']==115000 and prev['accounts'][0]['other_cash_minor']==-5000
    own.roll_forward(date(2027,1,3),ctx=ctx(own));assert own.actual_cash('USD')==115000


@pytest.mark.parametrize('kind',['receipt','bill_payment','split_payment','other_cash','refund'])
def test_payment_date_correction_changes_timeline_not_amount_original_or_allocation(own,kind):
    # Split children allocate on Jan 1, so move the whole payment earlier from Jan 3.
    if kind=='receipt':receipt(own,day=date(2027,1,3))
    elif kind=='bill_payment':record_bill_payment(own,'materials','paid-one',10000,date(2027,1,3),'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    elif kind=='split_payment':imported(own);split(own)
    elif kind=='other_cash':cash(own,day=date(2027,1,3))
    else:credit(own);refund(own,day=date(2027,1,3))
    ident={'receipt':'existing-r','bill_payment':'paid-one','split_payment':'bulk-one','other_cash':'fee-one','refund':'refund-one'}[kind]
    key=kind+':'+ident
    if kind=='split_payment':
        # Move original & child allocation dates forward to construct an independent fixture.
        own.split_receipts[ident]['date']='2027-01-03'
        for rid in own.split_receipts[ident]['allocation_ids']:own.receipts[rid].date_received=date(2027,1,3)
    originals=copy.deepcopy((own.receipts,own.outflows,own.split_receipts,own.cash_movements));events=list(cash_events(own));before=sum(e['amount_minor'] for e in events)
    datefix(own,key,own.as_of)
    assert sum(e['amount_minor'] for e in cash_events(own))==before
    assert (own.receipts,own.outflows,own.split_receipts,own.cash_movements)==originals
    row=next(r for r in observed_movements(own) if r['key']==key);assert row['date']=='2027-01-01'
    event=next(e for e in cash_events(own) if e['id']==ident and e['kind'] in {'observed_receipt','observed_bill_payment','other_cash_observed','refund_observed'})
    assert event['original_date']==date(2027,1,3) and event['date']==own.as_of


def test_date_correction_later_amount_correction_keeps_accounting_date_and_matches_stale(own):
    receipt(own,amount=10000)
    correct_receipt(own,'existing-r','custom-x',8000,date(2027,1,5),'customer-x','Correction.',review_revision(own),ctx=ctx(own))
    bid=statement(own,[dict(id='bank-one',date='2027-01-01',currency='USD',signed_minor=8000,reference='source')])
    match(own,bid,movement='receipt:existing-r')
    fingerprint=receipt_fingerprint(own,'existing-r');datefix(own,day=date(2027,1,3))
    assert receipt_fingerprint(own,'existing-r')!=fingerprint and effective_receipt(own,'existing-r').date_received==date(2027,1,3)
    events=list(cash_events(own));assert next(e for e in events if e['kind']=='receipt_correction')['date']==date(2027,1,5)
    from cashflow_copilot.completion import statement_report
    assert statement_report(own,bid,ctx=ctx(own))['rows'][0]['status']=='needs_review'
    assert own.actual_cash('USD',date(2027,1,2))==100000 and own.actual_cash('USD',date(2027,1,5))==108000
    with pytest.raises(CashflowError):datefix(own,day=date(2027,1,6))


def test_unchanged_v10_movement_and_receipt_fingerprints_remain_compatible(own):
    from cashflow_copilot.planning import _hash
    receipt(own)
    row=observed_movements(own)[0];saved_row={k:v for k,v in row.items() if k!='fingerprint'}
    assert row['fingerprint']==_hash([own.business_id,saved_row,[]])
    assert receipt_fingerprint(own,'existing-r')==_hash(dict(original=own.receipts['existing-r'],adjustments=[]))


def test_refund_date_never_moves_before_same_day_source_payment(own):
    receipt(own,amount=20000,day=date(2027,1,3))
    adjust_face(own,'invoice','custom-x',10000,own.as_of,'customer-x','Credit source.',review_revision(own),ctx=ctx(own))
    refund(own,day=date(2027,1,3));before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):datefix(own,'refund:refund-one',date(2027,1,2))
    assert vars(own)==before


def test_payment_amount_correction_respects_effective_corrected_bill_and_other_dates(own):
    record_bill_payment(own,'materials','paid-one',10000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    cash(own);datefix(own,'bill_payment:paid-one',date(2027,1,3));datefix(own,'other_cash:fee-one',date(2027,1,3))
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):correct_bill_payment(own,'materials','paid-one',9000,date(2027,1,2),'supplier-source','Bad chronology.',review_revision(own),ctx=ctx(own))
    with pytest.raises(CashflowError):correct_cash_movement(own,'fee-one',1000,date(2027,1,2),'supplier-source','Bad chronology.',review_revision(own),ctx=ctx(own))
    assert vars(own)==before


def test_date_correction_and_saved_forecast_keep_original_plan_but_restate_actual_dates(own):
    snap=save_forecast(own,'Original forecast.',review_revision(own),ctx=ctx(own))['snapshot_id']
    saved=copy.deepcopy(own.forecast_snapshots[snap]);receipt(own,amount=10000);datefix(own,day=date(2027,1,3))
    result=compare_forecast(own,snap,date(2027,1,3),ctx=ctx(own))
    assert [r['recorded_receipts_minor'] for r in result['currencies']['USD']]==[0,0,10000]
    assert own.forecast_snapshots[snap]==saved and result['invoices'][0]['recorded_net_allocation_minor']==10000


def test_date_correction_that_creates_equal_movements_requires_payment_warning_review(own):
    cash(own,'fee-one',1200,day=date(2027,1,1));cash(own,'fee-two',1200,day=date(2027,1,3))
    datefix(own,'other_cash:fee-two',date(2027,1,1))
    assert payment_warnings(own,ctx=ctx(own))[0]['status']=='review_required'
    with pytest.raises(CashflowError):plan_draft_preview(own,'auto-collections',ctx=ctx(own))


@pytest.mark.parametrize('bad',['past','closed','far','same','unknown','source','agent','viewer','business','stale'])
def test_invalid_date_correction_atomic(own,bad):
    receipt(own);key='receipt:existing-r';day=own.as_of+timedelta(days=2);ev='customer-x';caller=ctx(own);rev=review_revision(own)
    if bad=='past':day=own.as_of-timedelta(days=1)
    if bad=='closed':own.roll_forward(date(2027,1,2),ctx=ctx(own));rev=review_revision(own)
    if bad=='far':day=own.as_of+timedelta(days=367)
    if bad=='same':day=own.as_of
    if bad=='unknown':key='receipt:missing'
    if bad=='source':ev='missing'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):correct_payment_date(own,key,day,ev,'Checked.',rev,ctx=caller)
    assert vars(own)==before


def test_split_date_cannot_move_after_child_allocation(own):
    imported(own);split(own);before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):datefix(own,'split_payment:bulk-one')
    child=own.split_receipts['bulk-one']['allocation_ids'][0]
    with pytest.raises(CashflowError):datefix(own,'receipt:'+child)
    assert vars(own)==before


def test_exact_instalment_lifecycle_applies_only_after_acceptance_and_never_creates_cash(own):
    record_bill_payment(own,'materials','paid-one',10000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    adjust_face(own,'bill','materials',45000,own.as_of,'supplier-source','Adjusted source.',review_revision(own),ctx=ctx(own))
    before=own.actual_cash('USD');old_schedule=bill_schedule(own.outflows['materials']);agreement(own)
    assert bill_schedule(own.outflows['materials'])==old_schedule
    step(own);assert bill_schedule(own.outflows['materials'])==old_schedule and not own.runtime_evidence_ids
    step(own,'accept');assert bill_schedule(own.outflows['materials'])==old_schedule and not own.consent_map
    step(own,'apply');assert bill_schedule(own.outflows['materials'])==[(date(2027,1,15),20000),(date(2027,1,22),15000)]
    assert own.outflows['materials'].date==date(2027,1,7) and own.actual_cash('USD')==before
    record_bill_payment(own,'materials','paid-two',5000,date(2027,1,15),'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    assert bill_schedule(own.outflows['materials'])==[(date(2027,1,15),15000),(date(2027,1,22),15000)]
    assert not own.actions and len(own.outflows['materials'].observed_payments)==2


@pytest.mark.parametrize('bad',['protected','not_supplier','no_remaining','sum','order','duplicate_date','negative','bool','past','far','unknown','agent','viewer','business','stale','constraint','source','extra'])
def test_invalid_agreement_atomic(own,bad):
    bill='materials';schedule=[dict(date='2027-01-15',amount_minor=20000),dict(date='2027-01-22',amount_minor=30000)]
    rev=review_revision(own);caller=ctx(own);ev='supplier-source'
    if bad=='protected':bill='staff'
    if bad=='not_supplier':bill='overhead'
    if bad=='no_remaining':own.outflows[bill].face_adjustments.append(dict(new_total_minor=0,date=str(own.as_of),schedule=[]));rev=review_revision(own)
    if bad=='sum':schedule[0]['amount_minor']=10000
    if bad=='order':schedule.reverse()
    if bad=='duplicate_date':schedule[1]['date']=schedule[0]['date']
    if bad=='negative':schedule[0]['amount_minor']=-20000
    if bad=='bool':schedule[0]['amount_minor']=True
    if bad=='past':schedule[0]['date']='2026-12-31'
    if bad=='far':schedule[1]['date']='2029-01-01'
    if bad=='unknown':bill='missing'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    if bad=='constraint':own.constraints.allow_supplier_date_change=False;rev=review_revision(own)
    if bad=='source':ev='missing'
    if bad=='extra':schedule[0]['approve']=True
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):create_instalments(own,'agreement-one',bill,schedule,ev,'Checked.',rev,ctx=caller)
    assert vars(own)==before


@pytest.mark.parametrize('stage',['draft','owner_approved','counterparty_accepted'])
def test_agreement_changed_bill_invalidates_old_approval_and_consent(own,stage):
    agreement(own)
    if stage!='draft':step(own)
    if stage=='counterparty_accepted':step(own,'accept')
    record_bill_payment(own,'materials','later-payment',100,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):step(own,{'draft':'approve','owner_approved':'accept','counterparty_accepted':'apply'}[stage])
    assert vars(own)==before
    step(own,'cancel');assert own.instalment_proposals['agreement-one']['state']=='cancelled'
    with pytest.raises(CashflowError):step(own,'approve')
    agreement(own,'fresh-proposal')


@pytest.mark.parametrize('illegal',['accept','apply','send','record_payment'])
def test_instalment_steps_cannot_be_skipped_or_executed_by_agent(own,illegal):
    agreement(own);before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):step(own,illegal)
    with pytest.raises(AuthorizationError):step(own,'approve',caller=ctx(own,'agent'))
    assert vars(own)==before


def test_new_source_adjustment_after_applied_agreement_preserves_dates_and_prohibits_auto_redeferral(own):
    agreement(own);step(own);step(own,'accept');step(own,'apply')
    adjust_face(own,'bill','materials',40000,own.as_of,'supplier-source','New source amount.',review_revision(own),ctx=ctx(own))
    assert bill_schedule(own.outflows['materials'])==[(date(2027,1,15),20000),(date(2027,1,22),20000)]
    with pytest.raises(CashflowError):own.apply_recorded_amendment('materials',new_date=date(2027,1,25),evidence_id='supplier-source',kind='owner_approved_deferral',actor='owner',ctx=ctx(own))
    assert own.outflows['staff'].date==date(2027,1,5)


def test_payment_date_change_invalidates_pending_supplier_agreement(own):
    record_bill_payment(own,'materials','paid-one',10000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    agreement(own);step(own);step(own,'accept');datefix(own,'bill_payment:paid-one',date(2027,1,3))
    with pytest.raises(CashflowError):step(own,'apply')


def test_instalment_acceptance_cannot_authorize_unrelated_generic_bill_move(own):
    agreement(own);step(own);step(own,'accept')
    eid=own.instalment_proposals['agreement-one']['acceptance_evidence_id']
    assert eid not in own.runtime_evidence_ids and not own.consent_map
    with pytest.raises(CashflowError):
        own.apply_recorded_amendment('materials',actor='owner',evidence_id=eid,kind='counterparty_accepted_move',new_date=date(2027,1,29),ctx=ctx(own))
    step(own,'apply')
    assert own.instalment_proposals['agreement-one']['state']=='applied'


def test_future_payment_basis_blocks_earlier_supplier_instalment(own):
    record_bill_payment(own,'materials','paid-one',10000,date(2027,1,20),'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    with pytest.raises(CashflowError):agreement(own)


def test_combined_scenario_exact_comparison_currency_separation_no_mutation(own):
    own.accounts['eur']=Account('eur','EUR',10000,1000)
    before=copy.deepcopy(vars(own))
    result=compound_scenario(own,[dict(invoice_id='custom-x',delay_days=7),dict(invoice_id='custom-y',delay_days=40)],
        [dict(label='Stock',currency='USD',amount_minor=5000,date='2027-01-10'),dict(label='Fee',currency='EUR',amount_minor=500,date='2027-01-10')],ctx=ctx(own,'viewer'))
    assert vars(own)==before
    base=result['baseline'].currencies;scenario=result['scenario'].currencies
    assert scenario['USD'].closing_minor==base['USD'].closing_minor-5000-45000
    assert scenario['EUR'].closing_minor==9500 and base['EUR'].closing_minor==10000
    assert not result['floors_hold'] and 'No feasible' in result['conclusion']
    assert any('outside' in a for a in result['assumptions']) and len(result['evidence_ids'])==2


@pytest.mark.parametrize('bad',['none','unknown','duplicate','held','date_unknown','delay_bool','delay_negative','delay_big','currency','expense_zero','expense_bool','expense_past','expense_outside','expense_extra','business'])
def test_invalid_compound_scenario_atomic(own,bad):
    delays=[dict(invoice_id='custom-x',delay_days=7)];expenses=[dict(label='Stock',currency='USD',amount_minor=5000,date='2027-01-10')];caller=ctx(own)
    if bad=='none':delays=[];expenses=[]
    if bad=='unknown':delays[0]['invoice_id']='missing'
    if bad=='duplicate':delays*=2
    if bad=='held':own.receivables['custom-x'].collection_hold=dict(active=True,evidence_id='customer-x')
    if bad=='date_unknown':own.receivables['custom-x'].expected_date=None
    if bad=='delay_bool':delays[0]['delay_days']=True
    if bad=='delay_negative':delays[0]['delay_days']=-1
    if bad=='delay_big':delays[0]['delay_days']=91
    if bad=='currency':expenses[0]['currency']='EUR'
    if bad=='expense_zero':expenses[0]['amount_minor']=0
    if bad=='expense_bool':expenses[0]['amount_minor']=True
    if bad=='expense_past':expenses[0]['date']='2026-12-31'
    if bad=='expense_outside':expenses[0]['date']='2027-02-15'
    if bad=='expense_extra':expenses[0]['approve']=True
    if bad=='business':caller=ctx(own,business='other')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):compound_scenario(own,delays,expenses,ctx=caller)
    assert vars(own)==before


def test_saved_scenario_restarts_and_reruns_current_inputs_not_old_results(own,tmp_path):
    delays=[dict(invoice_id='custom-x',delay_days=7)];rev=review_revision(own)
    result=save_scenario(own,'Delay scenario',delays,[],rev,ctx=ctx(own));ident=result['scenario_id']
    path=tmp_path/'scenario.sqlite';save(path,own);loaded=_load(path);before=copy.deepcopy(vars(loaded))
    assert save_scenario(loaded,'Delay scenario',delays,[],rev,ctx=ctx(loaded))['duplicate'] and vars(loaded)==before
    saved=loaded.scenarios[ident];assert saved['basis']==_revision(loaded,'scenario',[])
    receipt(loaded,amount=1000);assert saved['basis']!=_revision(loaded,'scenario',[])
    assert compound_scenario(loaded,saved['delays'],saved['expenses'],ctx=ctx(loaded))['baseline'].currencies['USD'].days[0].inflow_minor==1000


def test_search_literal_party_reference_evidence_date_currency_and_html_safe(own):
    own.receivables['custom-x'].counterparty='<script>Company</script>';own.receivables['custom-x'].invoice_reference='INV-REF-99'
    before=copy.deepcopy(vars(own))
    for q in ['custom-x','company','INV-REF-99','Synthetic source: customer-x']:
        assert any(r['id']=='custom-x' for r in search_records(own,q,ctx=ctx(own,'viewer'))['rows'])
    assert search_records(own,"' OR 1=1 --",ctx=ctx(own))['total']==0
    assert search_records(own,'',kind='invoice',currency='USD',from_date=date(2027,1,13),ctx=ctx(own))['rows'][0]['id']=='custom-y'
    assert search_records(own,'customer-x',kind='evidence',from_date=date(2027,1,1),ctx=ctx(own))['total']==0
    assert vars(own)==before


def test_search_refunds_statements_agreements_scenarios_and_corrected_effective_dates(own):
    credit(own);refund(own,day=date(2027,1,3));datefix(own,'refund:refund-one',date(2027,1,2));agreement(own)
    save_scenario(own,'Delayed y',[dict(invoice_id='custom-y',delay_days=3)],[],review_revision(own),ctx=ctx(own))
    statement(own,[dict(id='bank-one',date='2027-01-02',currency='USD',signed_minor=-5000,reference='Refund bank ref')])
    for kind in ['refund','payment','statement','agreement','scenario']:
        assert search_records(own,'',kind=kind,ctx=ctx(own))['total']>=1
    assert search_records(own,'refund-one',kind='refund',from_date=date(2027,1,2),to_date=date(2027,1,2),ctx=ctx(own))['total']==1


def test_search_retains_reversed_payment_and_original_and_correction_source(own):
    receipt(own)
    correct_receipt(own,'existing-r','custom-x',0,own.as_of,'supplier-source','Reversal reference ZEBRA-42.',review_revision(own),ctx=ctx(own))
    for query in ['existing-r','ZEBRA-42','customer-x','supplier-source']:
        result=search_records(own,query,kind='payment',status='reversed',ctx=ctx(own))
        assert result['total']==1 and result['rows'][0]['amount_minor']==0


def test_search_pagination_and_cross_business_denial(own):
    from cashflow_copilot.models import Evidence
    for i in range(60):own.evidence['ev-'+str(i)]=Evidence('ev-'+str(i),'synthetic','pagination test')
    first=search_records(own,'pagination',ctx=ctx(own));second=search_records(own,'pagination',offset=50,ctx=ctx(own))
    assert first['total']==60 and len(first['rows'])==50 and first['next_offset']==50 and len(second['rows'])==10
    assert not ({r['id'] for r in first['rows']}&{r['id'] for r in second['rows']})
    for fn in [lambda:search_records(own,ctx=ctx(own,business='other')),lambda:cash_calendar(own,ctx=ctx(own,business='other'))]:
        with pytest.raises(AuthorizationError):fn()


@pytest.mark.parametrize('bad',[dict(kind='secrets'),dict(currency='JPY'),dict(offset=-1),dict(offset=True),dict(query='x'*201),dict(from_date='2027-01-01'),dict(from_date=date(2027,1,3),to_date=date(2027,1,1))])
def test_search_invalid_filters(own,bad):
    with pytest.raises(CashflowError):search_records(own,**bad,ctx=ctx(own))


def test_calendar_cash_components_equal_engine_and_keep_currencies_and_labels(own):
    own.accounts['eur']=Account('eur','EUR',10000,1000);credit(own);refund(own,day=date(2027,1,2));agreement(own);step(own);step(own,'accept');step(own,'apply')
    before=copy.deepcopy(vars(own));cal=cash_calendar(own,days=30,ctx=ctx(own,'viewer'));fc=forecast(own,own.as_of,30)
    for row in cal['rows']:
        ccy=row['currency'];d=next(d for d in fc.currencies[ccy].days if str(d.date)==row['date'])
        assert row['closing_minor']==d.closing_minor and row['intraday_low_minor']==d.intraday_low_minor
        assert sum(e['amount_minor'] for e in row['events'])==row['inflow_minor']-row['outflow_minor']
    assert any(e['state']=='recorded' and 'refund' in e['label'] for r in cal['rows'] for e in r['events'])
    assert any(e['state']=='scheduled' and 'Protected' in e['label'] for r in cal['rows'] for e in r['events'])
    assert vars(own)==before and len(cal['rows'])==60
    assert len(cash_calendar(own,days=7,currency='EUR',ctx=ctx(own))['rows'])==7


def test_calendar_unknown_held_overdue_and_later_start(own):
    own.receivables['custom-x'].expected_date=None
    own.receivables['custom-y'].collection_hold=dict(active=True,evidence_id='customer-y')
    own.outflows['materials'].date=date(2026,12,31)
    cal=cash_calendar(own,days=5,ctx=ctx(own));assert len(cal['undated'])==2
    assert any('overdue' in e['label'] for e in cal['rows'][0]['events'])
    later=cash_calendar(own,start=date(2027,1,3),days=5,ctx=ctx(own));assert later['rows'][0]['opening_minor']==50000


@pytest.mark.parametrize('bad',[dict(days=0),dict(days=63),dict(days=True),dict(currency='EUR'),dict(start=date(2026,12,31)),dict(start=date(2029,1,1))])
def test_invalid_calendar(own,bad):
    with pytest.raises(CashflowError):cash_calendar(own,**bad,ctx=ctx(own))


def test_all_new_metadata_backup_restore_idempotency_and_agent_denial(own,tmp_path):
    credit(own);rev=review_revision(own);refund(own,day=date(2027,1,3),revision=rev)
    revdate=review_revision(own);datefix(own,'refund:refund-one',date(2027,1,2),revdate)
    agreement(own);revapprove=review_revision(own);step(own,revision=revapprove);step(own,'accept');step(own,'apply')
    path=tmp_path/'state.sqlite';save(path,own);loaded=_load(path)
    assert vars(loaded)==vars(own)
    metadata=recovery.create_backup(path,'Six feature checkpoint.',ctx=ctx(loaded));assert metadata['app_version']=='0.17.1'
    preview=recovery.restore_preview(path,metadata['id'],ctx=ctx(loaded));assert preview['restored']['refunds']==1
    before=copy.deepcopy(vars(loaded))
    assert refund(loaded,day=date(2027,1,3),revision=rev)['duplicate']
    assert datefix(loaded,'refund:refund-one',date(2027,1,2),revdate)['duplicate']
    assert step(loaded,revision=revapprove)['duplicate'] and loaded.instalment_proposals['agreement-one']['state']=='applied'
    assert vars(loaded)==before
    for fn in [lambda:refund(loaded,revision=rev,caller=ctx(loaded,'agent')),lambda:step(loaded,revision=revapprove,caller=ctx(loaded,'agent'))]:
        with pytest.raises(AuthorizationError):fn()
    assert len(TOOLS)==8
    for name in ['record_refund','correct_payment_date','advance_instalments','save_scenario','send_payment']:
        with pytest.raises(CashflowError):execute_tool(ctx(loaded,'agent'),loaded,name,{},'mock')


@pytest.mark.parametrize('path',['/refunds','/payment-dates','/search','/instalments','/scenarios','/calendar'])
def test_six_pages_readonly_english_honest_and_csrf(live_server,path):
    base,db=live_server;before=copy.deepcopy(vars(_load(db)));status,body=_get(base,path)
    assert status==200 and 'AI not connected' in body and 'SYNTHETIC DEMO' in body
    assert vars(_load(db))==before
    status,_=_post(base,'/extensions/confirm',dict(operation='refund',confirmed='yes',csrf='wrong'))
    assert status==403


def http_fields(db,**values):return dict(revision=review_revision(_load(db)),**values)


def test_http_refund_preview_confirm_restart_concurrent_retry_and_injection(live_server,own):
    base,db=live_server;credit(own);save(db,own);_,html=_get(base,'/refunds');csrf=re.search("name='csrf' value='([^']+)'",html)[1]
    fields=http_fields(db,csrf=csrf,operation='refund',kind='invoice',item_id='custom-x',refund_id='http-refund',amount='50.00',date='2027-01-01',evidence_id='customer-x',note='<script>alert(1)</script>')
    before=copy.deepcopy(vars(_load(db)));status,body=_post(base,'/extensions/preview',fields)
    assert status==200 and '&lt;script&gt;' in body and '<script>alert' not in body and vars(_load(db))==before
    assert _post(base,'/extensions/confirm',fields)[0]==400
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _: _post(base,'/extensions/confirm',dict(fields,confirmed='yes'))[0],range(3)))
    assert results==[200]*3
    loaded=_load(db);assert loaded.actual_cash('USD')==115000 and len(loaded.receivables['custom-x'].refunds)==1
    assert _post(base,'/extensions/confirm',dict(fields,confirmed='yes',role='agent'))[0]==400
    before=copy.deepcopy(vars(loaded));assert _post(base,'/extensions/confirm',dict(fields,confirmed='yes'))[0]==200 and vars(_load(db))==before


def test_http_scenario_search_calendar_and_unknown_repeated_fields(live_server,own):
    base,db=live_server;save(db,own);_,html=_get(base,'/scenarios');csrf=re.search("name='csrf' value='([^']+)'",html)[1]
    before=copy.deepcopy(vars(_load(db)))
    payload=dict(csrf=csrf,label='Stress two invoices',delays='custom-x,7\ncustom-y,40',expenses='Stock,USD,50.00,2027-01-10')
    status,body=_post(base,'/scenarios/evaluate',payload)
    assert status==200 and 'No feasible' in body and 'Preview saving' in body and vars(_load(db))==before
    assert _post(base,'/scenarios/evaluate',dict(payload,approve='yes'))[0]==400
    assert _post(base,'/scenarios/evaluate',dict(payload,delays=['custom-x,7','custom-y,4']))[0]==400
    assert _get(base,'/search?q=custom-x&kind=invoice')[0]==200
    assert _get(base,'/calendar?days=7&currency=USD')[0]==200
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as error:_get(base,'/calendar?days=1000')
    assert error.value.code==400


def test_http_receipt_amount_correction_and_date_correction_keep_originals(live_server,own):
    base,db=live_server;receipt(own);save(db,own);_,body=_get(base,'/corrections?receipt_id=existing-r')
    csrf=re.search("name='csrf' value='([^']+)'",body)[1]
    status,body=_post(base,'/corrections/confirm',http_fields(db,csrf=csrf,confirmed='yes',receipt_id='existing-r',invoice_id='custom-x',amount='80.00',date='2027-01-05',evidence_id='customer-x',note='Checked source correction.'))
    assert status==200 and _load(db).actual_cash('USD')==110000
    payload=http_fields(db,csrf=csrf,operation='date_correct',movement_key='receipt:existing-r',new_date='2027-01-03',evidence_id='customer-x',note='Checked source date.')
    assert _post(base,'/extensions/preview',payload)[0]==200
    assert _post(base,'/extensions/confirm',dict(payload,confirmed='yes'))[0]==200
    ledger=_load(db);assert ledger.actual_cash('USD')==100000 and ledger.actual_cash('USD',date(2027,1,5))==108000
    assert ledger.receipts['existing-r'].amount_minor==10000 and ledger.receipts['existing-r'].date_received==date(2027,1,1)
