"""One explicit synthetic happy-path scenario per workflow operation.

Fixture setup is an automated test owner's source attestation, never model
authorization or real-world bank evidence. Digest values hash the fixture rows.
"""
import copy,hashlib,inspect,json
from datetime import date
from cashflow_copilot.agent_workflows import _registry,DATES
from cashflow_copilot.authz import owner_context_for
from cashflow_copilot.owner_workflows import review_revision
from cashflow_copilot.models import Receipt
from tools.live_cases import case_ledger

NOTE='Synthetic test owner checked the invented source; no real bank verification.'
def names():return tuple(_registry())

def fixture(name):
    if name not in names():raise ValueError('Unknown case')
    base='credit_reversed' if name in {'record_refund','correct_refund','carry_credit'} else 'credit_customer' if name=='reverse_credit' else 'breach'
    ledger=case_ledger(base);ledger.business_id='operation-'+name
    # Prebuilt credit fixtures have no workflow authorizations. Keep their
    # existing observation business scope consistent in this isolated fixture.
    for r in ledger.receipts.values():r.business_id=ledger.business_id
    ctx=owner_context_for(ledger.business_id)
    common=dict(evidence_id='ev-customer',note=NOTE)
    def do(operation,**args):
        fn=_registry()[operation];spec=inspect.signature(fn).parameters
        args=copy.deepcopy(args)
        for key in DATES & args.keys():
            if isinstance(args[key],str):args[key]=date.fromisoformat(args[key])
        args['ctx']=ctx
        if 'revision' in spec:args['revision']=review_revision(ledger)
        if 'acknowledge_duplicates' in spec:args.setdefault('acknowledge_duplicates',False)
        return fn(ledger,**args)
    def receipt():ledger.record_receipt(Receipt('receipt-one','inv-x',10000,ledger.as_of,evidence_id='ev-customer'),ctx=ctx)
    observation=dict(id='observation-one',amount_minor=15000,currency='USD',date='2027-01-01',invoice_reference='SYNTHETIC-ROW')
    digest=lambda rows:hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
    def inbox():do('import_observations',rows=[observation],file_digest=digest([observation]))
    def split():
        inbox();do('confirm_split_observation',observation_id='observation-one',assignments={'inv-x':5000},note=NOTE)
    def statement(amount=10000):
        rows=[dict(id='row-one',currency='USD',signed_minor=amount,date='2027-01-01',reference='Synthetic deposit')]
        return do('import_statement',rows=rows,digest=digest(rows),note=NOTE)['batch_id']
    loan=dict(id='loan-one',lender='Synthetic Lender',reference='FIXTURE-LOAN',currency='USD',opening_principal_minor=10000,
        schedule=[dict(date='2027-01-10',principal_minor=10000,interest_minor=500)],
        evidence_id='ev-supplier',note=NOTE,already_in_opening=True,not_recorded_elsewhere=True)
    recurring=dict(template_id='recurring-one',label='Synthetic recurring operations',currency='USD',amount_minor=1234,start='2027-01-02',end='2027-01-30',frequency='weekly',protected=False,consent_required=False,counterparty='Synthetic Utility',source='ev-ops',note=NOTE)
    args=None
    if name=='correct_receipt':receipt();args=dict(receipt_id='receipt-one',invoice_id='inv-x',amount_minor=9000,day='2027-01-01',**common)
    elif name=='record_bill_payment':args=dict(bill_id='materials',payment_id='payment-one',amount_minor=10000,day='2027-01-01',**common)
    elif name=='correct_bill_payment':
        do('record_bill_payment',bill_id='materials',payment_id='payment-one',amount_minor=10000,day='2027-01-01',**common)
        args=dict(bill_id='materials',payment_id='payment-one',amount_minor=9000,day='2027-01-01',**common)
    elif name=='save_forecast':args=dict(label='Operation snapshot')
    elif name=='set_collection_hold':args=dict(invoice_id='inv-x',active=True,source='ev-customer',note=NOTE)
    elif name=='create_record':args=dict(record=dict(kind='invoice',id='invoice-new',currency='USD',amount_minor=12345,date='2027-01-15',due_date='2027-01-15',category='invoice',protected=False,counterparty='Synthetic Customer Two',invoice_reference='UNIQUE-NEW',evidence_id='ev-customer',note=NOTE))
    elif name=='adjust_face':args=dict(kind='invoice',item_id='inv-x',new_total=60000,day='2027-01-01',**common)
    elif name=='correct_split_payment':split();args=dict(payment_id='observation-one',new_amount=14000,day='2027-01-01',**common)
    elif name=='record_cash_movement':args=dict(movement_id='cash-one',classification='bank_fee',currency='USD',amount=100,day='2027-01-01',**common)
    elif name=='correct_cash_movement':
        do('record_cash_movement',movement_id='cash-one',classification='bank_fee',currency='USD',amount=100,day='2027-01-01',**common)
        args=dict(movement_id='cash-one',new_amount=90,day='2027-01-01',**common)
    elif name=='import_statement':
        rows=[dict(id='row-one',currency='USD',signed_minor=10000,date='2027-01-01',reference='Synthetic deposit')]
        args=dict(rows=rows,digest=digest(rows),note=NOTE)
    elif name=='review_statement_row':receipt();args=dict(batch_id=statement(),row_id='row-one',movement_key='receipt:receipt-one',status='matched',**common)
    elif name=='record_refund':args=dict(kind='invoice',item_id='inv-x',refund_id='refund-one',amount=1000,day='2027-01-01',**common)
    elif name=='correct_refund':
        do('record_refund',kind='invoice',item_id='inv-x',refund_id='refund-one',amount=1000,day='2027-01-01',**common)
        args=dict(refund_id='refund-one',amount=500,day='2027-01-01',**common)
    elif name=='correct_payment_date':receipt();args=dict(movement_key='receipt:receipt-one',new_date='2027-01-02',**common)
    elif name=='create_instalments':args=dict(proposal_id='instalment-one',bill_id='materials',schedule=[dict(date='2027-01-12',amount_minor=50000)],**common)
    elif name=='advance_instalments':
        do('create_instalments',proposal_id='instalment-one',bill_id='materials',schedule=[dict(date='2027-01-12',amount_minor=50000)],**common)
        do('advance_instalments',proposal_id='instalment-one',step='approve',**common)
        args=dict(proposal_id='instalment-one',step='accept',**common)
    elif name=='save_scenario':args=dict(label='Synthetic delay',delays=[dict(invoice_id='inv-x',delay_days=7)],expenses=[])
    elif name=='check_statement_balance':args=dict(check_id='balance-one',batch_id=statement(),account_id='usd',start='2027-01-01',end='2027-01-01',opening=100000,closing=110000,previous_id='',**common)
    elif name=='carry_credit':args=dict(transfer_id='credit-two',kind='invoice',source_id='inv-x',target_id='inv-y',amount=1000,**common)
    elif name=='reverse_credit':args=dict(transfer_id='carried',**common)
    elif name=='register_loan':args=dict(spec=loan)
    elif name=='void_loan_registration':do('register_loan',spec=loan);args=dict(loan_id='loan-one',**common)
    elif name=='item_identity':args=dict(kind='invoice',item_id='inv-x',counterparty='Synthetic Customer',invoice_reference='UPDATED-REF',source='ev-customer',note=NOTE)
    elif name=='review_duplicate':
        from cashflow_copilot.operations import duplicate_pairs
        ledger.receivables['inv-copy']=copy.deepcopy(ledger.receivables['inv-x']);ledger.receivables['inv-copy'].id='inv-copy'
        pair=duplicate_pairs(ledger,ctx=ctx)[0];args=dict(pair_id=pair['id'],verdict='distinct',source='ev-customer',note=NOTE)
    elif name=='create_recurring':args=recurring
    elif name in {'pause_recurring','confirm_recurring'}:
        do('create_recurring',**recurring)
        args=dict(template_id='recurring-one',active=False) if name=='pause_recurring' else dict(template_id='recurring-one',through='2027-01-09')
    elif name=='register_party':args=dict(party_id='party-one',role='customer',name='Synthetic Customer',**common)
    elif name in {'update_party','link_party'}:
        do('register_party',party_id='party-one',role='customer',name='Customer',**common)
        args=dict(party_id='party-one',step='alias',name='Synthetic Customer Alias',**common) if name=='update_party' else dict(kind='invoice',item_id='inv-x',party_id='party-one',**common)
    elif name=='add_account':args=dict(account_id='usd-other',currency='USD',name='Synthetic savings',**common)
    elif name=='assign_movement':
        receipt();do('add_account',account_id='usd-other',currency='USD',name='Synthetic savings',**common)
        args=dict(movement_key='receipt:receipt-one',account_id='usd-other',**common)
    elif name=='bind_statement':args=dict(batch_id=statement(),account_id='usd',**common)
    elif name in {'observed_transfer','reverse_transfer'}:
        do('add_account',account_id='usd-other',currency='USD',name='Synthetic savings',**common)
        args=dict(transfer_id='transfer-one',source_id='usd',target_id='usd-other',amount=10000,day='2027-01-01',**common)
        if name=='reverse_transfer':do('observed_transfer',**args);args=dict(transfer_id='transfer-one',**common)
    elif name in {'restrict_cash','release_cash'}:
        args=dict(restriction_id='restriction-one',account_id='usd',amount=10000,purpose='restricted_deposit',**common)
        if name=='release_cash':do('restrict_cash',**args);args=dict(restriction_id='restriction-one',**common)
    elif name in {'create_promise','review_promise'}:
        args=dict(promise_id='promise-one',invoice_id='inv-x',amount=10000,due='2027-01-12',**common)
        if name=='review_promise':do('create_promise',**args);receipt();args=dict(promise_id='promise-one',step='link',receipt_id='receipt-one',amount=10000,**common)
    elif name=='review_net_settlement':
        receipt();do('record_cash_movement',movement_id='fee-one',classification='bank_fee',currency='USD',amount=100,day='2027-01-01',**common)
        args=dict(batch_id=statement(9900),row_id='row-one',movement_keys=['receipt:receipt-one','other_cash:fee-one'],**common)
    elif name in {'record_unallocated_refund','correct_unallocated_refund'}:
        split();args=dict(payment_id='observation-one',refund_id='unallocated-refund-one',amount=1000,day='2027-01-01',**common)
        if name=='correct_unallocated_refund':do('record_unallocated_refund',**args);args=dict(refund_id='unallocated-refund-one',amount=900,day='2027-01-01',**common)
    elif name=='confirm_fact_check':args=dict(kind='collection',ident='inv-x',**common)
    elif name=='confirm_split_observation':inbox();args=dict(observation_id='observation-one',assignments={'inv-x':5000},note=NOTE)
    elif name=='allocate_split_balance':split();args=dict(payment_id='observation-one',assignments={'inv-y':5000},allocation_date='2027-01-01',**common)
    elif name=='import_observations':args=dict(rows=[observation],file_digest=digest([observation]))
    elif name=='allocate_observation':inbox();args=dict(observation_id='observation-one',invoice_id='inv-x',note=NOTE)
    elif name=='dismiss_observation':inbox();args=dict(observation_id='observation-one',note=NOTE)
    elif name=='confirm_collection_date':args=dict(invoice_id='inv-x',expected_date='2027-01-13',evidence_id='ev-customer',reason=NOTE)
    elif name in {'review_receipt','reconcile_existing_receipt','reopen_receipt_review'}:
        receipt();args=dict(receipt_id='receipt-one',action_id='',**common)
        if name!='review_receipt':do('review_receipt',**args);args=dict(receipt_id='receipt-one',**common)
        if name=='reopen_receipt_review':args=dict(receipt_id='receipt-one',note=NOTE)
    elif name=='assign_forecast_route':args=dict(kind='bill',item_id='materials',account_id='usd',**common)
    elif name=='sandbox_send_action':
        from cashflow_copilot.agent import execute_tool
        from cashflow_copilot.authz import RequestContext
        execute_tool(RequestContext('fixture-agent','agent',ledger.business_id),ledger,'draft_action',dict(action_id='action-one',kind='request_early_payment',receivable_id='inv-x',amount_minor=10000,date='2027-01-03'))
        args=dict(action_id='action-one')
    assert args is not None,name
    return ledger,dict(operation=name,arguments=args)


def assert_effect(name,after,before):
    """Independent fixture amounts and key outcomes; not a second executor."""
    cash_delta={'correct_receipt':-1000,'record_bill_payment':-10000,'correct_bill_payment':1000,
        'correct_split_payment':-1000,'record_cash_movement':-100,'correct_cash_movement':10,
        'record_refund':-1000,'correct_refund':500,'correct_payment_date':-10000,
        'record_unallocated_refund':-1000,'correct_unallocated_refund':100,
        'confirm_split_observation':15000,'allocate_observation':15000}
    assert after.actual_cash('USD')==before.actual_cash('USD')+cash_delta.get(name,0),name+' cash'
    assert after.actual_cash('EUR')==before.actual_cash('EUR'),name+' EUR isolation'
    assert after.outflows['staff'].protected and after.outflows['staff'].date==before.outflows['staff'].date
    if name=='save_forecast':assert len(after.forecast_snapshots)==1
    if name=='set_collection_hold':assert after.receivables['inv-x'].collection_hold['active']
    if name=='create_record':assert after.receivables['invoice-new'].original_minor==12345
    if name=='adjust_face':assert after.receivables['inv-x'].effective_total_minor==60000
    if name=='carry_credit':assert after.receivables['inv-x'].credit_balance_minor==9000 and after.receivables['inv-y'].remaining_minor==44000
    if name=='reverse_credit':assert after.receivables['inv-x'].credit_balance_minor==10000 and after.receivables['inv-y'].remaining_minor==45000
    if name=='register_loan':assert after.loans['loan-one']['opening_principal_minor']==10000
    if name=='void_loan_registration':assert after.loans['loan-one']['voided']
    if name=='create_instalments':assert after.instalment_proposals['instalment-one']['state']=='draft'
    if name=='advance_instalments':assert after.instalment_proposals['instalment-one']['state']=='counterparty_accepted'
    if name=='pause_recurring':assert not after.recurring_bills['recurring-one']['active']
    if name=='confirm_recurring':assert len(after.recurring_occurrences)==2
    if name=='observed_transfer':assert after.internal_transfers['transfer-one']
    if name=='reverse_transfer':assert after.internal_transfers['transfer-one']['reversal']
    if name=='restrict_cash':assert after.cash_restrictions['restriction-one']['amount_minor']==10000
    if name=='release_cash':assert after.cash_restrictions['restriction-one']['release']
    if name=='confirm_collection_date':assert str(after.receivables['inv-x'].expected_date)=='2027-01-13'
    if name=='allocate_observation':assert after.receipts['observation-one'].amount_minor==15000
    if name=='dismiss_observation':assert after.receipt_inbox['observation-one']['status']=='dismissed'
    if name=='review_receipt':assert after.receipt_reviews['receipt-one']['status']=='matched'
    if name=='reconcile_existing_receipt':assert after.receipt_reviews['receipt-one']['status']=='reconciled'
    if name=='reopen_receipt_review':assert 'receipt-one' not in after.receipt_reviews
    if name=='sandbox_send_action':assert after.actions['action-one'].state=='sent'
