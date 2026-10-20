"""Independent amounts from reproduced live failures; no external provider."""
import copy,json
import pytest
from cashflow_copilot.agent import execute_tool
from cashflow_copilot.agent_disclosure import outbound_filter
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.models import Account
from tools.live_cases import case_ledger

def evidence(case):
    ledger=case_ledger(case);before=copy.deepcopy(vars(ledger))
    result=execute_tool(RequestContext('agent','agent',ledger.business_id),ledger,'get_evidence',{})
    assert {k:v for k,v in vars(ledger).items() if k!='audit'}=={k:v for k,v in before.items() if k!='audit'}
    return ledger,result

def row(result,ident,kind):
    return next(r for r in result['result']['financial_review']['rows'] if r['id']==ident and r['record_type']==kind)

def test_supplier_cash_200_credit_out_100_settles_adjusted_100_without_missing_cash():
    ledger,result=evidence('credit_supplier');m=row(result,'materials','bill')['values'];o=row(result,'ops','bill')['values']
    assert m['Original face value']=='USD 500.00' and m['Current effective obligation']=='USD 100.00'
    assert m['Net recorded payment settlement, all recorded dates']=='USD 200.00'
    assert m['Net noncash credit applied (negative means carried out)']=='USD -100.00'
    assert m['Total settlement including noncash credit']=='USD 100.00'
    assert m['Remaining obligation']=='USD 0.00' and m['Unpaid residual schedule']=='None'
    assert o['Net recorded payment settlement, all recorded dates']=='USD 0.00'
    assert o['Total settlement including noncash credit']=='USD 100.00' and o['Remaining obligation']=='USD 100.00'
    assert ledger.actual_cash('USD')==80000

@pytest.mark.parametrize('case,credit,target_remaining',[('credit_customer','USD 30.00','USD 380.00'),('credit_reversed','USD 100.00','USD 450.00')])
def test_customer_credit_reversal_never_reverses_original_cash(case,credit,target_remaining):
    ledger,result=evidence(case);x=row(result,'inv-x','invoice')['values'];y=row(result,'inv-y','invoice')['values']
    assert x['Net recorded payment settlement, all recorded dates']=='USD 200.00'
    assert x['Current effective obligation']=='USD 100.00' and x['Remaining obligation']=='USD 0.00'
    assert x['Unused credit']==credit and y['Remaining obligation']==target_remaining
    assert ledger.actual_cash('USD')==120000

@pytest.mark.parametrize('case,principal,interest,total,cash,paid_principal',[
 ('future_loan','USD 100.00','USD 15.00','USD 115.00',100000,'USD 0.00'),
 ('loan_position','USD 80.00','USD 5.00','USD 85.00',97000,'USD 20.00')])
def test_loan_snapshot_excludes_future_settlement_and_formats_minor_units(case,principal,interest,total,cash,paid_principal):
    ledger,result=evidence(case);loan=row(result,'existing-loan','loan')['values']
    assert loan['Opening principal']=='USD 100.00'
    assert loan['Outstanding principal through snapshot']==principal and loan['Unpaid interest through snapshot']==interest
    assert loan['Unpaid total through snapshot']==total
    assert loan['loan-existing-loan-1 / 2027-01-10 / Principal paid through snapshot']==paid_principal
    assert loan['loan-existing-loan-1 / 2027-01-10 / Remaining principal after all dated recorded settlements']=='USD 30.00'
    assert loan['loan-existing-loan-1 / 2027-01-10 / Remaining interest after all dated recorded settlements']=='USD 0.00'
    assert ledger.actual_cash('USD')==cash

@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_actual_outbound_financial_values_are_formatted_while_local_history_retains_integers(mode):
    ledger,result=evidence('future_loan');original=copy.deepcopy(result)
    sent=outbound_filter(ledger,mode)(result)
    assert sent['result']['loan_positions'][0]['opening_principal_display']=='USD 100.00'
    assert 'opening_principal_minor' not in sent['result']['loan_positions'][0]
    assert sent['result']['accounts'][0]['opening_display']=='USD 1,000.00'
    assert result==original and result['result']['loan_positions'][0]['opening_principal_minor']==10000

def test_formatter_handles_jpy_negative_values_unknown_currency_and_keeps_draft_terms():
    from cashflow_copilot.agent_facts import model_money_view
    ledger=case_ledger('breach');ledger.accounts['jpy']=Account('jpy','JPY',12500,0)
    raw=dict(currencies={'JPY':dict(closing_minor=12500),'USD':dict(closing_minor=-123),'ZZZ':dict(closing_minor=123)},
        draft_terms=[dict(currency='USD',amount_minor=5000)],unknown=dict(amount_minor=7))
    result=model_money_view(raw,ledger)
    assert result['currencies']['JPY']['closing_display']=='JPY 12,500'
    assert result['currencies']['USD']['closing_display']=='USD -1.23'
    assert result['currencies']['ZZZ']['closing_minor']==123 and result['unknown']=={'amount_minor':7}
    assert result['draft_terms']==raw['draft_terms'] and raw['currencies']['USD']['closing_minor']==-123

def test_minimized_alias_omission_is_explicit_and_full_metadata_retains_actual_alias():
    ledger,result=evidence('party_identity')
    minimized=outbound_filter(ledger,'minimized')(result);full=outbound_filter(ledger,'full_metadata')(result)
    assert 'Customer Trading' not in json.dumps(minimized)
    assert 'omits source names' in minimized['metadata_visibility']
    assert full['result']['reviewed_party_identities'][0]['aliases']==['Customer Trading']

def test_ui_loan_values_are_independent_of_100x_wrong_prose_and_source_text_is_escaped():
    from cashflow_copilot import agent_web,web
    _,result=evidence('future_loan');result['result']['financial_review']['rows'][-1]['evidence_ids']=['<script>bad</script>']
    html=agent_web._sources_html(web,[dict(name='get_evidence',ok=True,result=result['result'])])
    assert 'Outstanding principal through snapshot' in html and 'USD 100.00' in html
    assert 'USD 10,000.00' not in html and '<script>bad</script>' not in html and '&lt;script&gt;' in html

def test_first_breach_and_worst_shortfall_are_distinct_computed_dates_and_amounts():
    ledger=case_ledger('breach')
    result=execute_tool(RequestContext('agent','agent',ledger.business_id),ledger,'compute_forecast',{})['result']['currencies']['USD']
    assert result['first_breach_date']=='2027-01-07' and result['first_breach_balance']=='USD -200.00'
    assert result['first_breach_shortfall']=='USD 400.00'
    assert result['min_balance_date']=='2027-01-08' and result['worst_shortfall']=='USD 600.00'

def test_explicit_full_collection_is_cash_equivalent_to_plain_seven_day_delay():
    ledger=case_ledger('delay');ctx=RequestContext('agent','agent',ledger.business_id)
    simple=execute_tool(ctx,ledger,'compute_forecast',dict(scenario=dict(receivable_id='inv-x',shift_receivable_days=7)))['result']
    full=execute_tool(ctx,ledger,'compute_forecast',dict(scenario=dict(receivable_id='inv-x',shift_receivable_days=7,collection_percent=100)))['result']
    for ccy in ['USD','EUR']:
        for key in ['opening_minor','min_balance_minor','closing_minor','first_breach_date','first_breach_shortfall','worst_shortfall_minor']:
            assert full['currencies'][ccy][key]==simple['currencies'][ccy][key]
    assert '2027-01-19' in simple['scenario_description'] and '2027-01-19' in full['scenario_description']
    assert ledger.receivables['inv-x'].remaining_minor==65000 and not ledger.receipts
