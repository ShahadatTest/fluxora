"""Regressions for live-observed cash-scale mistakes and model wire compatibility."""
import copy,json
from types import SimpleNamespace
import pytest

def test_transfer_catalog_explains_observed_same_currency_boundary():
    from cashflow_copilot.agent_workflows import catalog
    detail=catalog('observed_transfer')['operations'][0]
    assert 'SAME currency' in detail['structured_fields']['constraints']
    assert 'already observed' in detail['structured_fields']['constraints']
    assert 'cross-currency' in catalog()['rules']
from cashflow_copilot.agent_workflows import prepare,public
from cashflow_copilot.agent_disclosure import outbound_filter
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.model_adapter import OpenRouterAdapter
from tests.test_model_adapter import stub,_StubHandler,_completion

@pytest.mark.parametrize('mode',['minimized','full_metadata'])
def test_live_workflow_cash_reply_uses_major_strings_and_retains_exact_local_minor_units(ledger,mode):
    original=copy.deepcopy(ledger.accounts)
    result=prepare(ledger,'cash-scale','Cash scale',[dict(operation='save_forecast',arguments=dict(label='Review'))],
        ctx=RequestContext('agent','agent',ledger.business_id))
    assert result['cash_before_minor']=={'EUR':100000,'USD':1000000}
    assert result['cash_before_display']=={'EUR':'EUR 1,000.00','USD':'USD 10,000.00'}
    local=copy.deepcopy(result);clean=outbound_filter(ledger,mode)(result)
    assert 'cash_before_minor' not in clean and 'cash_after_minor' not in clean
    assert clean['cash_before_display']==result['cash_before_display']==clean['cash_after_display']
    assert result==local and ledger.accounts==original and not ledger.forecast_snapshots
    assert ledger.agent_workflows['cash-scale']['status']=='pending_approval'

def test_formatter_respects_zero_decimal_currency_negative_cash_and_legacy_stored_job(ledger):
    prepare(ledger,'legacy','Legacy',[dict(operation='save_forecast',arguments=dict(label='Review'))],ctx=RequestContext('agent','agent',ledger.business_id))
    job=ledger.agent_workflows['legacy'];job['preview']['cash_before_minor']={'JPY':12500,'USD':-123}
    job['preview']['cash_after_minor']={'JPY':12500,'USD':-23}
    result=public(job)
    assert result['cash_before_display']=={'JPY':'JPY 12,500','USD':'USD -1.23'}
    assert result['cash_after_display']=={'JPY':'JPY 12,500','USD':'USD -0.23'}

@pytest.mark.parametrize('model,effort,ping_cap',[
    ('openai/gpt-6-luna','none',64),('deepseek/deepseek-v4-flash','none',64),('z-ai/glm-5.3-flash','low',256)])
def test_actual_model_profiles_and_verification_wire_keep_privacy_and_bounds(stub,model,effort,ping_cap):
    _StubHandler.responses=[(200,_completion('Local synthetic answer')),(200,_completion('OK'))]
    a=OpenRouterAdapter(api_key='DUMMY-PROFILE-KEY',base_url=stub,model=model,max_retries=0)
    assert a.chat([dict(role='user',content='Synthetic')],[]).text=='Local synthetic answer'
    assert a.verify().state=='verified'
    body=_StubHandler.requests[0]['body'];ping=_StubHandler.requests[1]['body']
    assert body['reasoning']==ping['reasoning']=={'effort':effort}
    assert body['max_tokens']==2048 and ping['max_tokens']==ping_cap
    assert ping['messages']==[dict(role='user',content='Reply OK.')]
    assert body['provider']==ping['provider']==dict(require_parameters=True,data_collection='deny',allow_fallbacks=False)
    assert 'models' not in body and 'tools' not in ping and 'reasoning_max_tokens' not in a.request_policy()

def test_agent_panel_reports_actual_luna_effort_instead_of_stale_qwen_budget(ledger,monkeypatch):
    from cashflow_copilot import agent_web,web
    a=OpenRouterAdapter(api_key='DUMMY-PANEL',model='openai/gpt-6-luna')
    monkeypatch.setattr(agent_web,'live_adapter',lambda:a)
    monkeypatch.setattr(web,'read_state',lambda fn:fn(ledger))
    html=agent_web._panel_html(web)
    assert 'Reasoning effort: none.' in html and '512 reasoning tokens requested' not in html
    assert 'DUMMY-PANEL' not in html

def test_owner_review_shows_book_cash_with_units_independent_of_wrong_model_prose(ledger):
    from cashflow_copilot.agent_workflow_web import _financial_preview
    from cashflow_copilot import web
    prepare(ledger,'cash-ui','Cash review',[dict(operation='save_forecast',arguments=dict(label='Review'))],ctx=RequestContext('agent','agent',ledger.business_id))
    html=_financial_preview(web,ledger.agent_workflows['cash-ui'])
    assert 'Current book cash' in html and 'USD 10,000.00' in html and 'EUR 1,000.00' in html
    assert 'USD 1,000,000' not in html

def test_agent_result_renders_deterministic_workflow_cash_separately_from_wrong_prose(ledger):
    from cashflow_copilot import agent_web,web
    result=prepare(ledger,'independent-card','Review',[dict(operation='save_forecast',arguments=dict(label='Review'))],ctx=RequestContext('agent','agent',ledger.business_id))
    html=agent_web._sources_html(web,[dict(name='prepare_workflow',ok=True,result=result)])
    assert 'deterministic workflow cash' in html and 'USD 10,000.00' in html and 'EUR 1,000.00' in html
    assert 'USD 1,000,000' not in html and 'has not executed' in html
