"""Regressions from actual Gemini weekly-unit and payroll commentary failures."""
import copy,json
import pytest
from cashflow_copilot.agent import execute_tool
from cashflow_copilot.agent_disclosure import outbound_filter
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Account

def weekly(ledger):
    return execute_tool(RequestContext('test-agent','agent',ledger.business_id),ledger,'read_workspace',{'view':'weekly_cashflow'})

@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_actual_workspace_reply_formats_weekly_money_and_preserves_local_integers(mode):
    ledger=Ledger.seed();reply=weekly(ledger);original=copy.deepcopy(reply)
    sent=outbound_filter(ledger,mode)(reply)
    eur=sent['result']['currencies']['EUR']['weeks'][0]
    usd=sent['result']['currencies']['USD']['weeks'][1]
    assert eur['min_balance_display']=='EUR 1,000.00'
    assert usd['min_balance_display']=='USD 6,000.00'
    assert 'min_balance_minor' not in eur
    assert 'major units' in sent['money_units']
    assert reply==original and reply['result']['currencies']['EUR']['weeks'][0]['min_balance_minor']==100000

def test_weekly_formatting_honors_zero_decimal_currency_and_negative_balance():
    ledger=Ledger.seed();ledger.accounts['jpy']=Account('jpy','JPY',12500,0)
    ledger.accounts['usd-main'].opening_minor=-123
    sent=outbound_filter(ledger,'minimized')(weekly(ledger))
    assert sent['result']['currencies']['JPY']['weeks'][0]['min_balance_display']=='JPY 12,500'
    assert sent['result']['currencies']['USD']['weeks'][0]['min_balance_display']=='USD -1.23'

def test_broader_workspace_filter_does_not_rewrite_pending_operation_arguments():
    ledger=Ledger.seed()
    args=dict(currency='USD',amount_minor=10000,account_id='usd-main')
    reply=dict(ok=True,tool='read_workspace',result=dict(workflows=[dict(id='sample',steps=[dict(operation='observed_transfer',arguments=args)])]))
    sent=outbound_filter(ledger,'minimized')(reply)
    assert sent['result']['workflows'][0]['steps'][0]['arguments']==args


@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_catalog_parameter_names_survive_while_proposed_private_labels_are_filtered(mode):
    ledger=Ledger.seed();ctx=RequestContext('test-agent','agent',ledger.business_id)
    reply=execute_tool(ctx,ledger,'read_workspace',{'view':'catalog','parameters_json':'{"operation":"save_forecast"}'})
    clean=outbound_filter(ledger,mode)
    assert clean(reply)['result']['operations'][0]['arguments']=={'label':{'type':'string','required':True}}
    proposed={'ok':True,'tool':'read_workspace','result':{'steps':[{'arguments':{'label':'Private business label'}}]}}
    sent=clean(proposed)['result']['steps'][0]['arguments']
    assert ('label' in sent)==(mode=='full_metadata')

def test_weekly_ui_uses_deterministic_major_units_even_when_model_prose_is_wrong():
    from cashflow_copilot import agent_web,web
    ledger=Ledger.seed();reply=weekly(ledger)
    calls=[dict(name='read_workspace',arguments={'view':'weekly_cashflow'},ok=True,result=reply['result'])]
    html=agent_web._sources_html(web,calls)+agent_web._deterministic_answer_html(web,calls)
    assert 'data-weekly-financial-facts' in html
    assert 'EUR 1,000.00' in html and 'USD 6,000.00' in html
    assert 'EUR 100,000' not in html and 'USD 600,000' not in html
    assert 'ev-supplier-invoice' in html and 'Minimum date' in html

def test_weekly_evidence_and_protected_obligation_ids_are_escaped():
    from cashflow_copilot import agent_web,web
    ledger=Ledger.seed();reply=weekly(ledger)
    reply['result']['currencies']['USD']['weeks'][0]['drivers'][0]['evidence_ids']=['<script>bad</script>']
    calls=[dict(name='read_workspace',arguments={'view':'weekly_cashflow'},ok=True,result=reply['result']),
           dict(name='get_evidence',ok=True,result={'outflows':[{'id':'<script>payroll</script>','protected':True}]})]
    html=agent_web._sources_html(web,calls)
    assert '<script>' not in html and '&lt;script&gt;bad' in html
    assert 'data-protected-obligations' in html and 'Owner approval does not waive protection' in html

def test_weekly_table_never_certifies_arbitrary_commentary():
    from cashflow_copilot import agent_web,web
    html=agent_web._answer_html(web,'The balance is EUR 100,000')
    assert 'Model commentary (unverified)' in html and 'not the financial answer' in html
