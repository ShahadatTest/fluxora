"""Independent financial controls for the seven approved owner workflows."""
import copy
from datetime import date,timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import re

import pytest

from cashflow_copilot import recovery,web
from cashflow_copilot.accounting import record_bill_payment,correct_bill_payment,correct_receipt,save_forecast,compare_forecast
from cashflow_copilot.adaptive import input_problems,generate_proposals,validate_current_request
from cashflow_copilot.agent import TOOLS,execute_tool
from cashflow_copilot.completion import (create_record,adjust_face,correct_split_payment,record_cash_movement,
    correct_cash_movement,import_statement,statement_report,review_statement_row,CASH_CLASSES)
from cashflow_copilot.business_tools import allocate_split_balance,data_freshness,confirm_fact_check
from cashflow_copilot.errors import CashflowError,AuthorizationError
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Receipt,Account,Plan,OutflowAmendment
from cashflow_copilot.owner_workflows import review_revision,daily_preview,close_day,allocate_observation
from cashflow_copilot.payment_review import payment_warnings,observed_movements,review_payment_pair
from cashflow_copilot.planning import _revision,plan_draft_preview
from cashflow_copilot.reports import report_tables
from cashflow_copilot.settlement import cash_events,bill_schedule,effective_split_payment,split_unallocated
from tests.test_adaptive_priorities import own
from tests.test_business_tools import imported,split,more
from tests.test_owner_workflows import ctx,save,receipt
from tests.test_web import live_server,_get,_post,_load,_mpost


def entry(kind='invoice',ident='manual-one',amount=12000):
    return dict(kind=kind,id=ident,currency='USD',amount_minor=amount,date='2027-01-20',due_date='2027-01-25' if kind=='invoice' else None,
        category='invoice' if kind=='invoice' else 'supplier',protected=False,counterparty='Synthetic party',invoice_reference=ident,
        evidence_id='customer-x' if kind=='invoice' else 'supplier-source',note='Checked exact synthetic source.')


def create(ledger,record=None,revision=None,caller=None,ack=False):
    return create_record(ledger,record or entry(),revision or review_revision(ledger),ctx=caller or ctx(ledger),acknowledge_duplicates=ack)


def face(ledger,kind='invoice',ident='custom-x',total=50000,revision=None,caller=None,day=None):
    return adjust_face(ledger,kind,ident,total,day or ledger.as_of,'customer-x','Checked source credit/correction.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def cash(ledger,ident='fee-one',amount=1200,classification='bank_fee',day=None,revision=None,caller=None,ack=False):
    return record_cash_movement(ledger,ident,classification,'USD',amount,day or ledger.as_of,'supplier-source','Checked observed synthetic movement.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger),acknowledge_duplicates=ack)


def split_correct(ledger,total=100000,day=None,revision=None,caller=None):
    return correct_split_payment(ledger,'bulk-one',total,day or ledger.as_of,'customer-x','Source corrects whole amount; no executed refund.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def statement(ledger,rows=None,digest='b'*64):
    rows=rows or [dict(id='bank-one',date='2027-01-01',currency='USD',signed_minor=-1200,reference='Synthetic source fee')]
    return import_statement(ledger,rows,digest,'Synthetic statement source checked.',review_revision(ledger),ctx=ctx(ledger))['batch_id']


def match(ledger,bid,rid='bank-one',movement='other_cash:fee-one',status='matched',revision=None,caller=None):
    return review_statement_row(ledger,bid,rid,movement,status,'supplier-source','Checked source identity, amount and posting date.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_manual_entry_changes_only_claim_or_obligation_and_replays_after_restart(own,tmp_path,kind):
    record=entry(kind);before=own.actual_cash('USD');rev=review_revision(own);result=create(own,record,rev)
    collection=own.receivables if kind=='invoice' else own.outflows
    item=collection['manual-one'];assert item.remaining_minor==12000 and item.effective_total_minor==12000
    assert own.actual_cash('USD')==before and not own.receipts and not own.consent_map
    if kind=='bill':assert item.counterparty_consent_required_for_change and not item.protected
    else:assert item.due_date==date(2027,1,25) and item.expected_date==date(2027,1,20)
    db=tmp_path/'manual.sqlite';save(db,own);loaded=_load(db);state=copy.deepcopy(vars(loaded))
    assert create(loaded,record,rev)==dict(result,duplicate=True) and vars(loaded)==state
    with pytest.raises(AuthorizationError):create(loaded,record,rev,ctx(loaded,'agent'))


def test_manual_payroll_always_protected_and_unknown_invoice_dates_stay_unknown(own):
    record=entry('bill','new-payroll');record['category']='payroll';create(own,record)
    assert own.outflows['new-payroll'].protected
    with pytest.raises(CashflowError):face(own,'bill','new-payroll',0)
    record=entry('invoice','unknown-date');record.update(date=None,due_date=None);create(own,record)
    assert own.receivables['unknown-date'].expected_date is None and own.receivables['unknown-date'].due_date is None
    assert any('unknown' in p for p in input_problems(own))


@pytest.mark.parametrize('bad',['kind','id','existing','negative','bool','currency','source','note','date','category','protected_type','extra','agent','viewer','business','stale'])
def test_bad_manual_entry_is_atomic(own,bad):
    record=entry();revision=review_revision(own);caller=ctx(own)
    if bad=='kind':record['kind']='receipt'
    if bad=='id':record['id']='../file'
    if bad=='existing':record['id']='custom-x'
    if bad=='negative':record['amount_minor']=-1
    if bad=='bool':record['amount_minor']=True
    if bad=='currency':record['currency']='EUR'
    if bad=='source':record['evidence_id']='missing'
    if bad=='note':record['note']=' '
    if bad=='date':record['date']='2027-02-30'
    if bad=='category':record['category']='payroll'
    if bad=='protected_type':record['protected']='yes'
    if bad=='extra':record['approve']=True
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':revision='old'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):create(own,record,revision,caller)
    assert vars(own)==state


def test_new_different_id_duplicate_source_requires_owner_ack(own):
    record=entry();create(own,record)
    record['id']='alias-two'
    with pytest.raises(CashflowError):create(own,record)
    result=create(own,record,ack=True)
    assert result['possible_duplicate_ids']==['manual-one'] and len(own.entry_records)==2


def test_invoice_credit_keeps_original_and_cash_and_shows_overallocated_credit(own):
    receipt(own,amount=20000);cash_before=own.actual_cash('USD');original=copy.deepcopy(own.receipts)
    result=face(own,total=10000)
    rec=own.receivables['custom-x']
    assert rec.original_minor==65000 and rec.effective_total_minor==10000 and rec.remaining_minor==0 and rec.credit_balance_minor==10000
    assert result['credit_or_overpaid_minor']==10000 and own.actual_cash('USD')==cash_before and own.receipts==original
    assert any('credit' in p for p in input_problems(own))
    curve=forecast(own,own.as_of,30).currencies['USD'];assert curve.days[11].inflow_minor==0
    with pytest.raises(CashflowError):receipt(own,rid='over-credit',amount=1)
    # Increasing an allocation on an already overallocated invoice cannot use a clamped remaining balance.
    with pytest.raises(CashflowError):correct_receipt(own,'existing-r','custom-x',20001,own.as_of,'customer-x','Invalid.',review_revision(own),ctx=ctx(own))


def test_invoice_adjustment_invalidates_source_check_and_financial_revision(own):
    confirm_fact_check(own,'collection','custom-x','customer-x','Checked.',review_revision(own),ctx=ctx(own))
    before=_revision(own,'financial-inputs',[]);face(own,total=75000)
    assert own.receivables['custom-x'].original_minor==65000 and own.receivables['custom-x'].remaining_minor==75000
    assert before!=_revision(own,'financial-inputs',[]) and data_freshness(own,ctx=ctx(own))['rows'][1]['status']=='changed_since_check'


def test_paid_bill_cancellation_tracks_overpayment_without_refund_cash(own):
    record_bill_payment(own,'materials','bill-one',20000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    original=copy.deepcopy(own.outflows['materials'].observed_payments);before=own.actual_cash('USD')
    face(own,'bill','materials',0)
    out=own.outflows['materials'];assert out.amount_minor==50000 and out.effective_total_minor==0 and out.remaining_minor==0 and out.overpaid_minor==20000
    assert own.actual_cash('USD')==before and out.observed_payments==original and bill_schedule(out)==[]
    assert any('overpayment' in p for p in input_problems(own))
    assert not any(e['amount_minor']>0 for e in cash_events(own))
    with pytest.raises(CashflowError):record_bill_payment(own,'materials','another',1,own.as_of,'supplier-source','No balance.',review_revision(own),ctx=ctx(own))


def test_legacy_bill_amount_adjustment_freezes_original_payment_and_does_not_change_cash(own):
    own.mark_paid('materials',own.as_of,'owner','supplier-source',ctx=ctx(own));before=own.actual_cash('USD')
    face(own,'bill','materials',10000)
    out=own.outflows['materials'];assert out.legacy_payment['amount_minor']==50000 and out.paid_minor==50000
    assert out.overpaid_minor==40000 and own.actual_cash('USD')==before
    state=copy.deepcopy(vars(own));assert own.mark_paid('materials',own.as_of,'owner','supplier-source',ctx=ctx(own))['duplicate']
    assert vars(own)==state


def test_adjusted_bill_schedule_reduces_latest_portion_without_moving_dates_or_granting_consent(own):
    out=own.outflows['materials'];out.recorded_splits=[(date(2027,1,7),20000),(date(2027,1,14),30000)]
    record_bill_payment(own,'materials','partial',10000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    face(own,'bill','materials',35000)
    assert bill_schedule(out)==[(date(2027,1,7),10000),(date(2027,1,14),15000)] and not own.consent_map
    rows=forecast(own,own.as_of,30).currencies['USD'].days
    assert rows[6].outflow_minor==10000 and rows[13].outflow_minor==15000
    with pytest.raises(CashflowError):own.apply_recorded_amendment('materials','owner','supplier-source','owner_approved_deferral',splits=[(date(2027,1,20),50000)],ctx=ctx(own))


def test_unpaid_bill_cancel_reopen_and_mark_paid_uses_effective_total(own):
    out=own.outflows['materials'];face(own,'bill','materials',0)
    assert out.remaining_minor==0 and not list(cash_events(own))
    with pytest.raises(CashflowError):own.mark_paid('materials',own.as_of,'owner','supplier-source',ctx=ctx(own))
    face(own,'bill','materials',30000)
    own.mark_paid('materials',own.as_of,'owner','supplier-source',ctx=ctx(own))
    assert out.paid_minor==30000 and own.actual_cash('USD')==70000 and out.amount_minor==50000


@pytest.mark.parametrize('bad',['negative','bool','same','missing','protected','past','future','source','note','agent','viewer','business','stale'])
def test_bad_source_amount_adjustment_is_atomic(own,bad):
    args=['invoice','custom-x',50000,own.as_of,'customer-x','Checked.',review_revision(own)];caller=ctx(own)
    if bad=='negative':args[2]=-1
    if bad=='bool':args[2]=False
    if bad=='same':args[2]=65000
    if bad=='missing':args[1]='missing'
    if bad=='protected':args[0:3]=['bill','staff',0]
    if bad=='past':args[3]=own.as_of-timedelta(days=1)
    if bad=='future':args[3]=own.as_of+timedelta(days=1)
    if bad=='source':args[4]='missing'
    if bad=='note':args[5]=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':args[-1]='old'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):adjust_face(own,*args,ctx=caller)
    assert vars(own)==state


def test_whole_split_correction_after_rebase_is_signed_once_and_original_retry_cannot_revive(own,tmp_path):
    imported(own);rev=review_revision(own);initial=split(own,revision=rev);original=copy.deepcopy(own.split_receipts['bulk-one'])
    own.roll_forward(date(2027,1,2),ctx=ctx(own));revision=review_revision(own);result=split_correct(own,revision=revision)
    assert result['cash_delta_minor']==-20000 and own.actual_cash('USD')==200000 and own.split_receipts['bulk-one']==original
    assert split_unallocated(own,original)==40000
    own.roll_forward(date(2027,1,3),ctx=ctx(own));assert own.actual_cash('USD')==200000
    db=tmp_path/'whole.sqlite';save(db,own);loaded=_load(db);state=copy.deepcopy(vars(loaded))
    assert split_correct(loaded,day=date(2027,1,2),revision=revision)==dict(result,duplicate=True)
    assert split(loaded,revision=rev)==dict(initial,duplicate=True) and vars(loaded)==state
    with pytest.raises(AuthorizationError):split_correct(loaded,day=date(2027,1,2),revision=revision,caller=ctx(loaded,'agent'))


def test_whole_split_zero_requires_allocation_reversal_and_never_sends_refund(own):
    imported(own);result=split(own)
    with pytest.raises(CashflowError):split_correct(own,0)
    for rid in result['allocation_ids']:
        current=own.receipts[rid]
        correct_receipt(own,rid,current.receivable_id,0,own.as_of,'customer-x','Reverse allocation.',review_revision(own),ctx=ctx(own))
    split_correct(own,0)
    assert own.actual_cash('USD')==100000 and effective_split_payment(own,own.split_receipts['bulk-one'])['amount_minor']==0
    assert own.split_receipts['bulk-one']['amount_minor']==120000 and not observed_movements(own)
    assert own.receivables['custom-x'].remaining_minor==65000


def test_whole_correction_capacity_cannot_be_used_before_its_date(own):
    imported(own);result=split(own);split_correct(own,140000,day=date(2027,1,3))
    with pytest.raises(CashflowError):more(own,{'custom-x':1},day=date(2027,1,2))
    with pytest.raises(CashflowError):correct_receipt(own,result['allocation_ids'][0],'custom-x',40001,date(2027,1,2),'customer-x','Too early.',review_revision(own),ctx=ctx(own))
    more(own,{'custom-x':1},day=date(2027,1,3));assert own.actual_cash('USD',date(2027,1,3))==240000


@pytest.mark.parametrize('bad',['negative','bool','allocated','same','past','source','note','agent','viewer','business','stale'])
def test_bad_whole_split_correction_is_atomic(own,bad):
    imported(own);split(own)
    args=['bulk-one',100000,own.as_of,'customer-x','Checked.',review_revision(own)];caller=ctx(own)
    if bad=='negative':args[1]=-1
    if bad=='bool':args[1]=False
    if bad=='allocated':args[1]=59999
    if bad=='same':args[1]=120000
    if bad=='past':args[2]=date(2026,12,31)
    if bad=='source':args[3]='missing'
    if bad=='note':args[4]=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':args[-1]='old'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):correct_split_payment(own,*args,ctx=caller)
    assert vars(own)==state


@pytest.mark.parametrize('classification,sign',list(CASH_CLASSES.items()))
def test_other_observed_cash_classification_direction_forecast_and_replay(own,classification,sign):
    revision=review_revision(own);result=cash(own,classification=classification,revision=revision)
    assert own.actual_cash('USD')==100000+sign*1200 and not own.receipts and not own.actions
    events=list(cash_events(own));assert len(events)==1 and events[0]['kind']=='other_cash_observed' and events[0]['amount_minor']==sign*1200
    row=forecast(own,own.as_of,30).currencies['USD'].days[0]
    assert row.closing_minor==100000+sign*1200
    state=copy.deepcopy(vars(own));assert cash(own,classification=classification,revision=revision)==dict(result,duplicate=True) and vars(own)==state


def test_other_cash_correction_rebase_daily_and_forecast_actual(own):
    sid=save_forecast(own,'Before fee',review_revision(own),ctx=ctx(own))['snapshot_id'];cash(own)
    own.roll_forward(date(2027,1,2),ctx=ctx(own))
    original=copy.deepcopy(own.cash_movements['fee-one'])
    correct_cash_movement(own,'fee-one',1000,own.as_of,'supplier-source','Source amount corrected.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==99000 and own.cash_movements['fee-one']['signed_minor']==original['signed_minor']
    preview=daily_preview(own,date(2027,1,3),ctx=ctx(own));assert preview['accounts'][0]['other_cash_minor']==200
    compared=compare_forecast(own,sid,date(2027,1,2),ctx=ctx(own))
    assert compared['currencies']['USD'][0]['other_cash_minor']==-1200 and compared['currencies']['USD'][1]['other_cash_minor']==200
    assert compared['currencies']['USD'][1]['recorded_closing_minor']==99000
    own.roll_forward(date(2027,1,3),ctx=ctx(own));assert own.actual_cash('USD')==99000
    correct_cash_movement(own,'fee-one',0,own.as_of,'supplier-source','Accounting reversal only.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==100000 and not observed_movements(own)


@pytest.mark.parametrize('bad',['id','duplicate_id','class','negative','bool','currency','past','too_future','source','note','agent','viewer','business','stale'])
def test_bad_other_cash_is_atomic(own,bad):
    args=['fee-one','bank_fee','USD',1200,own.as_of,'supplier-source','Checked.',review_revision(own)];caller=ctx(own)
    if bad=='id':args[0]='../x'
    if bad=='duplicate_id':receipt(own,rid='fee-one');args[-1]=review_revision(own)
    if bad=='class':args[1]='sales'
    if bad=='negative':args[3]=-1
    if bad=='bool':args[3]=True
    if bad=='currency':args[2]='EUR'
    if bad=='past':args[4]=date(2026,12,31)
    if bad=='too_future':args[4]=date(2028,1,3)
    if bad=='source':args[5]='missing'
    if bad=='note':args[6]=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':args[-1]='old'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):record_cash_movement(own,*args,ctx=caller)
    assert vars(own)==state


def test_duplicate_checks_across_receipt_bill_and_other_cash_and_no_auto_deletion(own):
    receipt(own,rid='first',amount=1200)
    with pytest.raises(CashflowError):cash(own,'deposit',1200,'other_inflow')
    cash(own,'deposit',1200,'other_inflow',ack=True)
    pairs=payment_warnings(own,ctx=ctx(own,'viewer'));assert len(pairs)==1 and pairs[0]['status']=='distinct_acknowledged'
    assert own.actual_cash('USD')==102400
    cash(own,'fee-one',1200)
    with pytest.raises(CashflowError):record_bill_payment(own,'materials','new-pay',1200,own.as_of,'supplier-source','Checked.',review_revision(own),ctx=ctx(own))
    record_bill_payment(own,'materials','new-pay',1200,own.as_of,'supplier-source','Distinct.',review_revision(own),ctx=ctx(own),acknowledge_duplicates=True)
    assert own.actual_cash('USD')==100000 and len(payment_warnings(own,ctx=ctx(own)))==2


def test_different_id_receipt_duplicate_requires_ack_at_engine_boundary(own):
    receipt(own,rid='one',amount=1000)
    with pytest.raises(CashflowError):receipt(own,rid='two',amount=1000)
    receipt(own,rid='two',amount=1000,acknowledge_duplicates=True)
    assert len(own.receipts)==2 and own.actual_cash('USD')==102000
    pair=payment_warnings(own,ctx=ctx(own))[0];revision=review_revision(own)
    result=review_payment_pair(own,pair['id'],'possible_duplicate','customer-x','Need source correction.',revision,ctx=ctx(own))
    assert any('duplicate observed' in p for p in input_problems(own))
    with pytest.raises(CashflowError):plan_draft_preview(own,'auto-combined',ctx=ctx(own))
    review_payment_pair(own,pair['id'],'distinct','customer-x','Source confirms distinct.',review_revision(own),ctx=ctx(own))
    state=copy.deepcopy(vars(own));assert review_payment_pair(own,pair['id'],'possible_duplicate','customer-x','Need source correction.',revision,ctx=ctx(own))==dict(result,duplicate=True)
    assert vars(own)==state and own.actual_cash('USD')==102000


def test_split_children_and_accounting_deltas_are_not_duplicate_observations(own):
    imported(own);split(own,{'custom-x':20000,'custom-y':20000})
    assert len(observed_movements(own))==1 and not payment_warnings(own,ctx=ctx(own))
    split_correct(own,140000);assert len(observed_movements(own))==1


def test_statement_import_and_match_do_not_change_cash_receipts_actions_or_ledger_balances(own):
    cash(own);before=copy.deepcopy([own.accounts,own.receivables,own.outflows,own.receipts]);bid=statement(own)
    assert [own.accounts,own.receivables,own.outflows,own.receipts]==before
    financial=_revision(own,'financial-inputs',[])
    report=statement_report(own,bid,ctx=ctx(own,'viewer'));assert report['rows'][0]['status']=='unreviewed'
    assert report['rows'][0]['candidates'][0]['key']=='other_cash:fee-one'
    match(own,bid);report=statement_report(own,bid,ctx=ctx(own))
    assert report['rows'][0]['status']=='matched' and not report['ledger_movements_without_match']
    assert own.actual_cash('USD')==98800 and not own.receipts and not own.actions and _revision(own,'financial-inputs',[])==financial
    before=copy.deepcopy(vars(own));assert statement(own)==bid and vars(own)==before


def test_statement_match_invalidated_by_correction_and_original_retry_cannot_revive_after_unmatch(own):
    cash(own);bid=statement(own);rev=review_revision(own);result=match(own,bid,revision=rev)
    correct_cash_movement(own,'fee-one',1000,own.as_of,'supplier-source','Corrected.',review_revision(own),ctx=ctx(own))
    assert statement_report(own,bid,ctx=ctx(own))['rows'][0]['status']=='needs_review'
    match(own,bid,movement='',status='unmatched')
    state=copy.deepcopy(vars(own));assert match(own,bid,revision=rev)==dict(result,duplicate=True)
    assert vars(own)==state and statement_report(own,bid,ctx=ctx(own))['rows'][0]['status']=='unmatched'


def test_statement_same_movement_cannot_match_two_rows_or_files(own):
    cash(own);bid=statement(own);match(own,bid)
    second=statement(own,[dict(id='alias-bank',date='2027-01-01',currency='USD',signed_minor=-1200,reference='Same source row')],digest='c'*64)
    with pytest.raises(CashflowError):match(own,second,rid='alias-bank')
    match(own,bid,movement='',status='excluded');match(own,second,rid='alias-bank')
    assert own.actual_cash('USD')==98800


def test_statement_matches_effective_observation_not_accounting_delta_or_child(own):
    imported(own);result=split(own);split_correct(own,100000)
    bid=statement(own,[dict(id='bank-split',date='2027-01-02',currency='USD',signed_minor=100000,reference='Original corrected total')])
    assert [r['key'] for r in statement_report(own,bid,ctx=ctx(own))['rows'][0]['candidates']]==['split_payment:bulk-one']
    match(own,bid,'bank-split','split_payment:bulk-one')
    for fake in ['receipt:'+result['allocation_ids'][0],'receipt_correction:fake']:
        with pytest.raises(CashflowError):match(own,bid,'bank-split',fake)
    assert own.actual_cash('USD')==200000


@pytest.mark.parametrize('bad',['amount','currency','date','source','note','missing','agent','viewer','business','stale','disposition'])
def test_bad_statement_match_is_atomic(own,bad):
    cash(own);bid=statement(own);args=[bid,'bank-one','other_cash:fee-one','matched','supplier-source','Checked.',review_revision(own)];caller=ctx(own)
    if bad in {'amount','currency','date'}:
        row=own.statement_batches[bid]['rows'][0]
        if bad=='amount':row['signed_minor']=-1201
        if bad=='currency':row['currency']='EUR'
        if bad=='date':row['date']='2027-01-09'
        args[-1]=review_revision(own)
    if bad=='source':args[4]='missing'
    if bad=='note':args[5]=' '
    if bad=='missing':args[1]='missing'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':args[-1]='old'
    if bad=='disposition':args[3]='approve'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):review_statement_row(own,*args,ctx=caller)
    assert vars(own)==state


@pytest.mark.parametrize('bad',['empty','duplicate_ids','amount','bool','currency','date','extra','digest','note','agent','viewer','business','stale'])
def test_bad_statement_import_is_atomic(own,bad):
    rows=[dict(id='bank-one',date='2027-01-01',currency='USD',signed_minor=-1200,reference='Source')]
    digest='b'*64;note='Checked.';revision=review_revision(own);caller=ctx(own)
    if bad=='empty':rows=[]
    if bad=='duplicate_ids':rows=rows*2
    if bad=='amount':rows[0]['signed_minor']=0
    if bad=='bool':rows[0]['signed_minor']=True
    if bad=='currency':rows[0]['currency']='EUR'
    if bad=='date':rows[0]['date']='bad'
    if bad=='extra':rows[0]['approve']=True
    if bad=='digest':digest='../file'
    if bad=='note':note=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':revision='old'
    state=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):import_statement(own,rows,digest,note,revision,ctx=caller)
    assert vars(own)==state


def test_new_metadata_roundtrip_backup_restore_and_exports_keep_originals(own,tmp_path):
    create(own);face(own);cash(own);imported(own);split(own);split_correct(own);bid=statement(own);match(own,bid)
    db=tmp_path/'complete.sqlite';save(db,own);loaded=_load(db)
    assert loaded.entry_records and loaded.receivables['custom-x'].face_adjustments and loaded.cash_movements and loaded.split_adjustments and loaded.statement_reviews
    assert loaded.actual_cash('USD')==198800
    report=report_tables(loaded,ctx=ctx(loaded,'viewer'))
    invoices=report['tables'][2]
    assert invoices['rows'][0][invoices['headers'].index('Effective invoice total')].minor==50000 and report['tables'][0]['rows'][0][2].minor==198800
    backup=recovery.create_backup(db,'All seven owner workflows',ctx=ctx(loaded))
    face(loaded,total=45000);save(db,loaded)
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(loaded));assert preview['restored']['statement_batches']==1
    recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    assert _load(db).receivables['custom-x'].effective_total_minor==50000 and _load(db).statement_reviews


def test_http_all_pages_preview_no_mutation_confirmation_csrf_and_extra_fields(live_server,own):
    base,db=live_server;save(db,own);state=copy.deepcopy(vars(_load(db)))
    for path in ['/entries','/invoice-adjustments','/bill-adjustments','/split-payment-corrections','/cash-movements','/statements','/payment-warnings']:
        status,html=_get(base,path);assert status==200 and 'SYNTHETIC DEMO' in html
    assert vars(_load(db))==state
    token=re.search("name='csrf' value='([^']+)'",_get(base,'/entries')[1]).group(1)
    fields=dict(csrf=token,operation='manual_record',revision=review_revision(own),kind='invoice',id='http-one',currency='USD',amount='120.00',
        date='2027-01-20',due_date='',category='invoice',counterparty='<script>unsafe</script>',invoice_reference='HTTP-1',evidence_id='customer-x',note='Checked source.')
    status,html=_post(base,'/owner-entries/preview',fields);assert status==200 and '&lt;script&gt;unsafe' in html and vars(_load(db))==state
    assert _post(base,'/owner-entries/confirm',fields)[0]==400
    fields['confirmed']='yes';assert _post(base,'/owner-entries/confirm',dict(fields,csrf='wrong'))[0]==403
    assert _post(base,'/owner-entries/confirm',dict(fields,role='owner'))[0]==400
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _: _post(base,'/owner-entries/confirm',fields)[0],range(3)))
    assert results==[200]*3 and len(_load(db).entry_records)==1 and _load(db).actual_cash('USD')==100000


def test_model_tools_remain_read_and_draft_only_with_new_source_fields(own):
    face(own);cash(own);assert len(TOOLS)==8
    result=execute_tool(ctx(own,'agent'),own,'get_evidence',{})['result']
    assert result['receivables'][0]['original_minor']==65000 and result['receivables'][0]['effective_total_minor']==50000
    assert result['other_observed_cash'][0]['classification']=='bank_fee'


def test_multiple_whole_corrections_keep_sequence_across_restarts_and_reordered_metadata(own,tmp_path):
    imported(own);split(own);db=tmp_path/'sequence.sqlite'
    original_ops=[]
    for amount in [100000,130000,90000]:
        revision=review_revision(own);result=split_correct(own,amount,revision=revision)
        original_ops.append((amount,revision,result));save(db,own);own=_load(db)
        assert effective_split_payment(own,own.split_receipts['bulk-one'])['amount_minor']==amount
        assert split_unallocated(own,own.split_receipts['bulk-one'])==amount-60000
        assert own.actual_cash('USD')==100000+amount
    own.split_adjustments=dict(reversed(list(own.split_adjustments.items())))
    assert effective_split_payment(own,own.split_receipts['bulk-one'])['amount_minor']==90000
    save(db,own);loaded=_load(db);before=copy.deepcopy(vars(loaded))
    for amount,revision,result in original_ops:
        assert split_correct(loaded,amount,revision=revision)==dict(result,duplicate=True)
    assert vars(loaded)==before and split_unallocated(loaded,loaded.split_receipts['bulk-one'])==30000


def test_receipt_csv_preview_flags_cross_path_and_within_file_movement_duplicates(own):
    from cashflow_copilot.operations import csv_duplicate_warnings
    cash(own,'capital',1200,'owner_capital')
    rows=[dict(record_type='receipt',reference=rid,currency='USD',amount_minor=1200,date='2027-01-01',
        counterparty='custom-x',invoice_reference='',will_skip=False) for rid in ['csv-one','csv-two']]
    preview=csv_duplicate_warnings(own,rows)
    assert [w['id'] for w in preview[0]['possible_duplicates']]==['other_cash:capital']
    assert {w['id'] for w in preview[1]['possible_duplicates']}=={'other_cash:capital','preview-receipt:csv-one'}


def test_credit_and_overpayment_priorities_open_the_correct_owner_review(own):
    from cashflow_copilot.attention import attention_items
    receipt(own,amount=20000);face(own,total=10000)
    record_bill_payment(own,'materials','paid-source',20000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    face(own,'bill','materials',0)
    items=attention_items(own,ctx=ctx(own,'viewer'))
    assert any(i['href']=='/credits' and i['target']=='custom-x' for i in items)
    assert any(i['href']=='/credits' and i['target']=='materials' for i in items)
