"""Broader read-only business tools for agent workflow preparation."""
import copy
import inspect
import json
from datetime import date

from .authz import OP_READ, require_access
from .errors import CashflowError
from .agent_workflows import DATES, INTEGERS, ARRAYS, OBJECTS, _parameters, _plain, catalog, public


def views():
    from . import funding, brief, operations, business_tools, workbench, exploration, completion, obligations, owner_workflows, accounting, treasury
    return dict(weekly_cashflow=funding.weekly_cashflow, account_funding=funding.account_funding,
        decision_brief=brief.decision_brief, collections=operations.collection_priorities,
        data_freshness=business_tools.data_freshness, promises=workbench.promise_report,
        search=exploration.search_records, calendar=exploration.cash_calendar,
        statement=completion.statement_report, statement_balance=obligations.statement_balance_report,
        loans=obligations.loan_report, affordability=business_tools.affordability,
        scenario=exploration.compound_scenario, recurring_preview=operations.recurring_preview,
        observation_candidates=owner_workflows.observation_candidates,
        receipt_candidates=owner_workflows.receipt_candidates,
        forecast_actual=accounting.compare_forecast, party_statement=workbench.party_statement,
        daily_close_preview=owner_workflows.daily_preview, attachments=workbench.attachment_inventory,
        cash_accounts=treasury.treasury_report, duplicate_candidates=operations.duplicate_pairs)


def read(ledger,view,parameters,*,ctx):
    require_access(ctx,ledger,OP_READ)
    if not isinstance(parameters,dict):raise CashflowError('Workspace parameters must be a JSON object.')
    if view=='catalog':
        if set(parameters)-{'operation'}:raise CashflowError('Catalog accepts operation only.')
        result=catalog(parameters.get('operation',''))
        if not parameters.get('operation'):
            result['read_views']={name:[k for k,p in _parameters(fn).items() if p.default is inspect.Parameter.empty] for name,fn in views().items()}
        return result
    if view=='queue':
        if set(parameters)-{'job_id','offset'}:raise CashflowError('Queue accepts job_id or offset only.')
        if parameters.get('job_id'):
            if not isinstance(parameters['job_id'],str):raise CashflowError('Workflow ID must be text.')
            job=ledger.agent_workflows.get(parameters['job_id'])
            if job is None:raise CashflowError('Unknown workflow in this business.')
            return public(job)
        offset=parameters.get('offset',0)
        if type(offset) is not int or not 0<=offset<=200:raise CashflowError('Queue offset must be 0–200.')
        rows=list(ledger.agent_workflows.values())
        return dict(total=len(rows),offset=offset,workflows=[dict(id=j['id'],label=j['label'],status=j['status'],
            step_count=len(j['steps']),review_url='/agent/workflows/'+j['id']) for j in rows[offset:offset+20]],
            next_offset=offset+20 if offset+20<len(rows) else None)
    if view not in views():raise CashflowError('Choose a supported read-only workspace view.')
    fn=views()[view];spec=_parameters(fn)
    if set(parameters)-set(spec):raise CashflowError('Unknown workspace parameter; context and revision are server owned.')
    if any(p.default is inspect.Parameter.empty and name not in parameters for name,p in spec.items()):
        raise CashflowError('Missing workspace parameter. Required: '+', '.join(k for k,p in spec.items() if p.default is inspect.Parameter.empty))
    args=copy.deepcopy(parameters)
    for key,value in args.items():
        if value is None and spec[key].default is None:continue
        expected=int if key in INTEGERS else list if key in ARRAYS else dict if key in OBJECTS else str
        if type(value) is not expected:raise CashflowError('Workspace parameter has the wrong type.')
        if key in DATES:
            try:args[key]=date.fromisoformat(value)
            except ValueError:raise CashflowError('Workspace dates use YYYY-MM-DD.') from None
    result=_plain(fn(ledger,**args,ctx=ctx))
    if len(json.dumps(result).encode())>100000:raise CashflowError('Workspace result exceeds 100 KiB; use filters or smaller date windows.')
    return result
