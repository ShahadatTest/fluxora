"""Owner-approved local workflows. Model tools can prepare, never approve/apply.

An explicit function registry reuses existing domain validation. Preparation runs
on a private copy. Owner approval pins the exact steps and financial snapshot;
execution is synchronous, atomic and idempotent. No network/file/credential tool.
"""
import copy
import dataclasses
import inspect
import json
from datetime import date, datetime, timezone

from .authz import OP_DRAFT, OP_READ, RequestContext, require_access
from .errors import CashflowError, IdempotencyConflict
from .planning import _hash
from .owner_workflows import review_revision

MAX_STEPS = 8
MAX_PENDING = 40


def _registry():
    from . import accounting, completion, extension, exploration, obligations, operations, treasury, workbench, business_tools, owner_workflows, funding
    groups = [
        (accounting, 'correct_receipt record_bill_payment correct_bill_payment save_forecast set_collection_hold'),
        (completion, 'create_record adjust_face correct_split_payment record_cash_movement correct_cash_movement import_statement review_statement_row'),
        (extension, 'record_refund correct_refund correct_payment_date create_instalments advance_instalments'),
        (exploration, 'save_scenario'),
        (obligations, 'check_statement_balance carry_credit reverse_credit register_loan void_loan_registration'),
        (operations, 'item_identity review_duplicate create_recurring pause_recurring confirm_recurring'),
        (treasury, 'register_party update_party link_party add_account assign_movement bind_statement observed_transfer reverse_transfer restrict_cash release_cash'),
        (workbench, 'create_promise review_promise review_net_settlement record_unallocated_refund correct_unallocated_refund'),
        (business_tools, 'confirm_fact_check confirm_split_observation allocate_split_balance'),
        (owner_workflows, 'import_observations allocate_observation dismiss_observation confirm_collection_date review_receipt reconcile_existing_receipt reopen_receipt_review'),
        (funding, 'assign_forecast_route'),
    ]
    result = {name: getattr(module, name) for module, names in groups for name in names.split()}
    result['sandbox_send_action'] = _sandbox_send_action
    return result


DATES = {'day','start','end','due','due_date','through','expected_date','new_date','allocation_date','reference_date','from_date','to_date','new_as_of','purchase_date'}
INTEGERS = {'amount','amount_minor','new_amount','new_total','new_amount_minor','opening','closing','days','offset','horizon_days'}
BOOLEANS = {'active','protected','consent_required','accepted'}
ARRAYS = {'rows','schedule','movement_keys','delays','expenses','collections'}
OBJECTS = {'record','spec','context','assignments'}
INTERNAL = {'ledger','revision','ctx','actor','acknowledge_duplicates'}
OBSERVATIONS = {'record_bill_payment','correct_bill_payment','correct_receipt','correct_split_payment',
    'record_cash_movement','correct_cash_movement','record_refund','correct_refund','correct_payment_date',
    'observed_transfer','reverse_transfer','record_unallocated_refund','correct_unallocated_refund',
    'allocate_observation','confirm_split_observation','allocate_split_balance','review_receipt','reconcile_existing_receipt'}

STRUCTURED_HINTS = {
    'create_record': {'record': {'kind':'invoice|bill','id':'unique ID','currency':'recorded currency',
        'amount_minor':'positive integer','date':'YYYY-MM-DD or empty invoice expectation','due_date':'YYYY-MM-DD or empty',
        'category':'invoice|payroll|supplier|operations','protected':'boolean','counterparty':'text',
        'invoice_reference':'text','evidence_id':'existing source ID','note':'source review note'}},
    'create_instalments': {'schedule':[{'date':'YYYY-MM-DD','amount_minor':'positive integer; sum equals remaining payable'}]},
    'advance_instalments': {'step':'approve|accept|apply|cancel; accept requires separate owner source-backed counterparty confirmation'},
    'import_observations': {'rows':[{'id':'unique observation','amount_minor':'positive integer','currency':'recorded currency','date':'YYYY-MM-DD','invoice_reference':'text'}],
        'file_digest':'actual source file SHA256, never invented; imported rows are unallocated evidence, not cash'},
    'import_statement': {'rows':[{'id':'unique row','currency':'recorded currency','signed_minor':'nonzero signed integer','date':'YYYY-MM-DD','reference':'text'}],
        'digest':'actual source SHA256, not a model-generated proof','context':'optional account_id/start/end, as in reviewed statement workflow'},
    'save_scenario': {'delays':[{'invoice_id':'existing invoice','delay_days':'integer'}],
        'expenses':[{'label':'text','currency':'recorded currency','amount_minor':'positive integer','date':'YYYY-MM-DD'}],
        'collections':[{'invoice_id':'existing invoice','percent':'integer 0–100'}]},
    'register_loan': {'spec':{'id':'unique loan ID','lender':'text','reference':'agreement reference','currency':'recorded currency',
        'opening_principal_minor':'positive integer','schedule':[{'date':'YYYY-MM-DD','principal_minor':'integer','interest_minor':'integer'}],
        'evidence_id':'existing source ID','note':'source review','already_in_opening':'owner-reviewed boolean true',
        'not_recorded_elsewhere':'owner-reviewed boolean true'}},
    'confirm_split_observation': {'assignments':{'existing-invoice-id':'positive integer minor units'}},
    'allocate_split_balance': {'assignments':{'existing-invoice-id':'positive integer minor units'}},
    'review_net_settlement': {'movement_keys':['existing signed observation keys; exact net must match statement']},
    'observed_transfer': {'constraints':'Two different own accounts in the SAME currency only, on the current snapshot date, with evidence of an already observed transfer. No FX conversion, external payment or future execution.'},
}


def _parameters(fn):
    return {k:p for k,p in inspect.signature(fn).parameters.items() if k not in INTERNAL}


def catalog(operation=''):
    registry = _registry()
    if not isinstance(operation,str):raise CashflowError('Catalog operation must be text.')
    if operation and operation not in registry:
        raise CashflowError('Unsupported workflow operation. Read the workflow catalog; no arbitrary function call is allowed.')
    def spec(name, fn):
        fields = {}
        for key, parameter in _parameters(fn).items():
            kind = ('ISO date' if key in DATES else 'integer minor units' if key in INTEGERS else
                    'boolean' if key in BOOLEANS else 'array' if key in ARRAYS else 'object' if key in OBJECTS else 'string')
            fields[key] = dict(type=kind, required=parameter.default is inspect.Parameter.empty)
        return dict(operation=name, arguments=fields, observed_fact_confirmation=name in OBSERVATIONS,
                    structured_fields=STRUCTURED_HINTS.get(name,{}),
                    counterparty_confirmation='required separately for step=accept' if name=='advance_instalments' else None)
    return dict(operations=[spec(n,f) if operation else dict(operation=n,required_arguments=[k for k,p in _parameters(f).items() if p.default is inspect.Parameter.empty]) for n,f in registry.items() if not operation or n==operation],
        count=len(registry), max_steps=MAX_STEPS,
        rules='Prepare exact operation/arguments pairs. No approval/execute tool. Owner reviews and approves the whole immutable batch. '
              'Existing source evidence is required by domain rules. Observed payments need a separate owner fact confirmation. '
              'No real sending/payment, consent fabrication, reset/restore, raw attachment or credential access. '
              'observed_transfer is same-currency only. No operation executes or records a cross-currency conversion as a paired FX transaction. '
              'sandbox_send_action includes owner approval of the displayed exact action terms, then simulated send only.')


def _plain(value):
    from decimal import Decimal
    if isinstance(value,Decimal):return str(value)
    if dataclasses.is_dataclass(value):return _plain(dataclasses.asdict(value))
    if isinstance(value,dict):return {str(k):_plain(v) for k,v in value.items() if k not in {'payload_base64','content'}}
    if isinstance(value,(tuple,list,set)):return [_plain(v) for v in value]
    if isinstance(value,(date,datetime)):return value.isoformat()
    return value


def revision(ledger):
    # Other pending jobs and audit reads do not invalidate a financial review.
    return _hash({k:v for k,v in vars(ledger).items() if k not in {'agent_workflows','audit'}})


def _validate_steps(steps):
    if not isinstance(steps,list) or not 1<=len(steps)<=MAX_STEPS:
        raise CashflowError('Prepare 1–8 explicitly named local operations.')
    if len(json.dumps(steps).encode())>32000:raise CashflowError('Workflow parameters exceed 32 KiB.')
    registry=_registry(); validated=[]
    for step in steps:
        if not isinstance(step,dict) or set(step)!={'operation','arguments'}:
            raise CashflowError('Every workflow step needs operation and arguments only.')
        name=step['operation'];args=step['arguments']
        if not isinstance(name,str) or name not in registry:raise CashflowError('Unsupported workflow operation; no approval/permission or arbitrary code operation exists.')
        if not isinstance(args,dict):raise CashflowError('Operation arguments must be a JSON object.')
        parameters=_parameters(registry[name])
        if set(args)-set(parameters):raise CashflowError('Unexpected operation argument; owner flags/context/revision cannot come from the model.')
        if any(p.default is inspect.Parameter.empty and k not in args for k,p in parameters.items()):
            raise CashflowError('Missing required operation argument. Read this operation in the catalog.')
        for key,value in args.items():
            if value is None and (parameters[key].default is None or (name=='create_recurring' and key=='end')):continue
            expected = int if key in INTEGERS else bool if key in BOOLEANS else list if key in ARRAYS else dict if key in OBJECTS else str
            if type(value) is not expected:raise CashflowError('Operation argument type does not match the workflow catalog.')
            if isinstance(value,str) and len(value)>4000:raise CashflowError('Operation text is too long.')
            if key in DATES:
                try:date.fromisoformat(value)
                except ValueError:raise CashflowError('Workflow dates must be ISO YYYY-MM-DD.') from None
        validated.append(copy.deepcopy(step))
    return validated


def _sandbox_send_action(ledger, action_id, *, ctx):
    from . import actions
    action=ledger.actions.get(action_id)
    if action is None:raise CashflowError('Choose an existing action in this business.')
    if action.state=='draft':actions.approve(action,actor=ctx.actor,ctx=ctx)
    return actions.send(action,actor=ctx.actor,ctx=ctx)


def _run_steps(ledger, steps, *, owner, duplicate_ack):
    require_access(owner,ledger)  # internal executor is reachable only after owner authorization
    executor=RequestContext('agent-executor (simulation)' if owner.actor=='owner-preview' else 'agent-executor (owner approved)', 'owner', ledger.business_id)
    results=[]
    for step in steps:
        fn=_registry()[step['operation']];args=copy.deepcopy(step['arguments'])
        for key in DATES & args.keys():
            if args[key] is not None:args[key]=date.fromisoformat(args[key])
        parameters=inspect.signature(fn).parameters
        args['ctx']=executor
        if 'revision' in parameters:args['revision']=review_revision(ledger)
        if 'acknowledge_duplicates' in parameters:args['acknowledge_duplicates']=duplicate_ack
        if 'actor' in parameters:args['actor']=executor.actor
        results.append(dict(operation=step['operation'], result=_plain(fn(ledger,**args))))
    return results


def _cash(ledger):
    return {ccy:ledger.actual_cash(ccy) for ccy in sorted({a.currency for a in ledger.accounts.values()})}


def _projection(ledger):
    from .forecast import forecast
    result=forecast(ledger,ledger.as_of,ledger.horizon_days)
    return {ccy:dict(minimum_minor=c.min_balance_minor,minimum_date=str(c.min_balance_date),
        floor_minor=c.floor_minor,first_breach_date=str(c.first_breach_date) if c.first_breach_date else None,
        closing_minor=c.closing_minor) for ccy,c in result.currencies.items()}


def _changes(before,after):
    changes=[]
    for name,value in vars(after).items():
        if name in {'audit','agent_workflows'} or value==getattr(before,name,None):continue
        old=getattr(before,name,None)
        if isinstance(value,dict) and isinstance(old,dict):
            for ident in sorted(set(old)|set(value)):
                if old.get(ident)!=value.get(ident):changes.append(dict(section=name,id=ident,before=_plain(old.get(ident)),after=_plain(value.get(ident))))
        else:changes.append(dict(section=name,before=_plain(old),after=_plain(value)))
    # A review must not silently omit effects. Large batches must be split.
    if len(json.dumps(changes,default=str).encode())>160000:raise CashflowError('Review is too large; split the workflow into smaller batches.')
    return changes


def _digest(job):
    return _hash({k:job[k] for k in ('id','business_id','label','steps','basis','preview','requires_observed_confirmation','requires_counterparty_confirmation')})


def valid_pending(job,business):
    try:return (job['business_id']==business and job['status']=='pending_approval' and
                job['approval'] is None and job['execution'] is None and job['review_token']==_digest(job))
    except (KeyError,TypeError):return False


def prepare(ledger,job_id,label,steps,*,ctx):
    require_access(ctx,ledger,OP_DRAFT)
    import re
    if not isinstance(job_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',job_id):raise CashflowError('Use a workflow ID of 1–64 letters, numbers, dashes or underscores.')
    if not isinstance(label,str) or not 1<=len(label.strip())<=160:raise CashflowError('Use a workflow label of 1–160 characters.')
    steps=_validate_steps(steps);old=ledger.agent_workflows.get(job_id)
    if old:
        if old['steps']==steps and old['label']==label.strip():return public(old,duplicate=True)
        raise IdempotencyConflict('Workflow ID already has different immutable terms. Prepare a new ID.')
    if len(ledger.agent_workflows)>=200 or sum(j['status']=='pending_approval' for j in ledger.agent_workflows.values())>=MAX_PENDING:
        raise CashflowError('Workflow history/pending bound reached. Review the existing queue first.')
    candidate=copy.deepcopy(ledger)
    # Dry-run only. Allow duplicate review in the preview; actual application
    # still needs the owner's separate acknowledgement or domain guards refuse it.
    owner=RequestContext('owner-preview','owner',ledger.business_id)
    results=_run_steps(candidate,steps,owner=owner,duplicate_ack=True)
    if candidate.business_id!=ledger.business_id:raise CashflowError('A workflow cannot change business scope.')
    preview=dict(changes=_changes(ledger,candidate),results=results,
        cash_before_minor=_cash(ledger),cash_after_minor=_cash(candidate),
        forecast_before=_projection(ledger),forecast_after=_projection(candidate),
        notice='Private simulation only. Generated IDs/timestamps are preview values. Approval covers the exact parameters and unchanged source snapshot. No source truth or money movement is certified.')
    job=dict(id=job_id,business_id=ledger.business_id,label=label.strip(),steps=steps,basis=revision(ledger),preview=preview,
        requires_observed_confirmation=any(s['operation'] in OBSERVATIONS for s in steps),
        requires_counterparty_confirmation=any(s['operation']=='advance_instalments' and s['arguments'].get('step')=='accept' for s in steps),
        status='pending_approval',approval=None,execution=None,prepared_by=ctx.actor,
        created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    job['review_token']=_digest(job);ledger.agent_workflows[job_id]=job
    ledger.log_audit('agent_workflow_prepared',ctx.actor,job_id)
    return public(job)


def public(job,duplicate=False):
    from .money import format_amount
    before=job['preview']['cash_before_minor'];after=job['preview']['cash_after_minor']
    return dict(id=job['id'],label=job['label'],status=job['status'],duplicate=duplicate,
        steps=copy.deepcopy(job['steps']),requires_observed_confirmation=job['requires_observed_confirmation'],
        requires_counterparty_confirmation=job['requires_counterparty_confirmation'],
        cash_before_minor=before,cash_after_minor=after,
        cash_before_display={ccy:format_amount(value,ccy) for ccy,value in before.items()},
        cash_after_display={ccy:format_amount(value,ccy) for ccy,value in after.items()},
        money_unit_notice='Use the exact cash_before_display/cash_after_display strings when describing cash. Raw *_minor integers are minor units, never formatted currency amounts. Pending previews are not observed payments.',
        review_url='/agent/workflows/'+job['id'],
        execution=copy.deepcopy(job['execution']),
        note=('Awaiting explicit owner review/approval. Prepared steps have no financial effect.' if job['status']=='pending_approval' else
              'Local workflow '+job['status']+'. Read its actual execution results; no real money or messages were sent.'))


def decide(ledger,job_id,review_token,decision,*,ctx,observed_confirmed=False,duplicates_confirmed=False,counterparty_confirmed=False):
    require_access(ctx,ledger)  # owner only, including replay and rejection
    job=ledger.agent_workflows.get(job_id)
    if job is None:raise CashflowError('No workflow exists in this business.')
    if review_token!=job['review_token'] or _digest(job)!=review_token:raise CashflowError('Review terms changed. Reload the exact workflow.')
    if decision not in {'approve','reject'}:raise CashflowError('Choose approve or reject.')
    if job['status']!='pending_approval':
        expected='executed' if decision=='approve' else 'rejected'
        if job['status']!=expected:raise IdempotencyConflict('This workflow already has a different owner decision.')
        return dict(id=job_id,status=job['status'],duplicate=True)
    if decision=='reject':
        job['status']='rejected';job['approval']=dict(decision='rejected',actor=ctx.actor)
        ledger.log_audit('agent_workflow_rejected',ctx.actor,job_id)
        return dict(id=job_id,status='rejected',duplicate=False)
    if revision(ledger)!=job['basis']:raise CashflowError('Ledger or action terms changed since preparation. Prepare and review a new workflow; old approval cannot authorize new terms.')
    if job['requires_observed_confirmation'] and observed_confirmed is not True:
        raise CashflowError('Separately confirm the cited observations are source-backed facts, not model assumptions. Approval alone is not payment evidence.')
    if job['requires_counterparty_confirmation'] and counterparty_confirmed is not True:
        raise CashflowError('Separately confirm source-backed external acceptance of these exact supplier terms. Owner approval alone is not counterparty consent.')
    candidate=copy.deepcopy(ledger)
    results=_run_steps(candidate,_validate_steps(job['steps']),owner=ctx,duplicate_ack=duplicates_confirmed is True)
    applied=candidate.agent_workflows[job_id];applied['status']='executed'
    applied['approval']=dict(decision='approved',actor=ctx.actor,review_token=review_token,
        observed_facts_confirmed=observed_confirmed is True,duplicates_confirmed=duplicates_confirmed is True,
        counterparty_acceptance_confirmed=counterparty_confirmed is True)
    applied['execution']=dict(results=results,completed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    candidate.log_audit('agent_workflow_executed','agent-executor',job_id+' approved by '+ctx.actor)
    ledger.__dict__.clear();ledger.__dict__.update(vars(candidate))
    return dict(id=job_id,status='executed',duplicate=False,results=results)
