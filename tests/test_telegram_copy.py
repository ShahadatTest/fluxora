"""Readable Telegram cards retain exact facts, review boundaries and escaping."""
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from cashflow_copilot import telegram_copy as wording
from cashflow_copilot.errors import CashflowError
from cashflow_copilot.forecast import forecast
from cashflow_copilot.money import format_amount
from tests.test_hosted import public_server
from tests.test_telegram import bridge,link,send,press,reviews,deliveries
from tests.test_web import _load


class TelegramHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.stack=[];self.parts=[]
    def handle_starttag(self,tag,attrs):assert tag in {'b','code'} and not attrs;self.stack.append(tag)
    def handle_endtag(self,tag):assert self.stack.pop()==tag
    def handle_data(self,data):self.parts.append(data)


def readable(payload):
    assert payload['parse_mode']=='HTML'
    parser=TelegramHTML();parser.feed(payload['text']);parser.close();assert not parser.stack
    assert len(payload['text'].encode('utf-16-le'))//2<=3800
    return ''.join(parser.parts)


def test_forecast_card_preserves_engine_values_currency_dates_and_sources(bridge):
    tg=bridge;a=link(tg);before=_load(a.database)
    send(tg,'/forecast',2);body=readable(deliveries(tg)[-1])
    computed=forecast(before,before.as_of,before.horizon_days)
    for ccy,curve in computed.currencies.items():
        for value in [curve.min_balance_minor,curve.closing_minor,curve.floor_minor]:
            assert format_amount(value,ccy) in body
        assert str(curve.min_balance_date) in body
        if curve.first_breach_date:
            assert str(curve.first_breach_date) in body
            assert format_amount(curve.worst_shortfall_minor,ccy) in body
    assert 'Source references:' in body and 'conditional' in body and '/plans' in body
    assert 'Synthetic workspace' in body and not _load(a.database).actions


def test_draft_and_two_stage_snapshot_have_human_exact_terms_and_status(bridge):
    tg=bridge;a=link(tg);send(tg,'/draft inv-client-001 USD 100 2026-10-22',2)
    body=readable(deliveries(tg)[-1])
    for value in ['inv-client-001','USD','100','2026-10-22','15 minutes','Nothing has been prepared']:assert value in body
    assert '{' not in body and deliveries(tg)[-1]['reply_markup']['inline_keyboard'][0][0]['text']=='Create DRAFT'
    press(tg,reviews(tg)[-1]['token']);assert 'DRAFT ready' in readable(deliveries(tg)[-1])
    assert next(iter(_load(a.database).actions.values())).state=='draft'
    send(tg,'/snapshot Q4 <review> & reserve',3);press(tg,reviews(tg)[-1]['token'],101)
    card=readable(deliveries(tg)[-1])
    assert 'Owner approval required' in card and 'Q4 <review> & reserve' in card
    assert 'Recorded cash:' in card and 'Current recorded cash is unchanged' in card
    assert 'Source revision:' in card and not _load(a.database).forecast_snapshots
    press(tg,reviews(tg)[-1]['token'],102)
    assert 'Snapshot saved' in readable(deliveries(tg)[-1]) and len(_load(a.database).forecast_snapshots)==1


def test_html_in_user_labels_is_escaped_not_telegram_markup(bridge):
    tg=bridge;link(tg);label='<b>Approved</b> & <a href="https://evil.example">pay</a>'
    send(tg,'/snapshot '+label,2);payload=deliveries(tg)[-1]
    assert label in readable(payload)
    assert '<a href=' not in payload['text'] and '&lt;b&gt;Approved&lt;/b&gt;' in payload['text']
    tg.queue('plain <b>not formatting</b> & provider text')
    assert readable(deliveries(tg)[-1])=='plain <b>not formatting</b> & provider text'


def test_multibyte_oversized_review_is_refused_before_review_storage(bridge):
    tg=bridge;link(tg)
    with tg.workspace() as binding:
        with pytest.raises(CashflowError):tg._review(binding,tg._load(),'task',{},wording.card('Oversized','😀'*2000),'Run')
    assert not reviews(tg)
    with pytest.raises(CashflowError):tg.queue('x'*3801)


@pytest.mark.parametrize('command,title',[('/nonsense','Command not recognised'),('/draft incomplete','Add the exact draft terms')])
def test_error_copy_gives_specific_next_action_without_financial_effect(bridge,command,title):
    tg=bridge;a=link(tg);send(tg,command,2)
    body=readable(deliveries(tg)[-1]);assert title in body and not reviews(tg) and not _load(a.database).actions


def test_changed_review_explains_fresh_review_and_does_not_apply(bridge):
    tg=bridge;a=link(tg);send(tg,'/draft inv-client-001 USD 100 2026-10-22',2)
    assert a.post('/demo/apply-delay')[0]==200
    press(tg,reviews(tg)[0]['token']);body=readable(deliveries(tg)[-1])
    assert 'Recorded data changed' in body and 'fresh request' in body and not _load(a.database).actions


def test_provider_or_domain_exception_text_is_never_shown(bridge,monkeypatch):
    tg=bridge;link(tg)
    def unsafe(*args,**kwargs):raise CashflowError('dummy-secret credential reflected by provider')
    monkeypatch.setattr(tg,'_plan',unsafe)
    send(tg,'/snapshot harmless',2);body=readable(deliveries(tg)[-1])
    assert 'dummy-secret' not in body and 'Request needs review' in body


def test_completion_ignores_arbitrary_answer_and_failed_outcome_is_not_success():
    result=SimpleNamespace(answer='dummy-secret; payroll paid; all plans guaranteed',tool_calls=[])
    body=wording.completion({'task':'forecast'},SimpleNamespace(status='completed',result=result))
    assert 'dummy-secret' not in body and 'payroll paid' not in body
    failed=wording.completion({'task':'forecast'},SimpleNamespace(status='timeout',result=result))
    assert 'Task needs attention' in failed and 'timeout' in failed and 'Report ready' not in failed


def test_no_feasible_plan_and_minor_units_never_become_spendable_cash():
    body=wording.plans(dict(no_feasible_plan=True,plans=[dict(id='p',name='Test',financially_sufficient=False,
        currencies={'JPY':dict(minimum_minor=-105,shortfall_minor=205),'USD':dict(minimum_minor=123,shortfall_minor=0)})]))
    assert 'No feasible plan' in body and 'JPY -105' in body and 'USD 1.23' in body
    assert 'Owner approval' in body and 'currencies remain separate' in body.lower()
    exact=wording.fields(dict(amount_minor=123,currency='USD',flags=[True,None],nested={'source_id':'id<&>'}))
    assert 'integer minor units' in exact and '123' in exact and 'USD 123' not in exact
    assert 'id&lt;&amp;&gt;' in exact


def test_large_report_points_to_full_web_report_instead_of_dropping_currencies(bridge,monkeypatch):
    tg=bridge;link(tg)
    monkeypatch.setattr(wording,'forecast',lambda result:wording.card('Too long','x'*4000))
    send(tg,'/forecast',2);body=readable(deliveries(tg)[-1])
    assert 'Report ready in Web' in body and 'all currencies' in body
