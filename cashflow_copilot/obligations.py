"""Owner-attested statement boundaries, noncash credits and loan schedules.

Documents are evidence only. No transfer, borrowing or counterparty consent is
executed. Loan instalments use ordinary protected outflows and their observations.
"""
import copy
from datetime import date, timedelta

from .authz import OP_READ, require_access
from .completion import _amount, _currency, _id
from .errors import CashflowError, IdempotencyConflict
from .models import Outflow
from .owner_workflows import _begin, _finish, _evidence, _text
from .planning import _hash
from .settlement import cash_events, collection_held, bill_payment_entries


def check_statement_balance(ledger, check_id, batch_id, account_id, start, end,
                            opening, closing, previous_id, evidence_id, note, revision, *, ctx):
    payload = dict(check_id=check_id, batch_id=batch_id, account_id=account_id,
        start=str(start), end=str(end), opening_minor=opening, closing_minor=closing,
        previous_id=previous_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'statement_boundaries_recorded', revision, payload)
    if old: return old
    _id(check_id); _evidence(ledger, evidence_id); _text(note, 'Statement boundary source check')
    if check_id in ledger.statement_balance_checks:
        raise IdempotencyConflict('Check ID already exists; record corrected terms with a fresh check ID.')
    if len(ledger.statement_balance_checks) >= 200:
        raise CashflowError('At most 200 immutable statement boundary checks are supported.')
    batch = ledger.statement_batches.get(batch_id); account = ledger.accounts.get(account_id)
    if batch is None or account is None or batch['business_id'] != ledger.business_id:
        raise CashflowError('Choose a statement and account in this business.')
    _currency(ledger, account.currency)
    from .treasury import statement_account, currency_accounts
    scope = statement_account(ledger, batch_id)
    if (scope and scope != account_id) or (len(currency_accounts(ledger, account.currency)) > 1 and not scope):
        raise CashflowError('Confirm the statement account scope first; boundary terms must name that same account.')
    if type(start) is not date or type(end) is not date or not start <= end <= start + timedelta(days=366):
        raise CashflowError('Use an inclusive statement period of at most 367 calendar dates.')
    if batch.get('context') and (str(start)!=batch['context']['start'] or str(end)!=batch['context']['end']):
        raise CashflowError('Boundary checks must use the mapped statement\'s exact declared period.')
    if any(type(m) is not int or abs(m) > 10**15 for m in (opening, closing)):
        raise CashflowError('Use signed integer minor-unit boundary balances within 10^15.')
    if any(r['currency'] != account.currency or not start <= date.fromisoformat(r['date']) <= end for r in batch['rows']):
        raise CashflowError('Every row must use this account currency and fall inside the stated period.')
    if previous_id:
        prev = ledger.statement_balance_checks.get(previous_id)
        if prev is None or prev['account_id'] != account_id or date.fromisoformat(prev['end']) >= start:
            raise CashflowError('Choose an earlier boundary check for this same account, or leave it unlinked.')
    check = dict(payload, id=check_id, currency=account.currency, actor=ctx.actor,
        batch_digest=_hash(batch), recorded_as_of=str(ledger.as_of))
    ledger.statement_balance_checks[check_id] = check
    return _finish(ledger, key, 'statement_boundaries_recorded', payload, dict(check_id=check_id), ctx)


def statement_balance_report(ledger, check_id, *, ctx):
    require_access(ctx, ledger, OP_READ)
    check = ledger.statement_balance_checks.get(check_id)
    if check is None: raise CashflowError('Choose a saved statement boundary check.')
    from .completion import statement_report
    batch = ledger.statement_batches[check['batch_id']]
    start, end = date.fromisoformat(check['start']), date.fromisoformat(check['end'])
    net = sum(r['signed_minor'] for r in batch['rows'])
    arithmetic_delta = check['closing_minor'] - (check['opening_minor'] + net)
    current = {c['batch_id']: c for c in ledger.statement_balance_checks.values()}
    problems = []
    if current[check['batch_id']]['id'] != check_id: problems.append('Superseded boundary terms; use the latest check for this batch.')
    if check['batch_digest'] != _hash(batch): problems.append('Imported source changed after boundary review.')
    if arithmetic_delta: problems.append('Opening plus signed rows does not equal closing.')
    overlaps = [c['id'] for c in current.values() if c['batch_id'] != check['batch_id'] and c['account_id'] == check['account_id']
        and date.fromisoformat(c['start']) <= end and date.fromisoformat(c['end']) >= start]
    if overlaps: problems.append('Statement periods overlap: ' + ', '.join(overlaps))
    previous_delta = None
    if check['previous_id']:
        prev = ledger.statement_balance_checks[check['previous_id']]
        previous_delta = check['opening_minor'] - prev['closing_minor']
        if current[prev['batch_id']]['id'] != prev['id']: problems.append('Previous boundary check was superseded; review this link again.')
        if date.fromisoformat(prev['end']) + timedelta(days=1) != start: problems.append('Gap between the linked statement periods.')
        if previous_delta: problems.append('Linked closing and next opening balances differ.')
        if prev['closing_minor'] != prev['opening_minor'] + sum(r['signed_minor'] for r in ledger.statement_batches[prev['batch_id']]['rows']):
            problems.append('Previous statement arithmetic is unresolved.')
    else:
        problems.append('No preceding statement linked; period continuity is unconfirmed.')
    opening_actual = closing_actual = None
    if start >= ledger.as_of:
        account = ledger.accounts[check['account_id']]
        events = [e for e in cash_events(ledger, include_internal=True) if e['account_id'] == account.id]
        opening_actual = account.opening_minor + sum(e['amount_minor'] for e in events if ledger.as_of <= e['date'] < start)
        from .treasury import account_cash
        closing_actual = account_cash(ledger, account.id, end)
        if check['opening_minor'] != opening_actual: problems.append('Statement opening differs from the current ledger opening for this period.')
        if check['closing_minor'] != closing_actual: problems.append('Statement closing differs from recorded ledger cash.')
    else:
        problems.append('Period predates the current opening; historical ledger boundaries cannot be reconstructed.')
    matching = statement_report(ledger, check['batch_id'], ctx=ctx)
    unresolved = [r['id'] for r in matching['rows'] if r['status'] != 'matched']
    if unresolved: problems.append('Some rows are unmatched, excluded or stale; review their source disposition.')
    return dict(copy.deepcopy(check), net_minor=net, arithmetic_delta_minor=arithmetic_delta,
        previous_delta_minor=previous_delta, ledger_opening_minor=opening_actual, ledger_closing_minor=closing_actual,
        opening_delta_minor=None if opening_actual is None else check['opening_minor']-opening_actual,
        closing_delta_minor=None if closing_actual is None else check['closing_minor']-closing_actual,
        unresolved_row_ids=unresolved, overlap_check_ids=overlaps, problems=problems,
        status='needs_review' if problems else 'checks_aligned',
        note_boundary='Owner-attested arithmetic, continuity and recorded-cash comparison only. A balanced file can omit offsetting rows. This does not authenticate a bank, prove completeness or authorize a cash adjustment.')


def credit_locked(ledger, kind, item_ids):
    """Freeze dependent source correction until explicit reverse-order unwind."""
    ids = set(item_ids)
    if any(t['kind'] == kind and not t['reversed'] and ids.intersection((t['source_id'], t['target_id']))
           for t in ledger.credit_transfers.values()):
        raise CashflowError('This record supports active carried credit. Reverse its dependent credit transfers first, then correct the source and review fresh transfers.')


def guard_credit_operation(ledger, kind, payload):
    """Shared owner boundary, after authorization/replay and before mutation."""
    if kind=='item_identity_confirmed' and payload.get('kind')=='bill' and any(
        r['bill_id']==payload.get('item_id') for loan in ledger.loans.values() for r in loan['schedule']):
        raise CashflowError('Loan lender/reference comes from its immutable source register; generic bill identity edits are not supported.')
    if not ledger.credit_transfers: return
    if kind in {'source_amount_adjusted', 'item_identity_confirmed', 'refund_observed'}:
        credit_locked(ledger, payload.get('kind'), [payload.get('item_id')])
    elif kind=='refund_corrected':
        from .extension import refund_entries
        for source_kind,item,entry in refund_entries(ledger):
            if entry['id']==payload.get('refund_id'):credit_locked(ledger,source_kind,[item.id])
    elif kind == 'receipt_corrected':
        from .settlement import effective_receipt
        r = ledger.receipts.get(payload.get('receipt_id'))
        credit_locked(ledger, 'invoice', [payload.get('invoice_id'), effective_receipt(ledger, r.id).receivable_id if r else None])
    elif kind == 'bill_payment_corrected':
        credit_locked(ledger, 'bill', [payload.get('bill_id')])
    elif kind == 'payment_date_corrected':
        from .payment_review import observed_movements
        row = next((r for r in observed_movements(ledger) if r['key'] == payload.get('movement_key')), None)
        if row:
            if row['kind'] == 'split_payment':
                from .settlement import effective_receipt
                credit_locked(ledger, 'invoice', [effective_receipt(ledger,r).receivable_id for r in ledger.split_receipts[row['id']]['allocation_ids']])
            else:
                credit_locked(ledger, 'invoice', [row['target']]); credit_locked(ledger, 'bill', [row['target']])


def carry_credit(ledger, transfer_id, kind, source_id, target_id, amount, evidence_id, note, revision, *, ctx):
    payload = dict(transfer_id=transfer_id, kind=kind, source_id=source_id, target_id=target_id,
        amount_minor=amount, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'credit_carried', revision, payload)
    if old: return old
    _id(transfer_id); _amount(amount); _evidence(ledger, evidence_id); _text(note, 'Credit application source check')
    if transfer_id in ledger.credit_transfers: raise IdempotencyConflict('Transfer ID already exists; old credit cannot be revived or overwritten.')
    if len(ledger.credit_transfers) >= 300: raise CashflowError('At most 300 credit transfers are supported.')
    group = ledger.receivables if kind == 'invoice' else ledger.outflows if kind == 'bill' else {}
    source, target = group.get(source_id), group.get(target_id)
    if source is None or target is None or source_id == target_id:
        raise CashflowError('Choose distinct source/target records of the same customer or supplier role.')
    from .treasury import same_party
    if source.currency != target.currency or not same_party(ledger, kind, source, target):
        raise CashflowError('Credit requires the same reviewed party ID (or exact unregistered nonblank labels) and currency. No role/party/FX netting.')
    _currency(ledger, source.currency)
    if kind == 'bill' and (source.protected or target.protected): raise CashflowError('Protected obligations, including payroll and loan instalments, cannot use generic credit transfers.')
    if kind == 'invoice' and (collection_held(source) or collection_held(target)):
        raise CashflowError('Resolve held/disputed sources before credit application.')
    from .extension import _refund_dates, _refresh_bill
    if max(_refund_dates(ledger, kind, source) + _refund_dates(ledger, kind, target)) > ledger.as_of:
        raise CashflowError('Credit applies at the current snapshot only after all related observed/correction dates. Future observations cannot supply current credit.')
    if kind == 'invoice' and any(not a.observed for item in (source,target) for a in item.allocations):
        raise CashflowError('Projected allocations cannot supply or receive recorded credit.')
    available = source.credit_balance_minor if kind == 'invoice' else source.overpaid_minor
    if amount > available or amount > target.remaining_minor:
        raise CashflowError('Credit exceeds available source credit or target outstanding capacity.')
    # Materialize legacy observed payments before changing settlement totals.
    if kind == 'bill':
        for item in (source, target):
            if item.status == 'paid' and not item.observed_payments and not item.legacy_payment:
                entries = bill_payment_entries(item)
                if not entries: raise CashflowError('Legacy paid source needs a dated payment observation.')
                item.legacy_payment = copy.deepcopy(entries[0])
    t = dict(payload, id=transfer_id, currency=source.currency, party=source.counterparty,
        date=str(ledger.as_of), actor=ctx.actor, reversed=False, reversal=None)
    for item, sign in ((source,-1),(target,1)):
        item.credit_entries.append(dict(transfer_id=transfer_id, delta_minor=sign*amount, date=str(ledger.as_of), evidence_id=evidence_id, reversal=False))
    ledger.credit_transfers[transfer_id] = t
    if kind == 'bill':
        for item in (source,target): _refresh_bill(ledger,item)
    return _finish(ledger,key,'credit_carried',payload,dict(transfer_id=transfer_id,source_credit_minor=available-amount,
        target_remaining_minor=target.remaining_minor,cash_delta_minor=0),ctx)


def reverse_credit(ledger, transfer_id, evidence_id, note, revision, *, ctx):
    payload=dict(transfer_id=transfer_id,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'credit_carry_reversed',revision,payload)
    if old:return old
    t=ledger.credit_transfers.get(transfer_id)
    if t is None or t['reversed']:raise CashflowError('Choose an active carried credit; reversal never revives old terms.')
    _evidence(ledger,evidence_id);_text(note,'Credit reversal reason')
    active=[r for r in ledger.credit_transfers.values() if not r['reversed'] and r['kind']==t['kind']]
    index=active.index(t)
    if any({t['source_id'],t['target_id']}.intersection((r['source_id'],r['target_id'])) for r in active[index+1:]):
        raise CashflowError('Reverse later dependent transfers first, preserving the credit chain.')
    group=ledger.receivables if t['kind']=='invoice' else ledger.outflows
    for item,sign in ((group[t['source_id']],1),(group[t['target_id']],-1)):
        item.credit_entries.append(dict(transfer_id=transfer_id,delta_minor=sign*t['amount_minor'],date=str(ledger.as_of),evidence_id=evidence_id,reversal=True))
    t.update(reversed=True,reversal=dict(date=str(ledger.as_of),evidence_id=evidence_id,note=note,actor=ctx.actor))
    if t['kind']=='bill':
        from .extension import _refresh_bill
        for ident in (t['source_id'],t['target_id']):_refresh_bill(ledger,group[ident])
    return _finish(ledger,key,'credit_carry_reversed',payload,dict(transfer_id=transfer_id,cash_delta_minor=0),ctx)


def register_loan(ledger, spec, revision, *, ctx):
    payload=dict(spec=spec)
    key,old=_begin(ledger,ctx,'loan_schedule_registered',revision,payload)
    if old:return old
    if not isinstance(spec,dict) or set(spec)!={'id','lender','reference','currency','opening_principal_minor','schedule','evidence_id','note','already_in_opening','not_recorded_elsewhere'}:
        raise CashflowError('Use only the loan register fields.')
    s=spec;_id(s['id']);_currency(ledger,s['currency']);_amount(s['opening_principal_minor'])
    _evidence(ledger,s['evidence_id']);_text(s['note'],'Existing loan/source check')
    if len(s['id'])>40:raise CashflowError('Loan IDs support at most 40 characters.')
    for field in ('lender','reference'):
        if not isinstance(s[field],str) or not 1<=len(s[field].strip())<=200:raise CashflowError('Lender and agreement reference must contain 1–200 characters.')
    if s['already_in_opening'] is not True or s['not_recorded_elsewhere'] is not True:
        raise CashflowError('Confirm this existing debt is already represented in cash/opening and its future bills are not recorded elsewhere. Registering debt creates no drawdown cash.')
    if s['id'] in ledger.loans:raise IdempotencyConflict('Loan ID already exists; no schedule overwrite or duplicate bills.')
    if len(ledger.loans)>=24:raise CashflowError('At most 24 existing loans are supported.')
    voided_bills={r['bill_id'] for old_loan in ledger.loans.values() if old_loan.get('voided') for r in old_loan['schedule']}
    if any(o.id not in voided_bills and o.counterparty==s['lender'].strip() and o.invoice_reference==s['reference'].strip() for o in ledger.outflows.values()):
        raise CashflowError('An existing bill already uses this lender/agreement reference. Review it before registering duplicate obligations.')
    if not isinstance(s['schedule'],list) or not 1<=len(s['schedule'])<=60:raise CashflowError('Use 1–60 explicit dated instalments.')
    if len(ledger.outflows)+len(ledger.receivables)+len(s['schedule'])>600:raise CashflowError('Loan bills would exceed the 600-record bound.')
    rows=[];days=[];principal=0
    for i,r in enumerate(s['schedule']):
        if not isinstance(r,dict) or set(r)!={'date','principal_minor','interest_minor'}:raise CashflowError('Each instalment needs date, principal and interest only.')
        try:d=date.fromisoformat(r['date'])
        except (ValueError,TypeError):raise CashflowError('Each instalment needs a valid ISO date.')
        _amount(r['principal_minor'],True);_amount(r['interest_minor'],True);_amount(r['principal_minor']+r['interest_minor'])
        if not ledger.as_of<=d<=ledger.as_of+timedelta(days=3650):raise CashflowError('Instalments must fall from the opening date through 3,650 days ahead.')
        ident=f"loan-{s['id']}-{i+1}"
        if ident in ledger.outflows or ident in ledger.receivables:raise IdempotencyConflict('Generated instalment ID already exists.')
        rows.append(dict(r,bill_id=ident));days.append(d);principal+=r['principal_minor']
    if days!=sorted(set(days)) or principal!=s['opening_principal_minor']:
        raise CashflowError('Use strictly increasing dates and principal portions equal to exact opening principal. Interest is explicit, not calculated or guessed.')
    loan=copy.deepcopy(dict(s,schedule=rows,opening_date=str(ledger.as_of),actor=ctx.actor))
    for r in rows:
        ledger.outflows[r['bill_id']]=Outflow(r['bill_id'],s['currency'],r['principal_minor']+r['interest_minor'],
            date.fromisoformat(r['date']),True,s['evidence_id'],counterparty=s['lender'].strip(),invoice_reference=s['reference'].strip(),identity_evidence_id=s['evidence_id'])
    ledger.loans[s['id']]=loan
    return _finish(ledger,key,'loan_schedule_registered',payload,dict(loan_id=s['id'],bill_ids=[r['bill_id'] for r in rows],cash_delta_minor=0),ctx)


def void_loan_registration(ledger,loan_id,evidence_id,note,revision,*,ctx):
    payload=dict(loan_id=loan_id,evidence_id=evidence_id,note=note)
    key,old=_begin(ledger,ctx,'erroneous_loan_registration_voided',revision,payload)
    if old:return old
    loan=ledger.loans.get(loan_id)
    if loan is None or loan.get('voided'):raise CashflowError('Choose an active erroneous loan registration.')
    _evidence(ledger,evidence_id);_text(note,'Evidence for erroneous / duplicate registration')
    if any(bill_payment_entries(ledger.outflows[r['bill_id']]) for r in loan['schedule']):
        raise CashflowError('A loan with any payment history cannot be voided. Preserve observations; refinancing/paid schedule amendments are outside this workflow.')
    for r in loan['schedule']:
        out=ledger.outflows[r['bill_id']]
        out.face_adjustments.append(dict(id='loan-void-'+key[:24],old_total_minor=out.effective_total_minor,new_total_minor=0,
            date=str(ledger.as_of),evidence_id=evidence_id,note=note,actor=ctx.actor,schedule=[]))
        out.status,out.paid_date='adjusted',None
    loan['voided']=dict(date=str(ledger.as_of),evidence_id=evidence_id,note=note,actor=ctx.actor)
    return _finish(ledger,key,'erroneous_loan_registration_voided',payload,dict(loan_id=loan_id,cash_delta_minor=0),ctx)


def loan_report(ledger, *, ctx):
    require_access(ctx,ledger,OP_READ);result=[]
    events=list(cash_events(ledger))
    for loan in ledger.loans.values():
        rows=[]
        for r in loan['schedule']:
            out=ledger.outflows[r['bill_id']]
            # Current principal uses dated observed cash, not a future payment or
            # future accounting compensation already entered in the sandbox.
            paid=-sum(e['amount_minor'] for e in events if e.get('outflow_id')==out.id and e['date']<=ledger.as_of)
            interest_paid=min(paid,r['interest_minor']);principal_paid=min(max(0,paid-interest_paid),r['principal_minor'])
            rows.append(dict(r,paid_minor=out.paid_minor,paid_through_snapshot_minor=paid,interest_paid_minor=interest_paid,principal_paid_minor=principal_paid,
                remaining_minor=out.remaining_minor,overdue=out.remaining_minor>0 and out.date<ledger.as_of))
        result.append(dict(copy.deepcopy(loan),rows=rows,
            outstanding_principal_minor=0 if loan.get('voided') else loan['opening_principal_minor']-sum(r['principal_paid_minor'] for r in rows),
            unpaid_interest_minor=0 if loan.get('voided') else sum(r['interest_minor']-r['interest_paid_minor'] for r in rows),
            unpaid_total_minor=0 if loan.get('voided') else sum(r['principal_minor']+r['interest_minor']-r['paid_through_snapshot_minor'] for r in rows),
            note_boundary='Existing source-attested debt only; no drawdown cash, lending approval or calculated APR. Principal/interest position uses dated observed cash through the visible snapshot, allocated interest-first inside each instalment. Row settlement and remaining schedule include explicitly recorded future observations/corrections; these are not proof of payment today. Schedule amendments/refinancing after payment history require a separate future workflow.'))
    return result
