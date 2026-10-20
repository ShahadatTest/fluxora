"""Deterministic explanation inputs. No model prose is validated here."""
import copy
from .money import format_amount
from .settlement import bill_schedule


def financial_review(ledger, evidence_result):
    rows=[]
    for kind,group in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
        for item in group.values():
            credit=sum(c['delta_minor'] for c in item.credit_entries)
            settled=item.allocated_minor if kind=='invoice' else item.paid_minor
            projected=sum(a.amount_minor for a in item.allocations if not a.observed) if kind=='invoice' else 0
            cash_settlement=settled-credit-projected
            money=lambda n:format_amount(n,item.currency)
            refs={item.evidence_id}
            refs.update(c['evidence_id'] for c in item.credit_entries+item.face_adjustments+item.refunds)
            values={'Original face value':money(item.original_minor if kind=='invoice' else item.amount_minor),
                'Current effective obligation':money(item.effective_total_minor),
                'Net recorded payment settlement, all recorded dates':money(cash_settlement),
                'Net noncash credit applied (negative means carried out)':money(credit),
                'Projected allocations, not observed cash':money(projected),
                'Total settlement including noncash credit':money(settled),
                'Remaining obligation':money(item.remaining_minor),
                'Unused credit':money(item.credit_balance_minor if kind=='invoice' else item.overpaid_minor)}
            if kind=='bill':
                residual=bill_schedule(item) if item.status=='scheduled' else []
                values['Unpaid residual schedule']='; '.join(f'{day}: {money(amount)}' for day,amount in residual) or 'None'
                refs.update(p['evidence_id'] for p in item.observed_payments+item.payment_adjustments)
            else:refs.update(r.evidence_id for r in ledger.receipts.values() if r.receivable_id==item.id)
            rows.append(dict(record_type=kind,id=item.id,currency=item.currency,values=values,evidence_ids=sorted(refs-{None,''}),
                interpretation='Settlement = net recorded payment settlement + projected allocations + net noncash credit. '
                'Remaining = max(0, effective obligation - settlement); unused credit = max(0, settlement - effective obligation). '
                'Face-value adjustments restate the obligation; they are not payments, credit transfers or missing cash. '
                'Settlement includes all recorded dates, including future entries, and is not current bank cash. '
                'Use the residual schedule, not the historical source schedule, for unpaid bills.'))
    for loan in evidence_result.get('loan_positions',[]):
        money=lambda n:format_amount(n,loan['currency'])
        values={'Opening principal':money(loan['opening_principal_minor']),
            'Outstanding principal through snapshot':money(loan['outstanding_principal_minor']),
            'Unpaid interest through snapshot':money(loan['unpaid_interest_minor']),
            'Unpaid total through snapshot':money(loan['unpaid_total_minor'])}
        for row in loan['rows']:
            prefix=row['bill_id']+' / '+row['date']+' / '
            for label,key in [('Principal paid through snapshot','principal_paid_minor'),('Interest paid through snapshot','interest_paid_minor'),
                ('Recorded settlement, all dates','paid_minor'),('Remaining scheduled, including future records','remaining_minor')]:
                values[prefix+label]=money(row[key])
            interest_settled=min(max(0,row['paid_minor']),row['interest_minor'])
            principal_settled=min(max(0,row['paid_minor']-interest_settled),row['principal_minor'])
            values[prefix+'Remaining interest after all dated recorded settlements']=money(0 if loan.get('voided') else row['interest_minor']-interest_settled)
            values[prefix+'Remaining principal after all dated recorded settlements']=money(0 if loan.get('voided') else row['principal_minor']-principal_settled)
        rows.append(dict(record_type='loan',id=loan['id'],currency=loan['currency'],values=values,evidence_ids=[loan['evidence_id']],
            interpretation='Current principal and interest use observed cash only through the snapshot date, allocated interest first. '
            'Future-dated recorded settlements do not repay principal or reduce current cash today. '
            'The all-dates remainder is a separate interest-first allocation, not the current principal. '
            'Loan registration creates no drawdown cash. Voided registration has zero current position.'))
    return dict(as_of=str(ledger.as_of),rows=rows[:100],row_count=len(rows),truncated=len(rows)>100,
        rules='Deterministic accounting explanation, not bank/source authentication or approval. Copy formatted amounts exactly. '
        'Collection advance caps limit early customer collections; they are not borrowing or credit facilities. '
        'Projected scenario collections never settle invoices. No uncomputed timing or feasibility claim follows from a minimum/closing balance alone.')


def model_money_view(value,ledger,currency=None):
    """Format known-currency read results; preserve exact local tool history.

    Never guess a currency. Not used for proposal parameters or tool schemas.
    Unknown-context *_minor fields stay explicitly in minor units.
    """
    known={a.currency for a in ledger.accounts.values()}
    records={k:v.currency for group in (ledger.accounts,ledger.receivables,ledger.outflows) for k,v in group.items()}
    def visit(node,ccy=None):
        if isinstance(node,list):return [visit(x,ccy) for x in node]
        if not isinstance(node,dict):return node
        ccy=node.get('currency',ccy)
        if ccy not in known:ccy=None
        if ccy is None:
            for field in ('outflow_id','receivable_id','invoice_id','account_id'):
                if node.get(field) in records:ccy=records[node[field]];break
        result={}
        for key,item in node.items():
            if key in {'draft_terms','steps','arguments','structured_fields'}:
                result[key]=copy.deepcopy(item)
            elif key.endswith('_minor') and type(item) is int and ccy:
                result[key[:-6]+'_display']=format_amount(item,ccy)
            else:result[key]=visit(item,key if key in known else records.get(key,ccy))
        return result
    return visit(value,currency)
