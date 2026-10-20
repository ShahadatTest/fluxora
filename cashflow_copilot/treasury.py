"""Reviewed party identities and account-scoped observed cash. Owner only.

No bank verification, payment execution, FX funding or model authority. Identity
links never rewrite source names. Restrictions reduce decision cash, not book cash.
"""
import copy
from datetime import date

from .authz import OP_READ, require_access
from .completion import _id, _amount
from .errors import CashflowError, IdempotencyConflict
from .models import Account
from .owner_workflows import _begin, _finish, _evidence, _text


STATE_FIELDS = ('party_register', 'party_links', 'account_details', 'cash_defaults',
                'movement_accounts', 'statement_accounts', 'internal_transfers', 'cash_restrictions', 'forecast_routes', 'statement_profiles')


def currency_accounts(ledger, currency):
    return [a for a in ledger.accounts.values() if a.currency == currency]


def currency_opening(ledger, currency):
    return sum(a.opening_minor for a in currency_accounts(ledger, currency))


def currency_floor(ledger, currency):
    return sum(a.floor_minor for a in currency_accounts(ledger, currency))


def default_account(ledger, currency):
    accounts = currency_accounts(ledger, currency)
    if not accounts:
        raise CashflowError('This currency has no cash account. No FX conversion is made.')
    return ledger.cash_defaults.get(currency, accounts[0].id)


def _source(ledger, evidence_id, note):
    _evidence(ledger, evidence_id)
    _text(note, 'Checked source / reason')


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise CashflowError('Use a nonblank name of at most 200 characters.')
    return value.strip()


def _unique_name(ledger, role, name, except_id=''):
    if any(p['id'] != except_id and p['role'] == role and name.casefold() in
           {s.casefold() for s in [p['name'], *p['aliases']]} for p in ledger.party_register.values()):
        raise CashflowError('This name/alias already belongs to a registered party in this role. No automatic merge.')


def register_party(ledger, party_id, role, name, evidence_id, note, revision, *, ctx):
    payload = dict(party_id=party_id, role=role, name=name, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'party_registered', revision, payload)
    if old: return old
    _id(party_id); name = _label(name); _source(ledger, evidence_id, note)
    if role not in {'customer', 'supplier'}: raise CashflowError('Choose customer or supplier.')
    if party_id in ledger.party_register: raise IdempotencyConflict('Party ID already exists.')
    if len(ledger.party_register) >= 500: raise CashflowError('At most 500 registered parties are supported.')
    _unique_name(ledger, role, name)
    ledger.party_register[party_id] = dict(id=party_id, role=role, name=name, aliases=[],
        evidence_id=evidence_id, history=[dict(step='registered', name=name, evidence_id=evidence_id, note=note, actor=ctx.actor)])
    return _finish(ledger, key, 'party_registered', payload, dict(party_id=party_id), ctx)


def update_party(ledger, party_id, step, name, evidence_id, note, revision, *, ctx):
    payload = dict(party_id=party_id, step=step, name=name, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'party_identity_updated', revision, payload)
    if old: return old
    p = ledger.party_register.get(party_id)
    if p is None or step not in {'alias', 'rename', 'retire_alias'}: raise CashflowError('Choose a registered party and alias, rename or retire alias.')
    name = _label(name); _source(ledger, evidence_id, note); _unique_name(ledger, p['role'], name, party_id)
    if len(p['history']) >= 100: raise CashflowError('At most 100 identity events per party.')
    if step == 'retire_alias':
        if name not in p['aliases']: raise CashflowError('Choose an existing exact alias; the current name cannot be retired.')
        if any(link['party_id'] == party_id and link['source_name'].strip().casefold() == name.casefold() for link in ledger.party_links.values()):
            raise CashflowError('Unlink records using this alias first. Active credit must be reversed before unlinking.')
        p['aliases'].remove(name)
    elif name == p['name'] or (step == 'alias' and name in p['aliases']): raise CashflowError('This exact name is already recorded.')
    elif step == 'alias': p['aliases'].append(name)
    else:
        if name in p['aliases']: p['aliases'].remove(name)
        p['aliases'].append(p['name']); p['name'] = name
    p['history'].append(dict(step=step, name=name, evidence_id=evidence_id, note=note, actor=ctx.actor))
    return _finish(ledger, key, 'party_identity_updated', payload, dict(party_id=party_id), ctx)


def link_party(ledger, kind, item_id, party_id, evidence_id, note, revision, *, ctx):
    payload = dict(kind=kind, item_id=item_id, party_id=party_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'party_record_linked', revision, payload)
    if old: return old
    group = ledger.receivables if kind == 'invoice' else ledger.outflows if kind == 'bill' else {}
    item, p = group.get(item_id), ledger.party_register.get(party_id)
    if item is None or (party_id and (p is None or p['role'] != ('customer' if kind == 'invoice' else 'supplier'))):
        raise CashflowError('Choose an existing record and registered party in the same role.')
    _source(ledger, evidence_id, note)
    from .obligations import credit_locked
    credit_locked(ledger, kind, [item_id])
    if not party_id:
        if kind + ':' + item_id not in ledger.party_links: raise CashflowError('This record has no identity link to remove.')
        del ledger.party_links[kind + ':' + item_id]
        return _finish(ledger, key, 'party_record_linked', payload, dict(party_id='', item_id=item_id), ctx)
    if item.counterparty.strip().casefold() not in {s.casefold() for s in [p['name'], *p['aliases']]}:
        raise CashflowError('Review this source name as a registered alias first. No fuzzy identity match is made.')
    ledger.party_links[kind + ':' + item_id] = dict(party_id=party_id, source_name=item.counterparty,
        evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'party_record_linked', payload, dict(party_id=party_id, item_id=item_id), ctx)


def linked_party(ledger, kind, item):
    link = ledger.party_links.get(kind + ':' + item.id)
    return link['party_id'] if link and link['source_name'] == item.counterparty else None


def same_party(ledger, kind, left, right):
    a, b = linked_party(ledger, kind, left), linked_party(ledger, kind, right)
    if a or b: return bool(a and a == b)
    return bool(left.counterparty.strip() and left.counterparty == right.counterparty)


def party_summary(ledger, party_id, *, ctx):
    require_access(ctx, ledger, OP_READ)
    p = ledger.party_register.get(party_id)
    if p is None: raise CashflowError('Choose a registered party.')
    kind, group = ('invoice', ledger.receivables) if p['role'] == 'customer' else ('bill', ledger.outflows)
    rows = [dict(id=i.id, source_name=i.counterparty, currency=i.currency, remaining_minor=i.remaining_minor,
                 credit_minor=i.credit_balance_minor if kind == 'invoice' else i.overpaid_minor,
                 evidence_id=i.evidence_id) for i in group.values() if linked_party(ledger, kind, i) == party_id]
    currencies = {}
    for row in rows:
        totals = currencies.setdefault(row['currency'], dict(remaining_minor=0, credit_minor=0))
        for field in totals: totals[field] += row[field]
    return dict(copy.deepcopy(p), records=rows, currencies=currencies,
                note='Owner-reviewed identity links only; not verified legal identities. Credit requires a separate explicit operation.')


def add_account(ledger, account_id, currency, name, evidence_id, note, revision, *, ctx):
    payload = dict(account_id=account_id, currency=currency, name=name, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'cash_account_added', revision, payload)
    if old: return old
    _id(account_id); name = _label(name); _source(ledger, evidence_id, note)
    if account_id in ledger.accounts: raise IdempotencyConflict('Account ID already exists.')
    if len(ledger.accounts) >= 20: raise CashflowError('At most 20 cash accounts are supported.')
    # Add alongside a currently supported currency. New money requires source-
    # backed daily cash confirmation or an observed movement, never this form.
    original = default_account(ledger, currency)
    ledger.cash_defaults.setdefault(currency, original)
    ledger.accounts[account_id] = Account(account_id, currency, 0, 0, evidence_id)
    ledger.account_details[account_id] = dict(name=name, evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'cash_account_added', payload, dict(account_id=account_id, opening_minor=0), ctx)


def event_key(ledger, event):
    kind, ident = event['kind'], event['id']
    if kind.startswith('internal_transfer'): return kind + ':' + ident
    if kind in {'observed_receipt', 'receipt_correction'}:
        ident = event.get('receipt_id', ident)
        return ('split_payment:' if ident in ledger.split_receipts else 'receipt:') + ident
    if kind in {'observed_bill_payment', 'bill_payment_correction'}:
        return 'bill_payment:' + event.get('payment_id', ident)
    if kind in {'refund_observed', 'refund_correction'}: return 'refund:' + event.get('refund_id', ident)
    if kind.startswith('unallocated_refund'):
        if kind == 'unallocated_refund_correction':
            ident = next(r['id'] for r in ledger.unallocated_refunds.values() if any(c['id'] == ident for c in r['adjustments']))
        return 'unallocated_refund:' + ident
    if kind == 'other_cash_correction':
        ident = next(m['id'] for m in ledger.cash_movements.values() if any(c['id'] == ident for c in m['adjustments']))
    return 'other_cash:' + ident


def event_account(ledger, event):
    if event.get('account_id'): return event['account_id']
    binding = ledger.movement_accounts.get(event_key(ledger, event))
    return binding['account_id'] if binding else default_account(ledger, event['currency'])


def account_cash(ledger, account_id, on_date=None):
    account = ledger.accounts.get(account_id)
    if account is None: raise CashflowError('Choose an account in this business.')
    on_date = on_date or ledger.as_of
    if type(on_date) is not date or on_date < ledger.as_of: raise CashflowError('Account history before the current opening is unavailable.')
    from .settlement import cash_events
    return account.opening_minor + sum(e['amount_minor'] for e in cash_events(ledger, include_internal=True)
        if e['account_id'] == account_id and ledger.as_of <= e['date'] <= on_date)


def restricted_cash(ledger, *, account_id=None, currency=None):
    return sum(r['amount_minor'] for r in ledger.cash_restrictions.values() if not r['release']
               and (account_id is None or r['account_id'] == account_id)
               and (currency is None or ledger.accounts[r['account_id']].currency == currency))


def treasury_report(ledger, *, ctx):
    require_access(ctx, ledger, OP_READ)
    accounts, currencies = [], {}
    for a in ledger.accounts.values():
        book = account_cash(ledger, a.id); held = restricted_cash(ledger, account_id=a.id)
        row = dict(account_id=a.id, name=ledger.account_details.get(a.id, {}).get('name', a.id), currency=a.currency,
                   opening_minor=a.opening_minor, book_minor=book, restricted_minor=held,
                   available_minor=book-held, floor_minor=a.floor_minor, evidence_id=a.evidence_id,
                   default=default_account(ledger, a.currency) == a.id)
        accounts.append(row)
        total = currencies.setdefault(a.currency, dict(book_minor=0, restricted_minor=0, available_minor=0, floor_minor=0))
        for field in total: total[field] += row[field]
    return dict(accounts=accounts, currencies=currencies,
        restrictions=copy.deepcopy(list(ledger.cash_restrictions.values())),
        transfers=copy.deepcopy(list(ledger.internal_transfers.values())),
        note='Recorded synthetic observations, not bank-verified balances. Existing ordinary observations use the labelled default account until explicitly assigned. Currency forecasts pool available accounts and sum their floor buffers; they do not choose a payment account or guarantee account-level funding. Restricted cash is deducted once; payroll remains a scheduled obligation, not a second restriction.')


def assign_movement(ledger, movement_key, account_id, evidence_id, note, revision, *, ctx):
    payload = dict(movement_key=movement_key, account_id=account_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'movement_account_confirmed', revision, payload)
    if old: return old
    _source(ledger, evidence_id, note)
    from .payment_review import observed_movements
    row = next((r for r in observed_movements(ledger) if r['key'] == movement_key), None)
    a = ledger.accounts.get(account_id)
    if row is None or a is None or row['currency'] != a.currency or row['kind'].startswith('internal_transfer'):
        raise CashflowError('Choose an ordinary observed movement and an account in its own currency.')
    # Moving only some already-absorbed events would corrupt a later opening.
    from .settlement import cash_events
    if any(e['date'] < ledger.as_of for e in cash_events(ledger) if event_key(ledger, e) == movement_key):
        raise CashflowError('This movement has history absorbed into the opening. Historical account reassignment is unsupported.')
    ledger.movement_accounts[movement_key] = dict(account_id=account_id, evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'movement_account_confirmed', payload, dict(movement_key=movement_key, account_id=account_id), ctx)


def bind_statement(ledger, batch_id, account_id, evidence_id, note, revision, *, ctx):
    payload = dict(batch_id=batch_id, account_id=account_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'statement_account_confirmed', revision, payload)
    if old: return old
    _source(ledger, evidence_id, note)
    batch, account = ledger.statement_batches.get(batch_id), ledger.accounts.get(account_id)
    if batch is None or account is None or any(r['currency'] != account.currency for r in batch['rows']):
        raise CashflowError('Choose a statement containing only this account currency.')
    if batch_id in ledger.statement_accounts: raise CashflowError('This statement already has immutable account scope; import a corrected source under a new ID.')
    if any(c['batch_id'] == batch_id and c['account_id'] != account_id for c in ledger.statement_balance_checks.values()):
        raise CashflowError('Existing boundary terms name another account. Review a corrected imported source instead.')
    ledger.statement_accounts[batch_id] = dict(account_id=account_id, evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'statement_account_confirmed', payload, dict(batch_id=batch_id, account_id=account_id), ctx)


def statement_account(ledger, batch_id):
    binding = ledger.statement_accounts.get(batch_id)
    return binding['account_id'] if binding else None


def observed_transfer(ledger, transfer_id, source_id, target_id, amount, day, evidence_id, note, revision, *, ctx, acknowledge_duplicates=False):
    payload = dict(transfer_id=transfer_id, source_id=source_id, target_id=target_id, amount_minor=amount,
                   date=str(day), evidence_id=evidence_id, note=note, acknowledge_duplicates=acknowledge_duplicates)
    key, old = _begin(ledger, ctx, 'internal_transfer_observed', revision, payload)
    if old: return old
    _id(transfer_id); _amount(amount); _source(ledger, evidence_id, note)
    if transfer_id in ledger.internal_transfers: raise IdempotencyConflict('Transfer ID already exists.')
    if len(ledger.internal_transfers) >= 500: raise CashflowError('At most 500 observed internal transfers are supported.')
    a, b = ledger.accounts.get(source_id), ledger.accounts.get(target_id)
    if a is None or b is None or a.id == b.id or a.currency != b.currency:
        raise CashflowError('Choose two different own accounts in the same currency. No FX or external payment.')
    if type(day) is not date or day != ledger.as_of:
        raise CashflowError('Record an already-observed transfer on the current business snapshot date only; no future transfer is executed.')
    if amount > account_cash(ledger, a.id) - restricted_cash(ledger, account_id=a.id):
        raise CashflowError('Source account has insufficient unrestricted recorded cash.')
    from .payment_review import observed_movements
    candidates = [r for r in observed_movements(ledger) if r['date'] == str(day) and r['currency'] == a.currency
        and ((r['signed_minor'] == -amount and r.get('account_id', default_account(ledger,a.currency)) == a.id)
             or (r['signed_minor'] == amount and r.get('account_id', default_account(ledger,a.currency)) == b.id))]
    if candidates and acknowledge_duplicates is not True:
        raise CashflowError('Possible duplicate transfer/observed movement: '+', '.join(r['key'] for r in candidates)+'. Check the source and explicitly acknowledge distinct movements before recording.')
    ledger.internal_transfers[transfer_id] = dict(payload, id=transfer_id, currency=a.currency, actor=ctx.actor, reversal=None,
        possible_duplicate_keys=[r['key'] for r in candidates])
    return _finish(ledger, key, 'internal_transfer_observed', payload, dict(transfer_id=transfer_id, business_cash_delta_minor=0), ctx)


def reverse_transfer(ledger, transfer_id, evidence_id, note, revision, *, ctx):
    payload = dict(transfer_id=transfer_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'internal_transfer_reversed', revision, payload)
    if old: return old
    _source(ledger, evidence_id, note); t = ledger.internal_transfers.get(transfer_id)
    if t is None or t['reversal']: raise CashflowError('Choose an unreversed observed internal transfer.')
    if t['amount_minor'] > account_cash(ledger, t['target_id']) - restricted_cash(ledger, account_id=t['target_id']):
        raise CashflowError('Destination no longer has sufficient unrestricted cash to record the compensating reversal.')
    t['reversal'] = dict(date=str(ledger.as_of), evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'internal_transfer_reversed', payload, dict(transfer_id=transfer_id, business_cash_delta_minor=0), ctx)


def transfer_events(ledger):
    for t in ledger.internal_transfers.values():
        for reversed_, terms in [(False, t), (True, t['reversal'])]:
            if not terms: continue
            source, target = (t['target_id'], t['source_id']) if reversed_ else (t['source_id'], t['target_id'])
            for side, account, sign in [('out', source, -1), ('in', target, 1)]:
                yield dict(id=t['id'], kind=('internal_transfer_reversal_' if reversed_ else 'internal_transfer_') + side,
                    account_id=account, currency=t['currency'], date=date.fromisoformat(terms['date']),
                    amount_minor=sign*t['amount_minor'], evidence_id=terms['evidence_id'], transfer_id=t['id'])


def restrict_cash(ledger, restriction_id, account_id, amount, purpose, evidence_id, note, revision, *, ctx):
    payload = dict(restriction_id=restriction_id, account_id=account_id, amount_minor=amount,
                   purpose=purpose, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'cash_restriction_recorded', revision, payload)
    if old: return old
    _id(restriction_id); _amount(amount); _source(ledger, evidence_id, note)
    if restriction_id in ledger.cash_restrictions: raise IdempotencyConflict('Restriction ID already exists.')
    if len(ledger.cash_restrictions) >= 500: raise CashflowError('At most 500 cash restriction records.')
    if purpose not in {'restricted_deposit', 'legal_hold', 'unavailable_funds'}:
        raise CashflowError('Choose an externally restricted deposit, legal hold or source-backed unavailable funds. Payroll and cash-floor buffers are already represented; do not reserve them twice.')
    if amount > account_cash(ledger, account_id) - restricted_cash(ledger, account_id=account_id):
        raise CashflowError('Restriction exceeds this account\'s currently unrestricted recorded cash.')
    ledger.cash_restrictions[restriction_id] = dict(payload, id=restriction_id, date=str(ledger.as_of), actor=ctx.actor, release=None)
    return _finish(ledger, key, 'cash_restriction_recorded', payload, dict(restriction_id=restriction_id, book_cash_delta_minor=0), ctx)


def release_cash(ledger, restriction_id, evidence_id, note, revision, *, ctx):
    payload = dict(restriction_id=restriction_id, evidence_id=evidence_id, note=note)
    key, old = _begin(ledger, ctx, 'cash_restriction_released', revision, payload)
    if old: return old
    _source(ledger, evidence_id, note); r = ledger.cash_restrictions.get(restriction_id)
    if r is None or r['release']: raise CashflowError('Choose an active restriction.')
    r['release'] = dict(date=str(ledger.as_of), evidence_id=evidence_id, note=note, actor=ctx.actor)
    return _finish(ledger, key, 'cash_restriction_released', payload, dict(restriction_id=restriction_id, book_cash_delta_minor=0), ctx)
