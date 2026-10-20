"""Independent money, authority, correction and HTTP controls for v0.13."""
import copy
from datetime import date,timedelta
import json,re
from concurrent.futures import ThreadPoolExecutor
import pytest

from cashflow_copilot import recovery
from cashflow_copilot.accounting import record_bill_payment,correct_bill_payment,correct_receipt
from cashflow_copilot.agent import TOOLS,_tool_get_evidence
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.completion import adjust_face,review_statement_row
from cashflow_copilot.errors import CashflowError,AuthorizationError
from cashflow_copilot.exploration import search_records,cash_calendar
from cashflow_copilot.extension import record_refund,correct_refund,correct_payment_date
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account,Outflow,Receivable,Plan,OutflowAmendment
from cashflow_copilot.obligations import (check_statement_balance,statement_balance_report,carry_credit,
    reverse_credit,register_loan,loan_report,void_loan_registration)
from cashflow_copilot.operations import item_identity
from cashflow_copilot.owner_workflows import review_revision,daily_preview,close_day
from cashflow_copilot.planning import _hash
from cashflow_copilot.reports import report_tables
from cashflow_copilot.settlement import cash_events,bill_schedule,bill_payment_entries
from cashflow_copilot.workbench import party_statement
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx,save,receipt
from tests.test_completion import statement,cash
from tests.test_extension import credit
from tests.test_web import live_server,_get,_post,_load


def state(l):return _hash(vars(l))
def cdata(l,kind='invoice'):
    credit(l,kind)
    group=l.receivables if kind=='invoice' else l.outflows
    source,target=('custom-x','custom-y') if kind=='invoice' else ('materials','overhead')
    group[source].counterparty=group[target].counterparty='Synthetic Party'
    return source,target
def carry(l,kind='invoice',amount=7000,ident='carry-one',revision=None,caller=None,source=None,target=None):
    return carry_credit(l,ident,kind,source or ('custom-x' if kind=='invoice' else 'materials'),target or ('custom-y' if kind=='invoice' else 'overhead'),amount,
        'customer-x','Checked source-backed application.',revision or review_revision(l),ctx=caller or ctx(l))
def undo(l,ident='carry-one',revision=None,caller=None):
    return reverse_credit(l,ident,'customer-x','Reverse erroneous application.',revision or review_revision(l),ctx=caller or ctx(l))
def spec():
    return dict(id='loan-one',lender='Synthetic Lender',reference='LOAN-001',currency='USD',opening_principal_minor=10000,
        schedule=[dict(date='2027-01-10',principal_minor=5000,interest_minor=1000),dict(date='2027-02-10',principal_minor=5000,interest_minor=500)],
        evidence_id='supplier-source',note='Existing source-backed debt and remaining terms checked.',already_in_opening=True,not_recorded_elsewhere=True)
def loan(l,s=None,revision=None,caller=None):return register_loan(l,s or spec(),revision or review_revision(l),ctx=caller or ctx(l))
def bounds(l,bid,ident='check-one',start=None,end=None,opening=100000,closing=110000,previous='',revision=None,caller=None):
    return check_statement_balance(l,ident,bid,'operating',start or l.as_of,end or l.as_of,opening,closing,previous,
        'customer-x','Checked statement boundary source.',revision or review_revision(l),ctx=caller or ctx(l))
def bdata(l):
    receipt(l);bid=statement(l,[dict(id='bank-one',currency='USD',signed_minor=10000,date='2027-01-01',reference='Observed receipt')])
    review_statement_row(l,bid,'bank-one','receipt:existing-r','matched','customer-x','Checked row.',review_revision(l),ctx=ctx(l))
    return bid


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_credit_is_non_cash_settlement_and_persists_replays_and_reverses(own,tmp_path,kind):
    source,target=cdata(own,kind);before=own.actual_cash('USD');events=copy.deepcopy(list(cash_events(own)))
    group=own.receivables if kind=='invoice' else own.outflows;target_before=group[target].remaining_minor
    rev=review_revision(own);result=carry(own,kind)
    assert group[target].remaining_minor==target_before-7000
    assert (group[source].credit_balance_minor if kind=='invoice' else group[source].overpaid_minor)==3000
    assert list(cash_events(own))==events and own.actual_cash('USD')==before and not own.actions and not own.consent_map
    statement_view=party_statement(own,'customer' if kind=='invoice' else 'supplier','Synthetic Party',ctx=ctx(own))
    assert len([r for r in statement_view['rows'] if r['kind']=='credit_transfer'])==2
    db=tmp_path/'credit.sqlite';save(db,own);loaded=_load(db);snapshot=state(loaded)
    assert carry(loaded,kind,revision=rev)==dict(result,duplicate=True) and state(loaded)==snapshot
    undo(loaded);assert loaded.actual_cash('USD')==before
    g=loaded.receivables if kind=='invoice' else loaded.outflows
    assert g[target].remaining_minor==target_before and (g[source].credit_balance_minor if kind=='invoice' else g[source].overpaid_minor)==10000
    after=state(loaded);carry(loaded,kind,revision=rev);assert state(loaded)==after and loaded.credit_transfers['carry-one']['reversed']


def test_credit_only_bill_does_not_fabricate_legacy_paid_cash(own,tmp_path):
    cdata(own,'bill');own.outflows['overhead'].amount_minor=10000
    events=list(cash_events(own));carry(own,'bill',10000)
    bill=own.outflows['overhead'];assert bill.paid_minor==10000 and bill.remaining_minor==0
    assert bill_payment_entries(bill)==[] and list(cash_events(own))==events and bill.status=='adjusted'
    db=tmp_path/'credit-only.sqlite';save(db,own);l=_load(db)
    assert l.outflows['overhead'].paid_minor==10000 and list(cash_events(l))==events
    undo(l);assert l.outflows['overhead'].paid_minor==0 and l.outflows['overhead'].remaining_minor==10000


@pytest.mark.parametrize('bad',['party','blank_party','currency','same_record','capacity','source_capacity','negative','bool','unknown','role','protected','held','future','stale','evidence'])
def test_invalid_credit_is_atomic(own,bad):
    source,target=cdata(own);amount=7000;kind='invoice';caller=ctx(own);rev=None
    if bad=='party':own.receivables[target].counterparty='Other'
    if bad=='blank_party':own.receivables[source].counterparty=own.receivables[target].counterparty=''
    if bad=='currency':own.accounts['eur']=Account('eur','EUR',0,0);own.receivables[target].currency='EUR'
    if bad=='same_record':target=source
    if bad=='capacity':own.receivables[target].original_minor=5000
    if bad=='source_capacity':amount=10001
    if bad=='negative':amount=-1
    if bad=='bool':amount=True
    if bad=='unknown':target='missing'
    if bad=='role':kind='mixed'
    if bad=='protected':source,target=cdata(own,'bill');kind='bill';own.outflows[target].protected=True
    if bad=='held':own.receivables[target].collection_hold=dict(active=True,evidence_id='customer-x')
    if bad=='future':own.receivables[source].allocations[0].date_received+=timedelta(days=1)
    if bad=='stale':rev='old'
    if bad=='evidence':del own.evidence['customer-x']
    before=state(own)
    with pytest.raises(CashflowError):carry(own,kind,amount,revision=rev,caller=caller,source=source,target=target)
    assert state(own)==before


@pytest.mark.parametrize('kind',['invoice','bill'])
def test_active_credit_freezes_dependent_source_refund_identity_and_payment_corrections(own,kind):
    source,target=cdata(own,kind);carry(own,kind);before=state(own)
    operations=[lambda:adjust_face(own,kind,source,9000,own.as_of,'customer-x','Correct source.',review_revision(own),ctx=ctx(own)),
        lambda:item_identity(own,kind,target,'Changed party','REF','customer-x','Change identity.',review_revision(own),ctx=ctx(own)),
        lambda:record_refund(own,kind,source,'return',1000,own.as_of,'customer-x','Observed return.',review_revision(own),ctx=ctx(own))]
    if kind=='invoice':
        operations+=[lambda:correct_receipt(own,'existing-r',source,19000,own.as_of,'customer-x','Correct receipt.',review_revision(own),ctx=ctx(own)),
            lambda:correct_payment_date(own,'receipt:existing-r',own.as_of+timedelta(days=1),'customer-x','Correct date.',review_revision(own),ctx=ctx(own))]
    else:operations+=[lambda:correct_bill_payment(own,source,'paid-one',19000,own.as_of,'customer-x','Correct payment.',review_revision(own),ctx=ctx(own))]
    for op in operations:
        with pytest.raises(CashflowError,match='carried credit'):op()
        assert state(own)==before
    undo(own)
    adjust_face(own,kind,source,9000,own.as_of,'customer-x','Correct after unwind.',review_revision(own),ctx=ctx(own))


def test_existing_refund_correction_cannot_consume_carried_credit(own):
    cdata(own);record_refund(own,'invoice','custom-x','returned',1000,own.as_of,'customer-x','Observed.',review_revision(own),ctx=ctx(own))
    carry(own,amount=7000);before=state(own)
    with pytest.raises(CashflowError,match='carried credit'):
        correct_refund(own,'returned',2000,own.as_of,'customer-x','Correct return.',review_revision(own),ctx=ctx(own))
    assert state(own)==before


def test_credit_chain_requires_later_dependent_reversal_first(own):
    cdata(own);carry(own,amount=5000);carry(own,amount=3000,ident='carry-two');before=state(own)
    with pytest.raises(CashflowError,match='later dependent'):undo(own)
    assert state(own)==before
    undo(own,'carry-two');undo(own)
    assert own.receivables['custom-x'].credit_balance_minor==10000


def test_statement_arithmetic_cash_matching_and_continuity_are_separate(own):
    bid=bdata(own);events=list(cash_events(own));bounds(own,bid)
    first=statement_balance_report(own,'check-one',ctx=ctx(own))
    assert first['arithmetic_delta_minor']==0 and first['opening_delta_minor']==0 and first['closing_delta_minor']==0
    assert first['unresolved_row_ids']==[] and first['status']=='needs_review' and 'No preceding' in ' '.join(first['problems'])
    cash(own,amount=1000,day=own.as_of+timedelta(days=1))
    bid2=statement(own,[dict(id='bank-fee',currency='USD',signed_minor=-1000,date='2027-01-02',reference='Fee')],digest='c'*64)
    review_statement_row(own,bid2,'bank-fee','other_cash:fee-one','matched','supplier-source','Checked.',review_revision(own),ctx=ctx(own))
    bounds(own,bid2,'check-two',start=date(2027,1,2),end=date(2027,1,2),opening=110000,closing=109000,previous='check-one')
    second=statement_balance_report(own,'check-two',ctx=ctx(own))
    assert second['status']=='checks_aligned' and second['previous_delta_minor']==0
    assert own.actual_cash('USD',date(2027,1,2))==109000 and len(list(cash_events(own)))==len(events)+1 and not own.actions
    bounds(own,bid,'check-corrected',closing=110001)
    assert 'superseded' in ' '.join(statement_balance_report(own,'check-two',ctx=ctx(own))['problems'])


@pytest.mark.parametrize('issue',['arithmetic','ledger_open','unmatched','overlap','stale_match','historical'])
def test_balanced_statement_cannot_hide_issues(own,issue):
    bid=bdata(own);closing=110000;opening=100000
    if issue=='arithmetic':closing+=1
    if issue=='ledger_open':opening+=500;closing+=500
    if issue=='unmatched':own.statement_reviews.clear()
    if issue=='stale_match':correct_receipt(own,'existing-r','custom-x',9000,own.as_of,'customer-x','Correction.',review_revision(own),ctx=ctx(own))
    bounds(own,bid,opening=opening,closing=closing)
    if issue=='overlap':
        bid2=statement(own,[dict(id='other-bank',currency='USD',signed_minor=10000,date='2027-01-01',reference='Overlap')],digest='d'*64)
        bounds(own,bid2,'overlap')
    if issue=='historical':own.roll_forward(own.as_of+timedelta(days=1),ctx=ctx(own))
    r=statement_balance_report(own,'check-one',ctx=ctx(own));assert r['status']=='needs_review'
    if issue=='arithmetic':assert r['arithmetic_delta_minor']==1
    if issue=='ledger_open':assert r['opening_delta_minor']==500 and r['arithmetic_delta_minor']==0
    if issue in {'unmatched','stale_match'}:assert r['unresolved_row_ids']==['bank-one']
    if issue=='overlap':assert r['overlap_check_ids']==['overlap']
    if issue=='historical':assert r['ledger_opening_minor'] is None


@pytest.mark.parametrize('bad',['date','period','outside','currency','account','opening_bool','closing_big','previous','evidence','stale','id'])
def test_invalid_statement_boundary_is_atomic(own,bad):
    bid=bdata(own);args=dict(ident='check-one',start=own.as_of,end=own.as_of,opening=100000,closing=110000)
    if bad=='date':args['start']='2027-01-01'
    if bad=='period':args['end']=own.as_of-timedelta(days=1)
    if bad=='outside':args['end']=own.as_of+timedelta(days=367)
    if bad=='currency':own.statement_batches[bid]['rows'][0]['currency']='EUR'
    if bad=='account':own.accounts.clear()
    if bad=='opening_bool':args['opening']=True
    if bad=='closing_big':args['closing']=10**15+1
    if bad=='previous':args['previous']='missing'
    if bad=='evidence':del own.evidence['customer-x']
    if bad=='stale':args['revision']='old'
    if bad=='id':args['ident']='../x'
    before=state(own)
    with pytest.raises(CashflowError):bounds(own,bid,**args)
    assert state(own)==before


def test_loan_forecast_payment_correction_and_cash_are_counted_once(own,tmp_path):
    before=own.actual_cash('USD');baseline=forecast(own,own.as_of,30).currencies['USD'].closing_minor
    rev=review_revision(own);result=loan(own);assert own.actual_cash('USD')==before and not own.actions and not own.consent_map
    assert forecast(own,own.as_of,30).currencies['USD'].closing_minor==baseline-6000
    assert all(own.outflows[k].protected for k in result['bill_ids'])
    assert loan_report(own,ctx=ctx(own))[0]['outstanding_principal_minor']==10000
    bill=result['bill_ids'][0]
    record_bill_payment(own,bill,'emi-paid',3000,own.as_of,'supplier-source','Observed partial instalment.',review_revision(own),ctx=ctx(own))
    r=loan_report(own,ctx=ctx(own))[0]
    assert (r['outstanding_principal_minor'],r['unpaid_interest_minor'],r['unpaid_total_minor'])==(8000,500,8500)
    assert own.actual_cash('USD')==before-3000 and bill_schedule(own.outflows[bill])==[(date(2027,1,10),3000)]
    assert forecast(own,own.as_of,30).currencies['USD'].closing_minor==baseline-6000
    correct_bill_payment(own,bill,'emi-paid',500,own.as_of,'supplier-source','Observed amount correction.',review_revision(own),ctx=ctx(own))
    r=loan_report(own,ctx=ctx(own))[0]
    assert r['outstanding_principal_minor']==10000 and r['unpaid_interest_minor']==1000 and own.actual_cash('USD')==before-500
    db=tmp_path/'loan.sqlite';save(db,own);loaded=_load(db);snapshot=state(loaded)
    assert loan(loaded,revision=rev)==dict(result,duplicate=True) and state(loaded)==snapshot
    assert loan_report(loaded,ctx=ctx(loaded))==loan_report(own,ctx=ctx(own))
    cal=cash_calendar(own,ctx=ctx(own));assert any('loan-one' in str(r['events']) for r in cal['rows'])


@pytest.mark.parametrize('bad',['id','id_long','bool','zero','unknown_currency','lender','reference','source','opening_ack','duplicate_ack','extra','no_rows','row_extra','row_date','old_date','far_date','duplicate_date','order','principal_sum','negative_interest','bool_interest','zero_row','large_row','stale','duplicate_reference'])
def test_invalid_loan_is_atomic(own,bad):
    s=spec();rev=None
    if bad=='id':s['id']='../loan'
    if bad=='id_long':s['id']='a'*41
    if bad=='bool':s['opening_principal_minor']=True
    if bad=='zero':s['opening_principal_minor']=0
    if bad=='unknown_currency':s['currency']='EUR'
    if bad=='lender':s['lender']=' '
    if bad=='reference':s['reference']=' '
    if bad=='source':s['evidence_id']='absent'
    if bad=='opening_ack':s['already_in_opening']=False
    if bad=='duplicate_ack':s['not_recorded_elsewhere']=False
    if bad=='extra':s['execute']=True
    if bad=='no_rows':s['schedule']=[]
    if bad=='row_extra':s['schedule'][0]['approve']=True
    if bad=='row_date':s['schedule'][0]['date']='2027-02-30'
    if bad=='old_date':s['schedule'][0]['date']='2026-12-31'
    if bad=='far_date':s['schedule'][1]['date']='2039-01-01'
    if bad=='duplicate_date':s['schedule'][1]['date']=s['schedule'][0]['date']
    if bad=='order':s['schedule'].reverse()
    if bad=='principal_sum':s['schedule'][0]['principal_minor']=4999
    if bad=='negative_interest':s['schedule'][0]['interest_minor']=-1
    if bad=='bool_interest':s['schedule'][0]['interest_minor']=True
    if bad=='zero_row':s['schedule'][0].update(principal_minor=0,interest_minor=0)
    if bad=='large_row':s['schedule'][0]['interest_minor']=10**15
    if bad=='stale':rev='old'
    if bad=='duplicate_reference':own.outflows['materials'].counterparty=s['lender'];own.outflows['materials'].invoice_reference=s['reference']
    before=state(own)
    with pytest.raises(CashflowError):loan(own,s,revision=rev)
    assert state(own)==before


def test_loan_schedule_cannot_be_deflected_or_identity_edited_by_generic_flows(own):
    loan(own);bill='loan-loan-one-1';before=state(own)
    for op in [lambda:adjust_face(own,'bill',bill,1,own.as_of,'supplier-source','Reduce debt.',review_revision(own),ctx=ctx(own)),
        lambda:item_identity(own,'bill',bill,'Changed','Changed','supplier-source','Change lender.',review_revision(own),ctx=ctx(own)),
        lambda:forecast(own,own.as_of,30,Plan('bad','bad','bad',amendments=[OutflowAmendment(bill,own.as_of+timedelta(days=60))]))]:
        with pytest.raises(CashflowError):op()
        assert state(own)==before


def test_erroneous_unpaid_loan_can_be_voided_without_erasing_history_or_reviving_on_retry(own):
    rev=review_revision(own);baseline=forecast(own,own.as_of,30).currencies['USD'].closing_minor;loan(own)
    void_loan_registration(own,'loan-one','supplier-source','Duplicate schedule registration; checked source.',review_revision(own),ctx=ctx(own))
    assert forecast(own,own.as_of,30).currencies['USD'].closing_minor==baseline
    assert own.outflows['loan-loan-one-1'].protected and own.outflows['loan-loan-one-1'].amount_minor==6000
    assert own.outflows['loan-loan-one-1'].effective_total_minor==0 and not list(cash_events(own))
    after=state(own);loan(own,revision=rev);assert state(own)==after and own.loans['loan-one']['voided']
    corrected=spec();corrected['id']='corrected-loan';loan(own,corrected)
    assert loan_report(own,ctx=ctx(own))[0]['outstanding_principal_minor']==0
    assert forecast(own,own.as_of,30).currencies['USD'].closing_minor==baseline-6000


def test_paid_loan_registration_cannot_be_voided_even_after_payment_reversal(own):
    loan(own);bill='loan-loan-one-1'
    record_bill_payment(own,bill,'emi-paid',1000,own.as_of,'supplier-source','Observed.',review_revision(own),ctx=ctx(own))
    correct_bill_payment(own,bill,'emi-paid',0,own.as_of,'supplier-source','Reverse erroneous observation.',review_revision(own),ctx=ctx(own))
    before=state(own)
    with pytest.raises(CashflowError,match='payment history'):
        void_loan_registration(own,'loan-one','supplier-source','Attempt.',review_revision(own),ctx=ctx(own))
    assert state(own)==before


def test_future_loan_observation_and_correction_do_not_change_current_principal(own):
    loan(own);bill='loan-loan-one-1'
    record_bill_payment(own,bill,'future-emi',3000,date(2027,1,10),'supplier-source','Future-dated synthetic observation.',review_revision(own),ctx=ctx(own))
    r=loan_report(own,ctx=ctx(own))[0]
    assert own.actual_cash('USD')==100000 and r['outstanding_principal_minor']==10000 and r['unpaid_interest_minor']==1500
    assert r['rows'][0]['paid_minor']==3000 and r['rows'][0]['paid_through_snapshot_minor']==0
    own.roll_forward(date(2027,1,10),ctx=ctx(own))
    correct_bill_payment(own,bill,'future-emi',500,date(2027,1,11),'supplier-source','Future correction date.',review_revision(own),ctx=ctx(own))
    r=loan_report(own,ctx=ctx(own))[0]
    assert r['outstanding_principal_minor']==8000 and r['rows'][0]['paid_minor']==500 and r['rows'][0]['paid_through_snapshot_minor']==3000


@pytest.mark.parametrize('role',['agent','viewer','wrong_business'])
def test_loan_void_requires_owner_of_business(own,role):
    loan(own);before=state(own)
    caller=ctx(own,business='other') if role=='wrong_business' else ctx(own,role)
    with pytest.raises(AuthorizationError):
        void_loan_registration(own,'loan-one','supplier-source','Checked.',review_revision(own),ctx=caller)
    assert state(own)==before


@pytest.mark.parametrize('operation',['loan','carry','statement','reverse'])
@pytest.mark.parametrize('caller',['agent','viewer','business'])
def test_owner_business_boundary_precedes_replay(own,operation,caller):
    cdata(own);bid=statement(own);rev=review_revision(own)
    # bdata adds another distinct receipt; acknowledge its possible duplicate only if needed.
    if operation in {'carry','reverse'}:carry(own)
    elif operation=='loan':loan(own)
    else:bounds(own,bid,closing=130000)
    bad=ctx(own,caller) if caller!='business' else ctx(own,business='other')
    before=state(own)
    with pytest.raises(AuthorizationError):
        if operation=='carry':carry(own,revision=rev,caller=bad)
        elif operation=='reverse':undo(own,caller=bad)
        elif operation=='loan':loan(own,revision=rev,caller=bad)
        else:bounds(own,bid,closing=130000,revision=rev,caller=bad)
    assert state(own)==before


def test_new_facts_reach_audit_search_model_read_and_six_financial_tables(own,tmp_path):
    cdata(own);carry(own);loan(own);bid=statement(own);bounds(own,bid)
    for kind,term in [('credit','carry-one'),('loan','loan-one'),('statement_balance','check-one')]:
        assert search_records(own,term,kind=kind,ctx=ctx(own))['total']==1
    assert len(TOOLS)==8
    result=_tool_get_evidence(ctx(own,'agent'),own,{},None)
    assert result['credit_transfers'] and result['loan_register'] and result['statement_balance_terms']
    tables=report_tables(own,ctx=ctx(own))['tables'];assert len(tables)==6
    for table in tables:
        assert all(len(row)==len(table['headers']) for row in table['rows'])
    db=tmp_path/'all.sqlite';save(db,own)
    backup=recovery.create_backup(db,'All three workflows',ctx=ctx(own));_,_,restored=recovery.checked_backup(db,backup['id'])
    assert state(restored)==state(own)


def test_http_preview_confirm_stale_duplicates_and_restart(own,live_server):
    base,db=live_server;cdata(own);save(db,own)
    _,html=_get(base,'/credits');csrf=re.search("name='csrf' value='([^']+)'",html)[1]
    fields=dict(csrf=csrf,operation='carry',revision=review_revision(own),transfer_id='carry-ui',kind='invoice',source_id='custom-x',target_id='custom-y',amount='70.00',evidence_id='customer-x',note='Source checked.')
    before=state(_load(db));status,html=_post(base,'/obligations/preview',fields)
    assert status==200 and 'No saved state changed' in html and state(_load(db))==before
    assert _post(base,'/obligations/confirm',fields)[0]==400
    fields['confirmed']='yes'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:_post(base,'/obligations/confirm',fields)[0],range(2)))
    assert results==[200,200]
    loaded=_load(db);assert len(loaded.credit_transfers)==1 and loaded.receivables['custom-y'].remaining_minor==38000
    after=state(loaded);assert _post(base,'/obligations/confirm',fields)[0]==200 and state(_load(db))==after
    assert _post(base,'/obligations/confirm',dict(fields,transfer_id='stale-id'))[0]==400
    for path in ('/entries','/statement-balances','/credits','/loans'):
        assert _get(base,path)[0]==200
    assert 'Statement balance checks' in _get(base,'/entries')[1]
    assert _post(base,'/obligations/preview',dict(fields,unexpected='approve'))[0]==400
