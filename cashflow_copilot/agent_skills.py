"""Trusted bundled guidance for existing guided tasks; never executable plugins.

Automatic matching is advisory and may be wrong. Explicit skill selection
narrows existing tool permissions; it can never grant a new permission.
"""
import hashlib
import json
from pathlib import Path
import re

from .errors import CashflowError

VERSION='1.0.0'
_ROOT=Path(__file__).parent/'skills'
_SKILLS=(
    dict(id='cash-review',title='Cash Review',description='Review recorded cash, conditional forecasts, currency-specific gaps and funding sources.',
        reports=['forecast','weekly_cashflow','account_funding','cash_accounts','loans','settlement','constraints','data_freshness','missing_facts','forecast_actual'],
        actions=[],operations=['save_forecast'],report='forecast',example='Show the baseline cash forecast for every currency. Read only.',
        cues=[r'\bcash (?:position|forecast|flow|balance)\b',r'\bforecast\b',r'\bthirteen weeks\b',r'\bloan positions?\b']),
    dict(id='scenario-comparison',title='Scenario Comparison',description='Compare conditional what-if forecasts and bounded allowed plans without changing recorded cash.',
        reports=['forecast','plans','scenario','affordability','constraints','missing_facts'],
        actions=[],operations=['save_scenario','save_forecast'],report='plans',example='Compare the allowed plans. Read only; do not apply any plan.',
        cues=[r'\bwhat[ -]if\b',r'\bscenario\b',r'\bcompare\b',r'\b(?:delay|delayed|later)\b']),
    dict(id='collections-preparation',title='Collections Preparation',description='Review invoice collection priorities and prepare exact early-payment request drafts for later review.',
        reports=['collections','promises','party_statement','missing_facts','forecast','evidence'],
        actions=['request_early_payment'],operations=['create_promise','review_promise','set_collection_hold'],report='collections',example='Show collection priorities. Read only.',
        cues=[r'\bcollect(?:ion|ions)?\b',r'\boverdue invoices?\b',r'\bearly[ -]payment\b',r'\bfollow[ -]up\b']),
    dict(id='payroll-bills-review',title='Payroll & Bills Review',description='Inspect protected payroll and upcoming obligations, account funding and permitted nonprotected bill drafts.',
        reports=['account_funding','calendar','constraints','cash_accounts','forecast','recurring_preview','missing_facts'],
        actions=['request_supplier_extension','defer_ops_expense'],operations=['assign_forecast_route','save_forecast'],report='account_funding',example='Report account-specific payroll funding and account shortfalls. Read only.',
        cues=[r'\bpayroll\b',r'\bbills?\b',r'\bsupplier\b',r'\bfunding\b']),
    dict(id='evidence-reconciliation-review',title='Evidence & Reconciliation Review',description='Inspect evidence gaps, duplicates and unmatched observations; review only, never invent or confirm a payment.',
        reports=['evidence','statement','statement_balance','observation_candidates','receipt_candidates','attachments','duplicate_candidates','data_freshness','missing_facts','settlement','search','forecast_actual'],
        actions=[],operations=[],report='evidence',example='Show the evidence register. Read only.',
        cues=[r'\bevidence\b',r'\breconcil\w*\b',r'\bunmatched\b',r'\bduplicates?\b',r'\bstatement\b']),
    dict(id='daily-action-brief',title='Daily Action Brief',description='Review daily priorities, collection risks, protected obligations and pending action states from recorded facts.',
        reports=['decision_brief','calendar','collections','data_freshness','missing_facts','forecast','account_funding'],
        actions=[],operations=['save_forecast'],report='decision_brief',example='Show the daily decision brief. Read only.',
        cues=[r'\bdaily\b',r'\bdecision brief\b',r'\btoday\b',r'\bpriorit(?:y|ies)\b']),
)


def catalog():
    return [dict(id=s['id'],title=s['title'],description=s['description'],version=VERSION,
        reports=list(s['reports']),actions=list(s['actions']),operations=list(s['operations']),
        report=s['report'],example=s['example']) for s in _SKILLS]


def _definition(ident):
    if not isinstance(ident,str):raise CashflowError('Choose a bundled skill.')
    for row in _SKILLS:
        if row['id']==ident:return row
    raise CashflowError('Choose a bundled skill.')


def load(ident):
    row=_definition(ident)
    try:
        p=_ROOT/row['id']/'SKILL.md'
        if p.is_symlink() or p.parent.is_symlink():raise ValueError()
        raw=p.read_bytes()
        if not 100<=len(raw)<=12000:raise ValueError()
        text=raw.decode('utf-8').replace('\r\n','\n')
        parts=text.split('---\n',2)
        if len(parts)!=3 or parts[0]:raise ValueError()
        metadata=dict(line.split(': ',1) for line in parts[1].strip().splitlines())
        if metadata!={'name':row['id'],'description':row['description']}:raise ValueError()
        body=parts[2].strip()
        if not body:raise ValueError()
    except (OSError,UnicodeError,ValueError):
        raise CashflowError('Bundled skill unavailable; use a direct report.') from None
    return dict(id=row['id'],title=row['title'],version=VERSION,sha256=hashlib.sha256(raw).hexdigest(),instructions=body)


def select(question,requested='auto'):
    if requested not in ('auto',None,''):
        skill=load(requested);skill['selection']='explicit';return skill
    if not isinstance(question,str):raise CashflowError('Ask a text question.')
    # Only a unique best cue match activates guidance. Ambiguity keeps the
    # general catalog; this heuristic never supplies fields or authorizes work.
    scores=[(sum(bool(re.search(cue,question.casefold())) for cue in s['cues']),s['id']) for s in _SKILLS]
    best=max(score for score,_ in scores)
    matches=[ident for score,ident in scores if score==best]
    if not best or len(matches)!=1:return None
    skill=load(matches[0]);skill['selection']='suggested';return skill


def reference(skill):
    return {k:skill[k] for k in ('id','title','version','sha256','selection')} if skill else None


def verify_reference(ref):
    if ref is None:return None
    if not isinstance(ref,dict) or set(ref)!={'id','title','version','sha256','selection'} or ref['selection'] not in {'explicit','suggested'}:
        raise CashflowError('Review a current bundled skill.')
    current=load(ref['id']);current['selection']=ref['selection']
    if reference(current)!=ref:raise CashflowError('Bundled skill changed; review a fresh task.')
    return current


def check_plan(skill,plan):
    if not skill or skill['selection']!='explicit':return
    row=_definition(skill['id']);task=plan['task'];args=plan['parameters']
    allowed=task in row['reports']
    if task=='action_proposal':allowed=args.get('kind') in row['actions']
    if task=='proposal':allowed=bool(args.get('steps')) and all(s.get('operation') in row['operations'] for s in args['steps'])
    if not allowed:raise CashflowError('Task is outside the selected skill; choose another skill or a direct report.')


def fingerprint():
    content=dict(version=VERSION,registry=_SKILLS,code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        resources=[reference(dict(load(s['id']),selection='bundled')) for s in _SKILLS])
    return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()


def validate_registry():
    from .agent_tasks import tasks
    from .agent import AGENT_DRAFT_KINDS
    from .agent_workflows import catalog as operations
    known=set(tasks());ops={r['operation'] for r in operations()['operations']}
    for row in _SKILLS:
        load(row['id'])
        if not set(row['reports'])<=known or not set(row['operations'])<=ops or not set(row['actions'])<=set(AGENT_DRAFT_KINDS) or row['report'] not in row['reports']:
            raise CashflowError('Bundled skill contract is unavailable.')
    return True
