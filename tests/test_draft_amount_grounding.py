"""Equivalent decimal formatting uses exact source text without inventing money."""
import copy,json
import pytest
from cashflow_copilot import agent_tasks as tasks
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.authz import owner_context_for
from tests.test_guided_tasks import Parser,save,execute
from tests.test_web import _load

BASE=dict(kind='request_early_payment',target_id='inv-client-001',currency='USD',amount='100',date='2026-10-22')
QUESTION='Prepare an early-payment request draft for inv-client-001, USD 100 on 2026-10-22. Owner review only.'

@pytest.mark.parametrize('amount',['100.0','100.00','100.000000','0100.00'])
@pytest.mark.parametrize('skill',['auto','collections-preparation'])
def test_equivalent_model_amount_uses_exact_source_lexeme_without_a_ledger_effect(amount,skill):
    l=Ledger.seed();before=copy.deepcopy(vars(l));args=dict(BASE,amount=amount)
    result=tasks.translate(l,QUESTION,Parser('action_proposal',args),ctx=owner_context_for(l.business_id),skill_id=skill)
    assert result['status']=='review' and result['plan']['parameters']==BASE
    assert args['amount']==amount and vars(l)==before

@pytest.mark.parametrize('source,model',[('100.00','100'),('100.00','100.0'),('0.01','0.010'),('100','100.00')])
def test_original_user_spelling_is_preserved(source,model):
    l=Ledger.seed();question=QUESTION.replace('USD 100','USD '+source)
    plan=dict(task='action_proposal',parameters=dict(BASE,amount=model));before=copy.deepcopy(plan)
    validated=tasks.validate_plan(l,plan,question,ctx=owner_context_for(l.business_id))
    assert validated['parameters']['amount']==source and plan==before

@pytest.mark.parametrize('amount',['100.001','99.99','10000','1e2','+100','-100',100,100.0,True,'1,00','100.0000000'])
def test_changed_precision_units_types_signs_and_unsupported_formats_stay_rejected(amount):
    l=Ledger.seed();r=tasks.translate(l,QUESTION,Parser('action_proposal',dict(BASE,amount=amount)),ctx=owner_context_for(l.business_id))
    assert r['status']=='clarification' and not l.actions and not l.receipts

@pytest.mark.parametrize('question',[
    'Prepare early payment inv-client-001 USD on 2026-10-22 with no amount.',
    'Prepare early payment inv-client-100 USD on 2026-10-22; source ID is inv-client-001.',
    'Prepare early payment inv-client-001 USD -100 on 2026-10-22.',
    'Prepare early payment inv-client-001 USD 1,100 on 2026-10-22.',
    'Prepare early payment inv-client-001 USD 100,00 on 2026-10-22.',
    'Prepare early payment inv-client-001 USD 1e2 on 2026-10-22.',
    'Prepare early payment inv-client-001 USD 100 or 100.0 on 2026-10-22; choose the amount.',
])
def test_amount_cannot_be_recovered_from_an_id_date_grouped_number_or_ambiguous_spelling(question):
    l=Ledger.seed();r=tasks.translate(l,question,Parser('action_proposal',dict(BASE,amount='100.00')),ctx=owner_context_for(l.business_id))
    assert r['status']=='clarification' and not l.actions

def test_punctuation_after_source_amount_is_not_a_group_separator():
    l=Ledger.seed();r=tasks.translate(l,QUESTION.replace('USD 100 on','USD 100, on'),Parser('action_proposal',dict(BASE,amount='100.00')),ctx=owner_context_for(l.business_id))
    assert r['status']=='review' and r['plan']['parameters']['amount']=='100'

def test_date_components_cannot_supply_a_missing_amount():
    l=Ledger.seed();q='Prepare early payment inv-client-001 USD on 2026-10-22 with no stated amount.'
    r=tasks.translate(l,q,Parser('action_proposal',dict(BASE,amount='22.00')),ctx=owner_context_for(l.business_id))
    assert r['status']=='clarification'

@pytest.mark.parametrize('change',[{'currency':'EUR'},{'target_id':'inv-client-002'},{'date':'2026-10-23'}])
def test_equivalent_money_does_not_relax_currency_target_or_date_grounding(change):
    l=Ledger.seed();args=dict(BASE,amount='100.00');args.update(change)
    assert tasks.translate(l,QUESTION,Parser('action_proposal',args),ctx=owner_context_for(l.business_id))['status']=='clarification'

def test_normalized_amount_executes_as_one_draft_and_replays_without_cash_movement(tmp_path):
    l=Ledger.seed();db=tmp_path/'padding.sqlite';save(db,l)
    result=tasks.translate(l,QUESTION,Parser('action_proposal',dict(BASE,amount='100.00')),ctx=owner_context_for(l.business_id))
    assert result['status']=='review'
    done=execute(db,l,result['plan'],question=QUESTION)
    replay=execute(db,l,result['plan'],question=QUESTION);after=_load(db)
    action=after.actions['guided-'+'a'*32]
    assert done.status=='completed' and replay.replayed and len(after.actions)==1
    assert action.state=='draft' and action.terms['amount_minor']==10000
    assert action.counterparty_response is None and not after.receipts and after.actual_cash('USD')==l.actual_cash('USD')
