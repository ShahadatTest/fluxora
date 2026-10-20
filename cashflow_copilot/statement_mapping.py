"""Explicit CSV interpretation. Profiles are parsing preferences, never authority."""
import copy
import csv
import hashlib
from datetime import date, datetime, timedelta
from io import StringIO
import re

from .authz import OP_READ, require_access
from .errors import CashflowError, IdempotencyConflict
from .money import minor_digits
from .owner_workflows import _text

DEFAULT_MAPPING = dict(delimiter=',', date_format='iso', mode='signed', decimal='.',
    id_column='id', date_column='date', reference_column='reference', currency_column='currency',
    amount_column='amount', debit_column='', credit_column='')
DATE_FORMATS = {'iso':'%Y-%m-%d', 'dmy':'%d/%m/%Y', 'mdy':'%m/%d/%Y'}


def validate_mapping(mapping):
    if not isinstance(mapping,dict) or set(mapping)!=set(DEFAULT_MAPPING):
        raise CashflowError('Use the current complete statement mapping fields.')
    if mapping['delimiter'] not in {',',';','\t'} or mapping['date_format'] not in DATE_FORMATS or mapping['mode'] not in {'signed','debit_credit'} or mapping['decimal'] not in {'.',','}:
        raise CashflowError('Choose a listed delimiter, date format, amount mode and decimal separator.')
    columns=[k for k in mapping if k.endswith('_column')]
    if any(not isinstance(mapping[k],str) or len(mapping[k])>120 for k in columns):
        raise CashflowError('Column names must be text up to 120 characters.')
    required=['date_column']+(['amount_column'] if mapping['mode']=='signed' else ['debit_column','credit_column'])
    if any(not mapping[k] for k in required):raise CashflowError('Map the date and required amount columns explicitly.')
    active=['id_column','date_column','reference_column','currency_column']+(['amount_column'] if mapping['mode']=='signed' else ['debit_column','credit_column'])
    selected=[mapping[k] for k in active if mapping[k]]
    if len(selected)!=len(set(selected)):raise CashflowError('Each mapped column must serve one purpose only.')
    return copy.deepcopy(mapping)


def validate_context(ledger,context,rows):
    if not isinstance(context,dict) or set(context)!={'account_id','start','end','mapping'}:
        raise CashflowError('Mapped statements require an explicit account, inclusive period and mapping.')
    a=ledger.accounts.get(context['account_id'])
    try:start,end=date.fromisoformat(context['start']),date.fromisoformat(context['end'])
    except (TypeError,ValueError):raise CashflowError('Use valid ISO statement period dates.') from None
    if a is None or not start<=end<=start+timedelta(days=366):
        raise CashflowError('Choose an own account and inclusive statement period of at most 367 dates.')
    validate_mapping(context['mapping'])
    if any(r['currency']!=a.currency or not start<=date.fromisoformat(r['date'])<=end for r in rows):
        raise CashflowError('All statement rows must use the selected account currency and fall inside its period.')
    return a


def _minor(raw,currency,decimal,*,unsigned=False):
    raw=raw.strip()
    # No inferred grouping, currency symbols, parentheses, exponents or rounding.
    pattern=r'[0-9]{1,13}(?:'+re.escape(decimal)+r'[0-9]{1,3})?'
    if not unsigned:pattern=r'-?'+pattern
    if not re.fullmatch(pattern,raw):raise CashflowError('Use ungrouped ordinary amounts with the selected decimal separator; debit/credit columns must be nonnegative.')
    whole,sep,fraction=raw.replace(decimal,'.').partition('.')
    digits=minor_digits(currency)
    if len(fraction)>digits:raise CashflowError('Statement precision exceeds this currency; no rounding is applied.')
    sign=-1 if whole.startswith('-') else 1
    value=sign*(int(whole.lstrip('-'))*10**digits+int((fraction+'0'*digits)[:digits] or '0'))
    if abs(value)>10**15:raise CashflowError('Statement amount exceeds the supported money bound.')
    return value


def normalize_statement(ledger,data,mapping,account_id,start,end,*,ctx):
    require_access(ctx,ledger,OP_READ)
    mapping=validate_mapping(mapping)
    context=dict(account_id=account_id,start=start,end=end,mapping=mapping)
    a=validate_context(ledger,context,[])
    if not isinstance(data,bytes) or len(data)>1048576:raise CashflowError('Upload a UTF-8 CSV up to 1 MiB.')
    try:
        reader=csv.reader(StringIO(data.decode('utf-8-sig')),delimiter=mapping['delimiter'],strict=True)
        headers=next(reader)
        if not headers or any(not h or len(h)>120 for h in headers) or len(headers)!=len(set(headers)) or len(headers)>50:
            raise CashflowError('Use 1–50 unique nonempty CSV headers (at most 120 characters each).')
        active=['id_column','date_column','reference_column','currency_column']+(['amount_column'] if mapping['mode']=='signed' else ['debit_column','credit_column'])
        selected=[mapping[k] for k in active if mapping[k]]
        if any(c not in headers for c in selected):raise CashflowError('A mapped column is missing. Review exact header spelling; no automatic mapping is applied.')
        rows=[]
        for number,values in enumerate(reader,1):
            if not values:continue
            if len(values)!=len(headers):raise CashflowError('Statement row has incomplete/extra columns.')
            raw=dict(zip(headers,values))
            def value(key):return raw[mapping[key]].strip() if mapping[key] else ''
            ccy=value('currency_column').upper() if mapping['currency_column'] else a.currency
            if ccy!=a.currency:raise CashflowError('Statement row currency differs from selected account; no FX conversion.')
            try:
                dt=datetime.strptime(value('date_column'),DATE_FORMATS[mapping['date_format']]).date()
                if dt.strftime(DATE_FORMATS[mapping['date_format']])!=value('date_column'):raise ValueError()
            except ValueError:raise CashflowError('A row date does not exactly match the selected date format.') from None
            if mapping['mode']=='signed':minor=_minor(value('amount_column'),ccy,mapping['decimal'])
            else:
                debit=_minor(value('debit_column') or '0',ccy,mapping['decimal'],unsigned=True)
                credit=_minor(value('credit_column') or '0',ccy,mapping['decimal'],unsigned=True)
                if debit and credit:raise CashflowError('Both debit and credit are nonzero on one row; split/review the source explicitly.')
                minor=credit-debit
            if not minor:raise CashflowError('Transaction rows must be nonzero. Use a header-only CSV for a declared zero-activity period.')
            rows.append(dict(id=value('id_column') if mapping['id_column'] else f'row-{number:03d}',
                date=dt.isoformat(),currency=ccy,signed_minor=minor,reference=value('reference_column')))
            if len(rows)>100:raise CashflowError('Import at most 100 transaction rows per statement.')
    except (UnicodeError,csv.Error,StopIteration):raise CashflowError('Use a valid UTF-8 CSV with a header row.') from None
    validate_context(ledger,context,rows)
    return dict(rows=rows,digest=hashlib.sha256(data).hexdigest(),context=context,
        ignored_columns=[h for h in headers if h not in selected])


def import_mapped_statement(ledger,normalized,note,profile_name,revision,*,ctx):
    from .completion import import_statement
    from .owner_workflows import _begin,_finish
    from .planning import _hash
    payload=dict(normalized=normalized,note=note,profile_name=profile_name)
    key,old=_begin(ledger,ctx,'mapped_statement_imported',revision,payload)
    if old:return old
    mapping=validate_mapping(normalized['context']['mapping'])
    if profile_name:
        _text(profile_name,'Mapping profile name')
        if len(profile_name)>80:raise CashflowError('Profile name must be at most 80 characters.')
        ident='mapping-'+_hash(profile_name.strip().casefold())[:24]
        prior=ledger.statement_profiles.get(ident)
        if prior and prior['mapping']!=mapping:raise IdempotencyConflict('That profile name already has different parsing rules. Use a new name.')
        if not prior and len(ledger.statement_profiles)>=30:raise CashflowError('At most 30 immutable mapping profiles per business.')
    result=import_statement(ledger,normalized['rows'],normalized['digest'],note,revision,ctx=ctx,context=normalized['context'])
    if profile_name:
        ledger.statement_profiles.setdefault(ident,dict(id=ident,business_id=ledger.business_id,
            name=profile_name.strip(),mapping=mapping,actor=ctx.actor))
    return _finish(ledger,key,'mapped_statement_imported',payload,result,ctx)
